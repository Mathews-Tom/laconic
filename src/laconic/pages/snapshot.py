"""One-shot private extraction for the frozen GitHub Pages evidence snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from laconic import __version__
from laconic.costs import configure_pricing
from laconic.ledger import SCHEMA_VERSION
from laconic.pages.evidence import PAGES_SCHEMA_VERSION, PagesEvidence, build_pages_evidence
from laconic.pricing.registry import PriceRegistry, load_registry
from laconic.runtime.operator import open_query_only, owned_ledger_files
from laconic.runtime.storage import resolve_data_dir
from laconic.spend import claude_code, omp
from laconic.spend.join import join
from laconic.spend.ledger import SessionDecisions
from laconic.spend.omp import SessionUsage

INCLUSIVE_START: Final = "2026-09-06T07:53:09Z"
MAX_METADATA_RECORDS: Final = 50
CLOSED_AGE: Final = timedelta(minutes=30)
MANIFEST_SCHEMA_VERSION: Final = 1
DEFAULT_OMP_ROOT: Final = Path.home() / ".omp" / "agent" / "sessions"
DEFAULT_CLAUDE_ROOT: Final = Path.home() / ".claude" / "projects"
_HEX_40 = re.compile(r"[0-9a-f]{40}")

Provider = Literal["omp", "claude_code"]


class SnapshotError(RuntimeError):
    """Fail-closed extraction error carrying only a public-safe reason code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SessionMetadata:
    cwd: str
    timestamp: str


