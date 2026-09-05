"""Content-free status and explicit retention controls for runtime storage."""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from laconic.runtime.storage import (
    UnsafeStoragePathError,
    resolve_data_dir,
    session_ledger_path,
)

_LEDGER_NAME = re.compile(r"[0-9a-f]{64}\.sqlite3")
_DURATION = re.compile(r"([1-9][0-9]*)([smhdw])")
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


@dataclass(frozen=True, slots=True)
class RuntimeStorageStatus:
    root: Path
    exists: bool
    storage_bytes: int
    sessions: int
    eligible_observations: int
    compressed_observations: int
    pass_through_observations: int
    raw_chars: int
    visible_chars: int
    full_expansions: int
    span_expansions: int


@dataclass(frozen=True, slots=True)
class PurgePlan:
    root: Path
    selector: str
    targets: tuple[Path, ...]
    reclaim_bytes: int


@dataclass(frozen=True, slots=True)
class PurgeResult:
    plan: PurgePlan
    deleted_sessions: int
    deleted_files: int


def parse_duration(value: str) -> int:
    """Parse an explicit positive retention duration into seconds."""
    match = _DURATION.fullmatch(value)
    if match is None:
        raise ValueError("duration must be a positive integer followed by s, m, h, d, or w")
    amount = int(match.group(1))
    return amount * _DURATION_SECONDS[match.group(2)]


def _storage_root(data_dir: Path | None) -> Path:
    candidate = data_dir or resolve_data_dir()
    expanded = candidate.expanduser()
    if expanded.is_symlink():
        raise UnsafeStoragePathError(f"runtime storage root must not be a symlink: {expanded}")
    root = resolve_data_dir(expanded)
    if root.exists() and not root.is_dir():
        raise UnsafeStoragePathError(f"runtime storage is not a directory: {root}")
    return root


def _sessions_directory(root: Path) -> Path:
    sessions = root / "sessions"
    if sessions.is_symlink():
        raise UnsafeStoragePathError(
            f"runtime sessions directory must not be a symlink: {sessions}"
        )
    if sessions.exists() and not sessions.is_dir():
        raise UnsafeStoragePathError(f"runtime sessions path is not a directory: {sessions}")
    return sessions


def _owned_ledger_files(root: Path) -> tuple[Path, ...]:
    sessions = _sessions_directory(root)
    if not sessions.exists():
        return ()
    resolved_sessions = sessions.resolve(strict=True)
    ledgers: list[Path] = []
    for path in sorted(sessions.iterdir()):
        if _LEDGER_NAME.fullmatch(path.name) is None:
            continue
        if path.is_symlink() or not path.is_file():
            raise UnsafeStoragePathError(f"runtime ledger is not an ordinary file: {path}")
        if path.parent.resolve(strict=True) != resolved_sessions:
            raise UnsafeStoragePathError(f"runtime ledger escaped storage root: {path}")
        ledgers.append(path)
    return tuple(ledgers)


def _sidecar_paths(ledger: Path) -> tuple[Path, Path]:
    return Path(f"{ledger}-wal"), Path(f"{ledger}-shm")


def _file_bytes(path: Path) -> int:
    if path.is_symlink():
        raise UnsafeStoragePathError(f"runtime storage entry is not an ordinary file: {path}")
    if not path.exists():
        return 0
    if not path.is_file():
        raise UnsafeStoragePathError(f"runtime storage entry is not an ordinary file: {path}")
    return path.stat().st_size


def _read_metrics(path: Path) -> tuple[int, int, int, int, int, int, int]:
    uri = f"{path.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as database:
        database.execute("PRAGMA query_only = ON")
        decisions = database.execute(
            "SELECT count(*), "
            "coalesce(sum(CASE WHEN outcome = 'emitted' THEN 1 ELSE 0 END), 0), "
            "coalesce(sum(raw_chars), 0), coalesce(sum(visible_chars), 0) "
            "FROM runtime_decisions"
        ).fetchone()
        expansions = database.execute(
            "SELECT coalesce(sum(CASE WHEN span = 0 THEN 1 ELSE 0 END), 0), "
            "coalesce(sum(CASE WHEN span = 1 THEN 1 ELSE 0 END), 0) "
            "FROM runtime_expansions"
        ).fetchone()
    if decisions is None or expansions is None:
        raise sqlite3.DatabaseError("runtime metric query returned no aggregate row")
    eligible = int(decisions[0])
    compressed = int(decisions[1])
    return (
        eligible,
        compressed,
        eligible - compressed,
        int(decisions[2]),
        int(decisions[3]),
        int(expansions[0]),
        int(expansions[1]),
    )


