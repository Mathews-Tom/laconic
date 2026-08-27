"""Stage A core primitives: provider-agnostic ledger schema, source-root
allowlist containment, and the file-admission gate.

Governed by `.docs/K1_REPRESENTATIVE_CORPUS_PROTOCOL.md` § Stage A and
`.docs/K1_STAGE_A_DESIGN.md` §§ 5-7, 9. This module never reads a session
file's content -- it operates only on filesystem metadata (path, size,
mtime, symlink-ness) and on a `cwd` string a caller has already extracted
via the bounded, key-allowlisted read in `laconic.k1corpus.providers`. It
performs no provider-specific file discovery of its own.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

#: Recency threshold below which a session file is treated as still being
#: appended to, and excluded. Design doc §6.3: no provider here exposes a
#: reliable cross-process "session closed" signal, so a sustained silence
#: is used as a conservative proxy. This cannot misclassify a genuinely
#: active interactive session as closed (a live session is written far
#: more often than every 30 minutes); it can misclassify a paused-but-
#: resumable session as closed after 30 idle minutes, which is the
#: documented, safe-direction approximation.
STAGE_A_ACTIVE_THRESHOLD_SECONDS = 30 * 60

#: Bounded number of leading JSONL lines a provider adapter may parse
#: while searching for one allowlisted `cwd` key. Design doc §4.
STAGE_A_SCAN_LINE_BOUND = 50


class Provider(StrEnum):
    """One of the three providers authorized by H-53."""

    CLAUDE_CODE = "claude-code"
    CODEX = "codex"
    OMP = "omp"


class SizeBand(StrEnum):
    """A coarse magnitude bucket for a whole session file's byte size."""

    XS = "xs"
    S = "s"
    M = "m"
    L = "l"
    XL = "xl"


class AgeBand(StrEnum):
    """A coarse bucket for how long ago a session file was last modified."""

    LT_1D = "lt_1d"
    D1_7 = "1d_7d"
    D7_30 = "7d_30d"
    D30_90 = "30d_90d"
    GT_90D = "gt_90d"


class ClosureStatus(StrEnum):
    """A session file's closure state. Stage A only ever emits `CLOSED`
    into a ledger record -- an active file is excluded before a record is
    ever built (see `stat_admission`). `ACTIVE` exists for forward
    compatibility with a future re-scan mode, not because Stage A emits it."""

    CLOSED = "closed"
    ACTIVE = "active"


class ExclusionReason(StrEnum):
    """Why a candidate session file was not admitted into the ledger.
    Every exclusion is counted by reason; none is silently dropped."""

    SYMLINK = "symlink"
    NOT_A_REGULAR_FILE = "not_a_regular_file"
    ACTIVE = "active"
    OUTSIDE_ALLOWLIST = "outside_allowlist"
    CWD_NOT_FOUND = "cwd_not_found_within_scan_bound"
    UNPARSEABLE = "unparseable"


#: Ascending, exclusive-upper-bound size thresholds in bytes; a size at or
#: above the last threshold falls into `SizeBand.XL`.
_SIZE_BAND_THRESHOLDS: tuple[tuple[int, SizeBand], ...] = (
    (10 * 1024, SizeBand.XS),
    (100 * 1024, SizeBand.S),
    (1024 * 1024, SizeBand.M),
    (10 * 1024 * 1024, SizeBand.L),
)

#: Ascending, exclusive-upper-bound age thresholds in seconds; an age at
#: or beyond the last threshold falls into `AgeBand.GT_90D`.
_AGE_BAND_THRESHOLDS: tuple[tuple[float, AgeBand], ...] = (
    (1 * 86400, AgeBand.LT_1D),
    (7 * 86400, AgeBand.D1_7),
    (30 * 86400, AgeBand.D7_30),
    (90 * 86400, AgeBand.D30_90),
)


def size_band(size_bytes: int) -> SizeBand:
    """Bucket a session file's byte size into a coarse magnitude band."""
    for threshold, band in _SIZE_BAND_THRESHOLDS:
        if size_bytes < threshold:
            return band
    return SizeBand.XL


def age_band(age_seconds: float) -> AgeBand:
    """Bucket a session file's age (seconds since last modification) into
    a coarse time band."""
    for threshold, band in _AGE_BAND_THRESHOLDS:
        if age_seconds < threshold:
            return band
    return AgeBand.GT_90D