@dataclass(frozen=True, slots=True)
class TranscriptInventory:
    path: Path
    provider: Provider
    nested: bool
    timestamp: str
    size: int
    mtime_ns: int
    sha256: str

    def private_json(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "provider": self.provider,
            "nested": self.nested,
            "timestamp": self.timestamp,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class SelectedRuntime:
    decisions: tuple[SessionDecisions, ...]
    rows_sha256: str
    ledgers: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    evidence: PagesEvidence
    manifest_sha256: str
    source_inventory_sha256: str
    source_unchanged: bool
    selected_rows_unchanged: bool

    def public_summary(self) -> dict[str, Any]:
        payload = self.evidence.payload
        return {
            "snapshot_id": payload["snapshot_id"],
            "generator_commit": payload["generator_commit"],
            "manifest_sha256": self.manifest_sha256,
            "source_inventory_sha256": self.source_inventory_sha256,
            "inclusive_start": payload["inclusive_start"],
            "exclusive_end": payload["exclusive_end"],
            "population": payload["population"],
            "limitations": payload["limitations"],
            "provider_calls": payload["provider_calls"],
            "source_unchanged": self.source_unchanged,
            "selected_rows_unchanged": self.selected_rows_unchanged,
        }


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise SnapshotError("source_unreadable") from error
    return digest.hexdigest()


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SnapshotError("metadata_invalid") from error
    if parsed.tzinfo is None:
        raise SnapshotError("metadata_invalid")
    return parsed.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    normalized = value.astimezone(UTC).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def metadata_from_record(record: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Read only the two allowlisted top-level metadata fields."""

    cwd = record.get("cwd")
    timestamp = record.get("timestamp")
    if cwd is not None and not isinstance(cwd, str):
        raise SnapshotError("metadata_invalid")
    if timestamp is not None and not isinstance(timestamp, str):
        raise SnapshotError("metadata_invalid")
    return cwd, timestamp


def read_session_metadata(path: Path) -> SessionMetadata:
    """Read at most 50 leading records and discard every non-allowlisted value."""

    cwd: str | None = None
    timestamp: str | None = None
    records = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                records += 1
                if records > MAX_METADATA_RECORDS:
                    break
                try:
                    parsed: Any = json.loads(line)
                except json.JSONDecodeError as error:
                    raise SnapshotError("metadata_invalid") from error
                if not isinstance(parsed, dict):
                    raise SnapshotError("metadata_invalid")
                found_cwd, found_timestamp = metadata_from_record(parsed)
                if cwd is None and found_cwd:
                    cwd = found_cwd
                if timestamp is None and found_timestamp:
                    timestamp = found_timestamp
                del parsed, found_cwd, found_timestamp
                if cwd is not None and timestamp is not None:
                    return SessionMetadata(cwd=cwd, timestamp=timestamp)
    except OSError as error:
        raise SnapshotError("source_unreadable") from error
    raise SnapshotError("metadata_missing")


def _within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _inventory_one(
    path: Path,
    *,
    provider: Provider,
    nested: bool,
    repo_root: Path,
    inclusive_start: datetime,
    exclusive_end: datetime,
) -> TranscriptInventory | None:
    try:
        before = path.stat()
    except OSError:
        return None
    cutoff_ns = int((exclusive_end - CLOSED_AGE).timestamp() * 1_000_000_000)
    if before.st_mtime_ns > cutoff_ns:
        return None
    try:
        metadata = read_session_metadata(path)
        timestamp = _parse_timestamp(metadata.timestamp)
        cwd = Path(metadata.cwd).expanduser().resolve(strict=True)
    except (OSError, SnapshotError):
        return None
    if not cwd.is_dir() or not _within(cwd, repo_root):
        return None
    if timestamp < inclusive_start or timestamp >= exclusive_end:
        return None
    digest = _sha256_file(path)
    try:
        after = path.stat()
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise SnapshotError("source_mutated")
    return TranscriptInventory(
        path=resolved,
        provider=provider,
        nested=nested,
        timestamp=_format_timestamp(timestamp),
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
        sha256=digest,
    )


def discover_inventory(
    *,
    repo_root: Path,
    omp_roots: Sequence[Path],
    claude_roots: Sequence[Path],
    inclusive_start: datetime,
    exclusive_end: datetime,
) -> tuple[TranscriptInventory, ...]:
    """Select the predeclared closed repository cohort using existing walkers."""

    candidates: list[tuple[Path, Provider, bool]] = [
        (path, "omp", nested) for path, nested in omp.find_transcripts(omp_roots)
    ]
    candidates.extend(
        (path, "claude_code", False) for path in claude_code.find_transcripts(claude_roots)
    )
    selected: list[TranscriptInventory] = []
    seen: set[Path] = set()
    for path, provider, nested in candidates:
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        inventory = _inventory_one(
            resolved,
            provider=provider,
            nested=nested,
            repo_root=repo_root,
            inclusive_start=inclusive_start,
            exclusive_end=exclusive_end,
        )
        if inventory is not None:
            selected.append(inventory)
    return tuple(sorted(selected, key=lambda item: (str(item.path), item.provider, item.nested)))


def _load_selected_sessions(inventory: Sequence[TranscriptInventory]) -> tuple[SessionUsage, ...]:
    omp_sessions: list[SessionUsage] = []
    claude_paths: list[Path] = []
    try:
        for item in inventory:
            if item.provider == "omp":
                omp_sessions.append(omp.load_session(item.path, nested=item.nested))
            else:
                claude_paths.append(item.path)
        sessions = omp_sessions + claude_code.load_sessions(claude_paths)
    except Exception as error:
        raise SnapshotError("selected_transcript_invalid") from error
    ids = [session.session_id for session in sessions]
    if len(ids) != len(set(ids)):
        raise SnapshotError("duplicate_session_identity")
    if any(session.unknown_usage_keys for session in sessions):
        raise SnapshotError("unknown_usage_schema")
    return tuple(sorted(sessions, key=lambda session: session.session_id))


def _query_ledger(
    path: Path, session_ids: tuple[str, ...]
) -> tuple[int, list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    decisions: list[tuple[Any, ...]] = []
    expansions: list[tuple[Any, ...]] = []
    try:
        with closing(open_query_only(path)) as database:
            version_row = database.execute("PRAGMA user_version").fetchone()
            if version_row is None:
                raise SnapshotError("runtime_schema_invalid")
            version = int(version_row[0])
            if version != SCHEMA_VERSION:
                raise SnapshotError("runtime_schema_invalid")
            for offset in range(0, len(session_ids), 500):
                chunk = session_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                decision_rows = database.execute(
                    "SELECT session_id, sequence, request_id, tool_name, outcome, reason, "
                    "candidate_reference, raw_chars, visible_chars, latency_ms, created_at "
                    f"FROM runtime_decisions WHERE session_id IN ({placeholders}) "
                    "ORDER BY session_id, sequence",
                    chunk,
                ).fetchall()
                expansion_rows = database.execute(
                    "SELECT session_id, request_id, reference, span, created_at "
                    f"FROM runtime_expansions WHERE session_id IN ({placeholders}) "
                    "ORDER BY session_id, request_id",
                    chunk,
                ).fetchall()
                decisions.extend(tuple(row) for row in decision_rows)
                expansions.extend(tuple(row) for row in expansion_rows)
    except SnapshotError:
        raise
    except sqlite3.Error as error:
        raise SnapshotError("damaged_runtime_ledger") from error
    return version, decisions, expansions


def read_selected_runtime(session_ids: Sequence[str], data_dir: Path) -> SelectedRuntime:
    """Query only selected session rows and bind their canonical digest."""

    selected_ids = tuple(sorted(set(session_ids)))
    if not selected_ids:
        raise SnapshotError("no_selected_sessions")
    canonical: list[dict[str, Any]] = []
    decision_rows: list[tuple[Any, ...]] = []
    expansion_rows: list[tuple[Any, ...]] = []
    owners: dict[str, str] = {}
    ledgers: list[dict[str, Any]] = []
    try:
        paths = owned_ledger_files(resolve_data_dir(data_dir))
    except OSError as error:
        raise SnapshotError("runtime_store_invalid") from error
    for path in paths:
        version, decisions, expansions = _query_ledger(path, selected_ids)
        selected_in_ledger = {str(row[0]) for row in decisions + expansions}
        for session_id in selected_in_ledger:
            previous = owners.setdefault(session_id, path.name)
            if previous != path.name:
                raise SnapshotError("duplicate_runtime_identity")
        if decisions or expansions:
            ledger_record = {
                "ledger_identity": path.name,
                "schema_version": version,
                "decision_rows": len(decisions),
                "expansion_rows": len(expansions),
            }
            ledgers.append(ledger_record)
            canonical.append(
                {
                    **ledger_record,
                    "decisions": decisions,
                    "expansions": expansions,
                }
            )
            decision_rows.extend(decisions)
            expansion_rows.extend(expansions)
    aggregate: dict[str, dict[str, int]] = {}
    for row in decision_rows:
        session_id = str(row[0])
        bucket = aggregate.setdefault(
            session_id,
            {"eligible": 0, "emitted": 0, "raw": 0, "visible": 0, "full": 0, "span": 0},
        )
        bucket["eligible"] += 1
        bucket["emitted"] += int(row[4] == "emitted")
        bucket["raw"] += int(row[7])
        bucket["visible"] += int(row[8])
    for row in expansion_rows:
        session_id = str(row[0])
        bucket = aggregate.setdefault(
            session_id,
            {"eligible": 0, "emitted": 0, "raw": 0, "visible": 0, "full": 0, "span": 0},
        )
        bucket["span" if int(row[3]) else "full"] += 1
    aggregated_decisions = tuple(
        SessionDecisions(
            session_id=session_id,
            eligible=values["eligible"],
            emitted=values["emitted"],
            raw_chars=values["raw"],
            visible_chars=values["visible"],
            full_expansions=values["full"],
            span_expansions=values["span"],
        )
        for session_id, values in sorted(aggregate.items())
    )
    if not aggregated_decisions or not any(row.eligible for row in aggregated_decisions):
        raise SnapshotError("no_joined_runtime_evidence")
    return SelectedRuntime(
        decisions=aggregated_decisions,
        rows_sha256=_sha256_bytes(_canonical_json(canonical)),
        ledgers=tuple(sorted(ledgers, key=lambda item: str(item["ledger_identity"]))),
    )


def _registry_document(registry: PriceRegistry) -> dict[str, Any]:
    if registry.source_commit is None or _HEX_40.fullmatch(registry.source_commit) is None:
        raise SnapshotError("pricing_provenance_invalid")
    rates = {
        model: {
            "input": rate.input,
            "output": rate.output,
            "cache_read": rate.cache_read,
            "cache_write": rate.cache_write,
        }
        for model, rate in sorted(registry.rates.items())
    }
    return {
        "source": registry.source,
        "source_commit": registry.source_commit,
        "overrides": registry.overrides,
        "rates_sha256": _sha256_bytes(_canonical_json(rates)),
    }


def _git_output(repo_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SnapshotError("generator_commit_unverifiable") from error
    return result.stdout.strip()


def permanent_generator_commit(repo_root: Path) -> str:
    """Require HEAD to be the permanent merged wave-one revision."""

    head = _git_output(repo_root, "rev-parse", "HEAD")
    main = _git_output(repo_root, "rev-parse", "main")
    origin_main = _git_output(repo_root, "rev-parse", "origin/main")
    if head != main or head != origin_main or _HEX_40.fullmatch(head) is None:
        raise SnapshotError("generator_commit_not_permanent")
    _git_output(repo_root, "merge-base", "--is-ancestor", head, "origin/main")
    return head


def _inventory_document(inventory: Sequence[TranscriptInventory]) -> list[dict[str, Any]]:
    return [item.private_json() for item in inventory]


def _seal_manifest(directory: Path, payload: dict[str, Any]) -> tuple[str, Path]:
    try:
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        manifest_path = directory / "manifest.json"
        manifest_bytes = _canonical_json(payload)
        manifest_path.write_bytes(manifest_bytes)
        manifest_path.chmod(0o400)
        digest = _sha256_bytes(manifest_bytes)
        digest_path = directory / "manifest.sha256"
        digest_path.write_text(digest + "\n", encoding="ascii")
        digest_path.chmod(0o400)
    except OSError as error:
        raise SnapshotError("manifest_seal_failed") from error
    return digest, manifest_path


def _record_receipt(directory: Path, name: str, payload: dict[str, Any]) -> None:
    path = directory / name
    try:
        path.write_bytes(_canonical_json(payload))
        path.chmod(0o400)
    except OSError as error:
        raise SnapshotError("receipt_write_failed") from error


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
    except OSError as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise SnapshotError("public_write_failed") from error


def generate_snapshot(
    *,
    repo_root: Path,
    manifest_dir: Path,
    output: Path,
) -> SnapshotResult:
    """Run the production extractor with permanent provenance and owned sources."""

    return _generate_snapshot(
        repo_root=repo_root,
        manifest_dir=manifest_dir,
        output=output,
    )


def _generate_snapshot(
    *,
    repo_root: Path,
    manifest_dir: Path,
    output: Path,
    omp_roots: Sequence[Path] = (DEFAULT_OMP_ROOT,),
    claude_roots: Sequence[Path] = (DEFAULT_CLAUDE_ROOT,),
    data_dir: Path | None = None,
    now: datetime | None = None,
    generator_commit: str | None = None,
) -> SnapshotResult:
    """Fixture-capable implementation; production callers use generate_snapshot."""

    if os.environ.get("CI"):
        raise SnapshotError("private_extraction_forbidden_in_ci")

    try:
        root = repo_root.expanduser().resolve(strict=True)
    except OSError as error:
        raise SnapshotError("repository_root_invalid") from error
    if not root.is_dir():
        raise SnapshotError("repository_root_invalid")
    manifest = manifest_dir if manifest_dir.is_absolute() else root / manifest_dir
    destination = output if output.is_absolute() else root / output
    expected_manifest = root / ".laconic" / "research" / "pages-snapshot"
    expected_destination = root / "docs" / "pages" / "evidence" / "laconic-development.json"
    absolute_manifest = Path(os.path.abspath(manifest))
    absolute_destination = Path(os.path.abspath(destination))
    if (
        absolute_manifest != expected_manifest
        or absolute_destination != expected_destination
        or any(
            path.is_symlink()
            for path in (
                root / ".laconic",
                root / ".laconic" / "research",
                expected_manifest,
                root / "docs",
                root / "docs" / "pages",
                root / "docs" / "pages" / "evidence",
                expected_destination,
            )
        )
    ):
        raise SnapshotError("destination_invalid")
    if manifest.exists():
        raise SnapshotError("manifest_attempt_already_exists")

    exclusive = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    inclusive = _parse_timestamp(INCLUSIVE_START)
    if exclusive <= inclusive + CLOSED_AGE:
        raise SnapshotError("snapshot_bounds_invalid")
    exclusive_text = _format_timestamp(exclusive)
    snapshot_id = f"laconic-development-{exclusive.strftime('%Y%m%dT%H%M%SZ')}"
    commit = generator_commit or permanent_generator_commit(root)
    if _HEX_40.fullmatch(commit) is None:
        raise SnapshotError("generator_commit_unverifiable")
    runtime_dir = resolve_data_dir(data_dir)

    inventory = discover_inventory(
        repo_root=root,
        omp_roots=omp_roots,
        claude_roots=claude_roots,
        inclusive_start=inclusive,
        exclusive_end=exclusive,
    )
    if not inventory:
        raise SnapshotError("no_eligible_sessions")
    sessions = _load_selected_sessions(inventory)
    if not sessions:
        raise SnapshotError("no_eligible_sessions")
    runtime = read_selected_runtime([session.session_id for session in sessions], runtime_dir)
    registry = load_registry(runtime_dir)
    registry_document = _registry_document(registry)
    inventory_document = _inventory_document(inventory)
    inventory_sha256 = _sha256_bytes(_canonical_json(inventory_document))
    manifest_payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "repository_root": str(root),
        "inclusive_start": INCLUSIVE_START,
        "exclusive_end": exclusive_text,
        "closed_session_cutoff": _format_timestamp(exclusive - CLOSED_AGE),
        "source_roots": {
            "omp": [str(path.expanduser().resolve(strict=False)) for path in omp_roots],
            "claude_code": [str(path.expanduser().resolve(strict=False)) for path in claude_roots],
        },
        "transcripts": inventory_document,
        "source_inventory_sha256": inventory_sha256,
        "selected_session_ids": sorted(session.session_id for session in sessions),
        "selected_runtime_rows_sha256": runtime.rows_sha256,
        "selected_runtime_ledgers": list(runtime.ledgers),
        "runtime_schema_version": SCHEMA_VERSION,
        "generator_commit": commit,
        "laconic_version": __version__,
        "price_registry": registry_document,
        "public_schema_version": PAGES_SCHEMA_VERSION,
    }
    manifest_sha256, _ = _seal_manifest(manifest, manifest_payload)

    try:
        configure_pricing(runtime_dir)
        composition = join(sessions, runtime.decisions, damaged_ledgers=0)
        evidence = build_pages_evidence(
            composition,
            snapshot_id=snapshot_id,
            inclusive_start=INCLUSIVE_START,
            exclusive_end=exclusive_text,
            laconic_version=__version__,
            generator_commit=commit,
            manifest_sha256=manifest_sha256,
            source_inventory_sha256=inventory_sha256,
        )
        after_inventory = discover_inventory(
            repo_root=root,
            omp_roots=omp_roots,
            claude_roots=claude_roots,
            inclusive_start=inclusive,
            exclusive_end=exclusive,
        )
        if _inventory_document(after_inventory) != inventory_document:
            raise SnapshotError("source_mutated")
        after_runtime = read_selected_runtime(
            [session.session_id for session in sessions], runtime_dir
        )
        if after_runtime.rows_sha256 != runtime.rows_sha256:
            raise SnapshotError("selected_runtime_rows_mutated")
        after_registry = _registry_document(load_registry(runtime_dir))
        if after_registry != registry_document:
            raise SnapshotError("pricing_source_mutated")
        result = SnapshotResult(
            evidence=evidence,
            manifest_sha256=manifest_sha256,
            source_inventory_sha256=inventory_sha256,
            source_unchanged=True,
            selected_rows_unchanged=True,
        )
        _record_receipt(
            manifest,
            "ready.json",
            {
                "status": "validated",
                "public_sha256": evidence.sha256,
                "source_unchanged": True,
                "selected_rows_unchanged": True,
                "provider_calls": 0,
            },
        )
        _atomic_write(destination, evidence.to_json().encode("utf-8"))
    except SnapshotError as error:
        _record_receipt(manifest, "failure.json", {"status": "failed", "reason": error.code})
        raise
    except Exception as error:
        _record_receipt(
            manifest,
            "failure.json",
            {"status": "failed", "reason": "unexpected_private_extraction_failure"},
        )
        raise SnapshotError("unexpected_private_extraction_failure") from error

    return result