def runtime_storage_status(data_dir: Path | None = None) -> RuntimeStorageStatus:
    """Aggregate persisted counters without reading raw observation columns."""
    root = _storage_root(data_dir)
    ledgers = _owned_ledger_files(root)
    totals = [0] * 7
    storage_bytes = 0
    for ledger in ledgers:
        metrics = _read_metrics(ledger)
        totals = [current + value for current, value in zip(totals, metrics, strict=True)]
        storage_bytes += _file_bytes(ledger)
        storage_bytes += sum(_file_bytes(sidecar) for sidecar in _sidecar_paths(ledger))
    return RuntimeStorageStatus(
        root=root,
        exists=root.exists(),
        storage_bytes=storage_bytes,
        sessions=len(ledgers),
        eligible_observations=totals[0],
        compressed_observations=totals[1],
        pass_through_observations=totals[2],
        raw_chars=totals[3],
        visible_chars=totals[4],
        full_expansions=totals[5],
        span_expansions=totals[6],
    )


def _target_bytes(targets: tuple[Path, ...]) -> int:
    return sum(
        _file_bytes(path) + sum(_file_bytes(sidecar) for sidecar in _sidecar_paths(path))
        for path in targets
    )


def preview_purge_session(session_id: str, data_dir: Path | None = None) -> PurgePlan:
    """Preview deleting exactly one opaque session ledger and its SQLite sidecars."""
    root = _storage_root(data_dir)
    _sessions_directory(root)
    target = session_ledger_path(session_id, root)
    if target.is_symlink():
        raise UnsafeStoragePathError(f"runtime ledger is not an ordinary file: {target}")
    targets = (target,) if target.exists() else ()
    if targets and (target.is_symlink() or not target.is_file()):
        raise UnsafeStoragePathError(f"runtime ledger is not an ordinary file: {target}")
    return PurgePlan(
        root=root,
        selector=f"session={session_id}",
        targets=targets,
        reclaim_bytes=_target_bytes(targets),
    )


def _last_activity(path: Path) -> float:
    uri = f"{path.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as database:
        database.execute("PRAGMA query_only = ON")
        row = database.execute(
            "SELECT max(created_at) FROM ("
            "SELECT created_at FROM observations UNION ALL "
            "SELECT created_at FROM runtime_decisions UNION ALL "
            "SELECT created_at FROM runtime_expansions)"
        ).fetchone()
    if row is None or row[0] is None:
        return path.stat().st_mtime
    return float(row[0])


def preview_purge_older_than(
    older_than_seconds: int,
    data_dir: Path | None = None,
    *,
    now: float | None = None,
    selector: str | None = None,
) -> PurgePlan:
    """Preview deleting whole ledgers with no activity inside the retention window."""
    if older_than_seconds < 1:
        raise ValueError("older-than duration must be positive")
    root = _storage_root(data_dir)
    cutoff = (time.time() if now is None else now) - older_than_seconds
    targets = tuple(path for path in _owned_ledger_files(root) if _last_activity(path) < cutoff)
    return PurgePlan(
        root=root,
        selector=selector or f"older-than={older_than_seconds}s",
        targets=targets,
        reclaim_bytes=_target_bytes(targets),
    )


def apply_purge(plan: PurgePlan) -> PurgeResult:
    """Delete only prevalidated ledger paths contained by the plan's runtime root."""
    sessions = _sessions_directory(plan.root)
    resolved_sessions = sessions.resolve(strict=True) if sessions.exists() else None
    paths: list[Path] = []
    deleted_sessions = 0
    for ledger in plan.targets:
        if ledger.exists() or ledger.is_symlink():
            deleted_sessions += int(ledger.exists() and not ledger.is_symlink())
            paths.append(ledger)
        paths.extend(
            sidecar
            for sidecar in _sidecar_paths(ledger)
            if sidecar.exists() or sidecar.is_symlink()
        )
    for path in paths:
        if (
            resolved_sessions is None
            or path.parent.resolve(strict=True) != resolved_sessions
            or path.is_symlink()
            or not path.is_file()
        ):
            raise UnsafeStoragePathError(f"refusing to purge unsafe runtime path: {path}")
    for path in paths:
        path.unlink()
    return PurgeResult(plan=plan, deleted_sessions=deleted_sessions, deleted_files=len(paths))