def provenance_hash(*, provider: Provider, file_path: Path, size_bytes: int, mtime_ns: int) -> str:
    """A deliberately re-derivable audit/dedup fingerprint for one session
    file, hashing its identity metadata (never its content). An operator
    with local file access can recompute this from the real path, size,
    and mtime to trace a ledger row back to its source file; it is not a
    path-concealment mechanism (that is `project_lineage_id`'s job)."""
    canonical = f"{provider.value}|{file_path}|{size_bytes}|{mtime_ns}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def project_lineage_id(resolved_cwd: Path) -> str:
    """An opaque, stable identifier for one project directory. Never
    reversible to the raw path from the ledger alone; the same resolved
    `cwd` always yields the same lineage ID, enabling distinct-lineage
    counting and future Stage B grouping without ever storing the path."""
    digest = hashlib.sha256(str(resolved_cwd).encode("utf-8")).hexdigest()
    return f"lineage:{digest[:16]}"


@dataclass(frozen=True, slots=True)
class SourceRoot:
    """One authorized, resolved source root, identified only by an opaque
    label outside this module -- the real path never appears in a ledger."""

    label: str
    path: Path

    @classmethod
    def resolve(cls, label: str, raw_path: Path) -> SourceRoot:
        return cls(label=label, path=raw_path.expanduser().resolve())


#: The two source roots authorized in H-53. Fixed as a module constant --
#: never a CLI flag or config value, so no invocation can silently
#: broaden Stage A's data-authority boundary.
AUTHORIZED_ROOTS: tuple[SourceRoot, ...] = (
    SourceRoot.resolve("root_a", Path("~/WorkSpace/AetherForge")),
    SourceRoot.resolve("root_b", Path("~/WorkSpace/Retailogists/GitHub")),
)


def containing_root(
    cwd: Path, roots: tuple[SourceRoot, ...] = AUTHORIZED_ROOTS
) -> SourceRoot | None:
    """Return the authorized root containing ``cwd``, or ``None`` if
    ``cwd`` (resolved) is not one of the roots and not nested under one."""
    resolved = cwd.expanduser().resolve()
    for root in roots:
        if resolved == root.path or resolved.is_relative_to(root.path):
            return root
    return None


@dataclass(frozen=True, slots=True)
class FileMeta:
    """Filesystem metadata for one admitted candidate file, gathered
    without reading its content."""

    size_bytes: int
    mtime_ns: int
    age_seconds: float


def stat_admission(path: Path, *, now: float) -> FileMeta | ExclusionReason:
    """Apply the symlink -> regular-file -> active-threshold admission
    checks, in that order, using filesystem metadata only. Returns the
    gathered `FileMeta` on success, or the `ExclusionReason` that fired.

    This is only the metadata-only portion of the admission gate; the
    remaining allowlist-containment check requires a `cwd` value a
    provider adapter extracts separately (design doc §§ 4-5) and is
    applied by the caller via `containing_root`.
    """
    if path.is_symlink():
        return ExclusionReason.SYMLINK
    try:
        stat_result = path.stat()
    except OSError:
        return ExclusionReason.NOT_A_REGULAR_FILE
    if not path.is_file():
        return ExclusionReason.NOT_A_REGULAR_FILE
    age_seconds = now - stat_result.st_mtime
    if age_seconds < STAGE_A_ACTIVE_THRESHOLD_SECONDS:
        return ExclusionReason.ACTIVE
    return FileMeta(
        size_bytes=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        age_seconds=age_seconds,
    )


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One admitted session file's body-free ledger row."""

    provider: Provider
    session_id: str
    project_lineage_id: str
    closure_status: ClosureStatus
    size_band: SizeBand
    age_band: AgeBand
    provenance_hash: str

    def to_json(self) -> dict[str, str]:
        return {
            "provider": self.provider.value,
            "session_id": self.session_id,
            "project_lineage_id": self.project_lineage_id,
            "closure_status": self.closure_status.value,
            "size_band": self.size_band.value,
            "age_band": self.age_band.value,
            "provenance_hash": self.provenance_hash,
        }


def build_session_record(
    *,
    provider: Provider,
    session_id: str,
    resolved_cwd: Path,
    file_path: Path,
    file_meta: FileMeta,
) -> SessionRecord:
    """Build a `SessionRecord` for a file that has already passed every
    admission check (`stat_admission` succeeded and `containing_root`
    returned a root for ``resolved_cwd``). Callers must perform both
    checks first; this function does not repeat them."""
    return SessionRecord(
        provider=provider,
        session_id=session_id,
        project_lineage_id=project_lineage_id(resolved_cwd),
        closure_status=ClosureStatus.CLOSED,
        size_band=size_band(file_meta.size_bytes),
        age_band=age_band(file_meta.age_seconds),
        provenance_hash=provenance_hash(
            provider=provider,
            file_path=file_path,
            size_bytes=file_meta.size_bytes,
            mtime_ns=file_meta.mtime_ns,
        ),
    )
