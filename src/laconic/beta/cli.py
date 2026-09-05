"""``python -m laconic.beta``: the M18 qualification-campaign CLI.

Three command groups, matching the M18 PR-1 contract exactly:

- ``manifest generate|validate|hash`` -- the frozen campaign contract.
- ``receipt derive|validate`` -- one privacy-bounded per-session receipt,
  derived from an existing runtime session ledger via
  `laconic.runtime.storage.RuntimeStorage` (never a second raw store).
- ``report generate|check`` -- the deterministic Markdown aggregate report,
  with a freshness (``check``) mode for a committed report.

Every command that writes writes only after its payload clears
`laconic.beta.privacy`'s (or, for the manifest, `laconic.beta.manifest`'s)
exact-key allowlist, so a future field added to a dataclass without updating
that allowlist fails the write instead of silently reaching disk. Terminal
output is out of that boundary: a command may name a path the operator
themselves supplied, but never a value read out of an evidence file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from laconic.beta.manifest import (
    MIN_SESSIONS,
    ManifestValidationError,
    build_manifest,
    fingerprint_manifest,
    validate_manifest_json,
)
from laconic.beta.privacy import PrivacyViolationError, validate_receipt_json, validate_report_json
from laconic.beta.receipt import (
    ReceiptDerivationError,
    ReceiptFormatError,
    derive_receipt,
    receipt_from_json,
    repository_id_for_path,
)
from laconic.beta.report import EvidenceValidationError, render_from_payloads
from laconic.beta.scenarios import ALL_SCENARIOS
from laconic.runtime.references import InvalidSessionIdError
from laconic.runtime.storage import (
    RuntimeStorage,
    SessionLedgerNotFoundError,
    UnsafeStoragePathError,
    resolve_data_dir,
)

#: argparse owns 2 for its own usage errors.
EXIT_OK = 0
EXIT_VALIDATION_ERROR = 1
EXIT_REPORT_DRIFT = 3


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any], *, replace: bool = True) -> None:
    """Write one JSON artifact atomically, never through a symlink.

    ``replace=False`` refuses an existing target: the frozen manifest is the
    one artifact whose whole purpose is to predate its results, so silently
    re-freezing it over an in-flight campaign must take an explicit flag.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError(f"refusing to write through a symlink: {path}")
    if not replace and path.exists():
        raise FileExistsError(f"refusing to overwrite an existing file: {path}")
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _atomic_write_text(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _canonical_git_root(root: Path) -> Path:
    """Resolve one operator-supplied repository root, refusing anything that
    is not a canonical Git root.

    `.docs/DEVELOPMENT_PLAN.md` §6 M18 requires the campaign span "at least 3
    canonical Git roots". Hashing whatever directory an operator names would
    let three subdirectories of a single checkout satisfy the manifest's
    exactly-3-distinct-repository-IDs rule, so the report's repository count
    would stop meaning what the acceptance criterion says it means. Requiring
    a real ``.git`` directory also rejects the ``.git``-file form, which is
    how a linked worktree or a submodule presents itself -- three worktrees
    of one repository are three paths but one repository, and would otherwise
    clear the same rule.

    Repository identity remains the resolved root path (hashed), so two
    independent clones of one upstream still count separately. That residual
    is deliberate: distinguishing them needs the repository's own history,
    and the frozen manifest is reviewed by a human at the M18 gate.
    """
    resolved = root.expanduser().resolve(strict=False)
    git_entry = resolved / ".git"
    if not resolved.is_dir() or not git_entry.is_dir() or git_entry.is_symlink():
        raise ManifestValidationError(f"not a canonical Git root: {root}")
    return resolved


def _manifest_generate(args: argparse.Namespace) -> int:
    try:
        candidate_wheel_sha256 = hashlib.sha256(args.candidate_wheel.read_bytes()).hexdigest()
    except OSError as error:
        print(f"laconic.beta manifest generate: candidate wheel: {error}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    try:
        slots = tuple(
            (slot, repository_id_for_path(_canonical_git_root(Path(root))))
            for slot, root in enumerate(args.repositories, start=1)
        )
        manifest = build_manifest(candidate_wheel_sha256=candidate_wheel_sha256, slots=slots)
    except ManifestValidationError as error:
        print(f"laconic.beta manifest generate: {error}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    payload = manifest.to_json()
    try:
        validate_manifest_json(payload)
        _write_json(args.out, payload, replace=args.force)
    except (ManifestValidationError, OSError) as error:
        print(f"laconic.beta manifest generate: {error}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    print(f"wrote frozen M18 manifest ({fingerprint_manifest(manifest)}): {args.out}")
    return EXIT_OK


def _manifest_validate(args: argparse.Namespace) -> int:
    try:
        manifest = validate_manifest_json(_read_json(args.path))
    except (ManifestValidationError, OSError, json.JSONDecodeError) as error:
        print(f"laconic.beta manifest validate: {error}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    print(f"ok: {args.path} matches the frozen M18 manifest ({fingerprint_manifest(manifest)})")
    return EXIT_OK


def _manifest_hash(args: argparse.Namespace) -> int:
    try:
        manifest = validate_manifest_json(_read_json(args.path))
    except (ManifestValidationError, OSError, json.JSONDecodeError) as error:
        print(f"laconic.beta manifest hash: {error}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    print(fingerprint_manifest(manifest))
    return EXIT_OK


def _parse_scenarios(value: str) -> tuple[str, ...]:
    names = tuple(item.strip() for item in value.split(",") if item.strip())
    unknown = set(names) - ALL_SCENARIOS
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown scenario name(s): {sorted(unknown)}")
    return names


def _receipt_derive(args: argparse.Namespace) -> int:
    try:
        manifest = validate_manifest_json(_read_json(args.manifest))
    except (ManifestValidationError, OSError, json.JSONDecodeError) as error:
        print(f"laconic.beta receipt derive: invalid manifest: {error}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    try:
        root = resolve_data_dir(args.data_dir)
        sessions = root / "sessions"
        if not root.is_dir() or root.is_symlink() or not sessions.is_dir() or sessions.is_symlink():
            raise SessionLedgerNotFoundError("runtime storage does not exist")
        storage = RuntimeStorage(root)
        receipt = derive_receipt(
            storage,
            session_id=args.session,
            manifest=manifest,
            omp_version=args.omp_version,
            candidate_wheel_path=args.candidate_wheel,
            slot=args.slot,
            repository_root=args.repository,
            clean_shutdown=args.clean_shutdown,
            started_at=args.started_at,
            ended_at=args.ended_at,
            scenarios=args.scenarios,
            observed_corruption=args.observed_corruption,
        )
    except ReceiptDerivationError as error:
        print(f"laconic.beta receipt derive: {error}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    except (SessionLedgerNotFoundError, InvalidSessionIdError):
        # Never echo the caller's session id, not even back to their own
        # terminal: it is the one input this command promises not to expose.
        print(
            "laconic.beta receipt derive: no local ledger for the named session",
            file=sys.stderr,
        )
        return EXIT_VALIDATION_ERROR
    except (UnsafeStoragePathError, OSError) as error:
        print(f"laconic.beta receipt derive: runtime storage is unusable: {error}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    payload = receipt.to_json()
    try:
        validate_receipt_json(payload)
    except PrivacyViolationError as error:  # pragma: no cover - defense in depth
        print(f"laconic.beta receipt derive: refusing an invalid receipt: {error}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    try:
        _write_json(args.out, payload)
    except OSError as error:
        print(f"laconic.beta receipt derive: {error}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    print(f"wrote receipt for slot {receipt.slot}: {args.out}")
    return EXIT_OK


def _receipt_validate(args: argparse.Namespace) -> int:
    try:
        payload = _read_json(args.path)
        validate_receipt_json(payload)
        receipt = receipt_from_json(payload)
    except (PrivacyViolationError, ReceiptFormatError, OSError, json.JSONDecodeError) as error:
        print(f"laconic.beta receipt validate: {error}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    print(f"ok: {args.path} is a valid slot-{receipt.slot} receipt")
    return EXIT_OK


def _load_receipt_payloads(receipts_dir: Path) -> list[dict[str, Any]]:
    """Read every ordinary ``*.json`` file directly inside ``receipts_dir``.

    Symlinks are skipped rather than followed, so the receipts directory
    means exactly the files it contains.
    """
    return [
        _read_json(path)
        for path in sorted(receipts_dir.glob("*.json"))
        if path.is_file() and not path.is_symlink()
    ]


def _report_generate(args: argparse.Namespace) -> int:
    try:
        manifest = validate_manifest_json(_read_json(args.manifest))
        payloads = _load_receipt_payloads(args.receipts_dir)
        report, markdown = render_from_payloads(payloads, manifest)
    except (
        ManifestValidationError,
        EvidenceValidationError,
        OSError,
        json.JSONDecodeError,
    ) as error:
        print(f"laconic.beta report generate: {error}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    try:
        validate_report_json(report.to_json())
    except PrivacyViolationError as error:  # pragma: no cover - defense in depth
        print(f"laconic.beta report generate: refusing an invalid report: {error}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        if args.out.is_symlink():
            raise OSError(f"refusing to write through a symlink: {args.out}")
        _atomic_write_text(args.out, markdown)
    except OSError as error:
        print(f"laconic.beta report generate: {error}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    print(f"wrote {report.verdict.value} report ({len(payloads)} receipt(s)): {args.out}")
    return EXIT_OK


def _report_check(args: argparse.Namespace) -> int:
    try:
        manifest = validate_manifest_json(_read_json(args.manifest))
        payloads = _load_receipt_payloads(args.receipts_dir)
        report, fresh_markdown = render_from_payloads(payloads, manifest)
        validate_report_json(report.to_json())
    except (
        ManifestValidationError,
        EvidenceValidationError,
        PrivacyViolationError,
        OSError,
        json.JSONDecodeError,
    ) as error:
        print(f"laconic.beta report check: {error}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    try:
        committed_markdown = args.report.read_text(encoding="utf-8")
    except OSError as error:
        print(f"laconic.beta report check: {error}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    if committed_markdown != fresh_markdown:
        print(f"stale: {args.report} does not match the current receipt evidence", file=sys.stderr)
        return EXIT_REPORT_DRIFT
    print(f"ok: {args.report} matches the current receipt evidence ({report.verdict.value})")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """Build the M18 qualification CLI surface."""
    parser = argparse.ArgumentParser(
        prog="python -m laconic.beta",
        description="M18 runtime beta qualification: manifest, receipt, and report tooling.",
    )
    commands = parser.add_subparsers(dest="command")

    manifest = commands.add_parser("manifest", help="frozen campaign manifest")
    manifest_commands = manifest.add_subparsers(dest="manifest_command")

    manifest_generate = manifest_commands.add_parser(
        "generate",
        help="freeze one campaign manifest: build-time contract plus this campaign's population",
        description=(
            "Hashes the candidate wheel and the 10 ordered repository roots locally; "
            "the manifest never contains a path, only their SHA-256 digests. Every "
            "root must be a canonical Git root, and the 10 must name exactly 3 "
            "distinct repositories."
        ),
    )
    manifest_generate.add_argument("--candidate-wheel", type=Path, required=True)
    manifest_generate.add_argument(
        "repositories",
        metavar="REPOSITORY_ROOT",
        nargs=MIN_SESSIONS,
        help=(
            f"exactly {MIN_SESSIONS} canonical Git roots, in slot order (slot 1 first), "
            "naming exactly 3 distinct repositories"
        ),
    )
    manifest_generate.add_argument("--out", type=Path, required=True)
    manifest_generate.add_argument(
        "--force",
        action="store_true",
        help="replace an existing manifest; refuses to overwrite one by default",
    )
    manifest_generate.set_defaults(handler=_manifest_generate)

    manifest_validate = manifest_commands.add_parser(
        "validate", help="check a manifest file reproduces the frozen M18 contract exactly"
    )
    manifest_validate.add_argument("path", type=Path)
    manifest_validate.set_defaults(handler=_manifest_validate)

    manifest_hash = manifest_commands.add_parser(
        "hash", help="print a validated manifest's canonical SHA-256 fingerprint"
    )
    manifest_hash.add_argument("path", type=Path)
    manifest_hash.set_defaults(handler=_manifest_hash)

    receipt = commands.add_parser("receipt", help="per-session qualification receipts")
    receipt_commands = receipt.add_subparsers(dest="receipt_command")

    receipt_derive = receipt_commands.add_parser(
        "derive",
        help="derive one privacy-bounded receipt from an existing session ledger",
    )
    receipt_derive.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="runtime storage root (default: platform data dir)",
    )
    receipt_derive.add_argument(
        "--session", required=True, help="real OMP session id (never serialized)"
    )
    receipt_derive.add_argument("--manifest", type=Path, required=True)
    receipt_derive.add_argument("--omp-version", required=True)
    receipt_derive.add_argument("--candidate-wheel", type=Path, required=True)
    receipt_derive.add_argument("--slot", type=int, required=True, help="1-based session ordinal")
    receipt_derive.add_argument(
        "--repository", type=Path, required=True, help="real repository root (never serialized)"
    )
    receipt_derive.add_argument(
        "--clean-shutdown",
        action=argparse.BooleanOptionalAction,
        required=True,
        help="whether OMP reached a clean session_shutdown for this session",
    )
    receipt_derive.add_argument("--started-at", type=float, required=True, help="epoch seconds")
    receipt_derive.add_argument("--ended-at", type=float, required=True, help="epoch seconds")
    receipt_derive.add_argument(
        "--scenarios",
        type=_parse_scenarios,
        default=(),
        help="comma-separated M18 scenario names this session exercised",
    )
    receipt_derive.add_argument(
        "--observed-corruption",
        type=int,
        default=0,
        help="operator-observed result-corruption incidents for this session (default: 0)",
    )
    receipt_derive.add_argument("--out", type=Path, required=True)
    receipt_derive.set_defaults(handler=_receipt_derive)

    receipt_validate = receipt_commands.add_parser(
        "validate", help="check a receipt file is privacy-valid and internally consistent"
    )
    receipt_validate.add_argument("path", type=Path)
    receipt_validate.set_defaults(handler=_receipt_validate)

    report = commands.add_parser("report", help="deterministic aggregate campaign report")
    report_commands = report.add_subparsers(dest="report_command")

    report_generate = report_commands.add_parser(
        "generate", help="render the aggregate report from a receipt directory"
    )
    report_generate.add_argument("--receipts-dir", type=Path, required=True)
    report_generate.add_argument("--manifest", type=Path, required=True)
    report_generate.add_argument("--out", type=Path, required=True)
    report_generate.set_defaults(handler=_report_generate)

    report_check = report_commands.add_parser(
        "check", help="fail if a committed report has drifted from current receipt evidence"
    )
    report_check.add_argument("--receipts-dir", type=Path, required=True)
    report_check.add_argument("--manifest", type=Path, required=True)
    report_check.add_argument("--report", type=Path, required=True)
    report_check.set_defaults(handler=_report_check)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the M18 qualification CLI and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_OK
    exit_code: int = handler(args)
    return exit_code
