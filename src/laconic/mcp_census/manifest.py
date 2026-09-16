"""Population freeze for the post-v0.12 MCP opportunity census."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from laconic import __version__

SCHEMA_VERSION: Final = 1
CENSUS_ID: Final = "mcp-opportunity-post-v0.12.0"
INCLUSIVE_START: Final = "2026-09-12T17:02:31Z"
NORMALIZED_CATEGORIES: Final = ("mcp",)
CONSTANT_SUBJECT: Final = "mcp"
MIN_RESULTS: Final = 100
MIN_SESSIONS: Final = 10
MIN_CHARACTER_SHARE_PCT: Final = 10.0
MIN_CHARACTERS: Final = 1_000_000
MIN_EMISSION_PCT: Final = 20.0
MIN_REDUCTION_PCT: Final = 20.0

MANIFEST_KEYS: Final = frozenset(
    {
        "schema_version",
        "census_id",
        "inclusive_start",
        "exclusive_end",
        "files",
        "inventory_sha256",
        "normalized_categories",
        "eligibility",
        "codec",
        "thresholds",
        "allowed_public_keys",
        "provider_calls",
        "source_mode",
    }
)
FILE_KEYS: Final = frozenset({"host", "size", "mtime_ns", "sha256"})
SOURCE_ROOT_KEYS: Final = frozenset({"omp", "claude_code"})
ELIGIBILITY_KEYS: Final = frozenset(
    {
        "successful",
        "single_text",
        "non_empty",
        "attributed_session",
        "inclusive_start",
        "exclusive_end",
    }
)
CODEC_KEYS: Final = frozenset(
    {"laconic_version", "encoder", "subject", "keep_head", "keep_tail", "max_errors"}
)
THRESHOLD_KEYS: Final = frozenset(
    {
        "minimum_results",
        "minimum_sessions",
        "minimum_character_share_pct",
        "minimum_characters",
        "minimum_emission_pct",
        "minimum_reduction_pct",
        "maximum_recovery_mismatches",
    }
)
PUBLIC_KEYS: Final = (
    "schema_version",
    "census_id",
    "manifest_sha256",
    "source_inventory_sha256",
    "population",
    "predicates",
    "disposition",
    "failed_predicates",
    "provider_calls",
    "limitations",
)


class ManifestError(ValueError):
    """The frozen population manifest is malformed or no longer matches its sources."""


@dataclass(frozen=True, slots=True)
class SourceFile:
    host: str
    size: int
    mtime_ns: int
    sha256: str
    path: Path | None = field(default=None, compare=False, repr=False)

    def to_json(self) -> dict[str, object]:
        return {
            "host": self.host,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class CensusManifest:
    path: Path
    payload: dict[str, Any]
    sha256: str
    files: tuple[SourceFile, ...]
    source_roots: dict[str, Path] = field(compare=False, repr=False)

    @property
    def inclusive_start(self) -> datetime:
        return parse_utc(self.payload["inclusive_start"])

    @property
    def exclusive_end(self) -> datetime:
        return parse_utc(self.payload["exclusive_end"])


def canonical_json(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ManifestError("exclusive end must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ManifestError("timestamp must be an RFC 3339 UTC string")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ManifestError("timestamp must be an RFC 3339 UTC string") from error
    return parsed.astimezone(UTC)


def _eligibility_contract(exclusive_end: str) -> dict[str, object]:
    return {
        "successful": True,
        "single_text": True,
        "non_empty": True,
        "attributed_session": True,
        "inclusive_start": INCLUSIVE_START,
        "exclusive_end": exclusive_end,
    }


def _codec_contract() -> dict[str, object]:
    return {
        "laconic_version": __version__,
        "encoder": "FallbackEncoder",
        "subject": CONSTANT_SUBJECT,
        "keep_head": 40,
        "keep_tail": 40,
        "max_errors": 20,
    }


def _threshold_contract() -> dict[str, object]:
    return {
        "minimum_results": MIN_RESULTS,
        "minimum_sessions": MIN_SESSIONS,
        "minimum_character_share_pct": MIN_CHARACTER_SHARE_PCT,
        "minimum_characters": MIN_CHARACTERS,
        "minimum_emission_pct": MIN_EMISSION_PCT,
        "minimum_reduction_pct": MIN_REDUCTION_PCT,
        "maximum_recovery_mismatches": 0,
    }


def _atomic_create(path: Path, text: str) -> None:
    if path.exists() or path.is_symlink():
        raise ManifestError(f"refusing to replace frozen manifest: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _inventory_one(host: str, root: Path) -> tuple[SourceFile, ...]:
    resolved = root.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ManifestError(f"source root is not a directory: {resolved}")
    files: list[SourceFile] = []
    for path in sorted(resolved.rglob("*.jsonl")):
        if path.is_symlink():
            raise ManifestError(f"source inventory contains a symlink: {path}")
        if not path.is_file():
            continue
        stat = path.stat()
        files.append(
            SourceFile(
                host=host,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                sha256=sha256_file(path),
                path=path,
            )
        )
    return tuple(files)


def inventory(source_roots: dict[str, Path]) -> tuple[SourceFile, ...]:
    if frozenset(source_roots) != SOURCE_ROOT_KEYS:
        raise ManifestError("source roots must contain exactly omp and claude_code")
    files = [
        source
        for host in sorted(source_roots)
        for source in _inventory_one(host, source_roots[host])
    ]
    return tuple(sorted(files, key=lambda item: (item.host, item.sha256, item.size, item.mtime_ns)))


def inventory_sha256(files: tuple[SourceFile, ...]) -> str:
    return sha256_bytes(canonical_json([entry.to_json() for entry in files]).encode("utf-8"))


def freeze_manifest(
    path: Path,
    *,
    source_roots: dict[str, Path],
    exclusive_end: datetime | None = None,
) -> CensusManifest:
    """Mint the cutoff, inventory sources, then persist before result parsing."""
    cutoff = exclusive_end if exclusive_end is not None else utc_now()
    start = parse_utc(INCLUSIVE_START)
    if cutoff <= start:
        raise ManifestError("exclusive end must follow the inclusive start")
    resolved_roots = {
        host: root.expanduser().resolve(strict=True) for host, root in source_roots.items()
    }
    files = inventory(resolved_roots)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "census_id": CENSUS_ID,
        "inclusive_start": INCLUSIVE_START,
        "exclusive_end": format_utc(cutoff),
        "files": [entry.to_json() for entry in files],
        "inventory_sha256": inventory_sha256(files),
        "normalized_categories": list(NORMALIZED_CATEGORIES),
        "eligibility": _eligibility_contract(format_utc(cutoff)),
        "codec": _codec_contract(),
        "thresholds": _threshold_contract(),
        "allowed_public_keys": list(PUBLIC_KEYS),
        "provider_calls": 0,
        "source_mode": "read_only",
    }
    text = canonical_json(payload)
    _atomic_create(path, text)
    return CensusManifest(
        path=path,
        payload=payload,
        sha256=sha256_bytes(text.encode("utf-8")),
        files=files,
        source_roots=resolved_roots,
    )


def _exact_keys(value: object, allowed: frozenset[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != allowed:
        raise ManifestError(f"{field} must contain exactly {sorted(allowed)}")
    return cast(dict[str, Any], value)


def _source_file(value: object) -> SourceFile:
    row = _exact_keys(value, FILE_KEYS, "manifest file")
    host = row["host"]
    size = row["size"]
    mtime_ns = row["mtime_ns"]
    digest = row["sha256"]
    if host not in SOURCE_ROOT_KEYS:
        raise ManifestError("manifest file host is invalid")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ManifestError("manifest file size must be a non-negative integer")
    if isinstance(mtime_ns, bool) or not isinstance(mtime_ns, int) or mtime_ns < 0:
        raise ManifestError("manifest file mtime must be a non-negative integer")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ManifestError("manifest file digest must be lowercase SHA-256")
    return SourceFile(host, size, mtime_ns, digest)


def load_manifest(path: Path) -> CensusManifest:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot read census manifest: {path}") from error
    root = _exact_keys(payload, MANIFEST_KEYS, "manifest")
    if root["schema_version"] != SCHEMA_VERSION or root["census_id"] != CENSUS_ID:
        raise ManifestError("unsupported census manifest identity")
    eligibility = _exact_keys(root["eligibility"], ELIGIBILITY_KEYS, "eligibility")
    codec = _exact_keys(root["codec"], CODEC_KEYS, "codec")
    thresholds = _exact_keys(root["thresholds"], THRESHOLD_KEYS, "thresholds")
    if eligibility != _eligibility_contract(root["exclusive_end"]):
        raise ManifestError("eligibility contract changed")
    if codec != _codec_contract():
        raise ManifestError("codec contract changed")
    if thresholds != _threshold_contract():
        raise ManifestError("threshold contract changed")
    if root["normalized_categories"] != list(NORMALIZED_CATEGORIES):
        raise ManifestError("normalized categories changed")
    if root["allowed_public_keys"] != list(PUBLIC_KEYS):
        raise ManifestError("public allowlist changed")
    if root["provider_calls"] != 0 or root["source_mode"] != "read_only":
        raise ManifestError("census must remain read-only with zero provider calls")
    start = parse_utc(root["inclusive_start"])
    end = parse_utc(root["exclusive_end"])
    if start != parse_utc(INCLUSIVE_START) or end <= start:
        raise ManifestError("population time bounds changed")
    if (
        root["eligibility"]["inclusive_start"] != root["inclusive_start"]
        or root["eligibility"]["exclusive_end"] != root["exclusive_end"]
    ):
        raise ManifestError("eligibility bounds disagree with population bounds")
    rows = root["files"]
    if not isinstance(rows, list):
        raise ManifestError("manifest files must be a list")
    files = tuple(_source_file(row) for row in rows)
    if list(files) != sorted(
        files,
        key=lambda item: (item.host, item.sha256, item.size, item.mtime_ns),
    ):
        raise ManifestError("manifest files must be sorted")
    if root["inventory_sha256"] != inventory_sha256(files):
        raise ManifestError("manifest inventory digest mismatch")
    canonical = canonical_json(root).encode("utf-8")
    if raw != canonical:
        raise ManifestError("manifest must use canonical JSON serialization")
    return CensusManifest(
        path=path,
        payload=root,
        sha256=sha256_bytes(raw),
        files=files,
        source_roots={},
    )


def current_inventory_matches(manifest: CensusManifest) -> bool:
    if frozenset(manifest.source_roots) != SOURCE_ROOT_KEYS:
        return False
    try:
        return inventory(manifest.source_roots) == manifest.files
    except (OSError, ManifestError):
        return False
