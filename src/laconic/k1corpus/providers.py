"""Stage A provider adapters: real session-file discovery for Claude
Code, Codex, and OMP, plus the bounded, key-allowlisted `cwd` extraction
described in `.docs/K1_STAGE_A_DESIGN.md` §§ 3-4.

Every JSONL line is parsed once (necessarily loading every key present
on that line into memory transiently, the same allowlist-then-discard
pattern `laconic.observe.receipt` already uses for hook payloads) and
only ever consulted for its `cwd` key -- or `payload.cwd` for Codex's
nested `session_meta` envelope. No other key -- including known
free-text fields such as Codex's `payload.instructions` or OMP's
`title`/`titleSource` -- is ever read, copied, returned, or stored by
this module; the parsed object goes out of scope immediately after the
single-key check.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Iterator
from itertools import islice
from pathlib import Path
from typing import Any

from laconic.k1corpus.stage_a import (
    AUTHORIZED_ROOTS,
    STAGE_A_SCAN_LINE_BOUND,
    ExclusionReason,
    Provider,
    SessionRecord,
    SourceRoot,
    build_session_record,
    containing_root,
    stat_admission,
)

#: Per-provider real session-storage root, joined onto `$HOME`.
_PROVIDER_ROOTS: dict[Provider, tuple[str, ...]] = {
    Provider.CLAUDE_CODE: (".claude", "projects"),
    Provider.CODEX: (".codex", "sessions"),
    Provider.OMP: (".omp", "agent", "sessions"),
}

_UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_CLAUDE_CODE_STEM_RE = re.compile(_UUID_PATTERN)
_TRAILING_UUID_RE = re.compile(rf"({_UUID_PATTERN})$")


def provider_root(provider: Provider, *, home: Path | None = None) -> Path:
    """Return the real storage root for ``provider`` under ``home``
    (default: the real `Path.home()`)."""
    base = home if home is not None else Path.home()
    return base.joinpath(*_PROVIDER_ROOTS[provider])


def discover_session_files(provider: Provider, *, home: Path | None = None) -> Iterator[Path]:
    """Yield every `*.jsonl` file under ``provider``'s real storage root,
    recursively, in a stable sorted order. Yields nothing if the root
    does not exist. Performs no admission, association, or content
    check -- purely a file listing."""
    root = provider_root(provider, home=home)
    if not root.is_dir():
        return
    yield from sorted(root.rglob("*.jsonl"))


def extract_session_id(provider: Provider, file_path: Path) -> str | None:
    """Extract an opaque session identifier from ``file_path``'s name
    alone (no content read). Returns ``None`` if the filename does not
    match the provider's expected shape -- callers must treat that as
    `ExclusionReason.UNPARSEABLE`, never fabricate an identifier."""
    stem = file_path.stem
    match provider:
        case Provider.CLAUDE_CODE:
            return f"claude-code:{stem}" if _CLAUDE_CODE_STEM_RE.fullmatch(stem) else None
        case Provider.CODEX:
            match_obj = _TRAILING_UUID_RE.search(stem)
            return f"codex:{match_obj.group(1)}" if match_obj else None
        case Provider.OMP:
            _, _, tail = stem.rpartition("_")
            return f"omp:{tail}" if _CLAUDE_CODE_STEM_RE.fullmatch(tail) else None


def _extract_cwd_from_object(obj: dict[str, Any]) -> str | None:
    """Return ``obj["cwd"]`` or ``obj["payload"]["cwd"]`` if present and a
    non-empty string. Every other key of ``obj`` -- and of a nested
    `payload` dict -- is never read here."""
    cwd = obj.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    payload = obj.get("payload")
    if isinstance(payload, dict):
        nested = payload.get("cwd")
        if isinstance(nested, str) and nested:
            return nested
    return None


def scan_cwd(file_path: Path, *, line_bound: int = STAGE_A_SCAN_LINE_BOUND) -> str | None:
    """Read at most ``line_bound`` leading lines of ``file_path``, parse
    each as JSON, and return the first `cwd` value found via
    `_extract_cwd_from_object`, stopping immediately once found. Returns
    ``None`` if no line within the bound carries one. Raises `OSError`
    if the file cannot be opened -- callers treat that as
    `ExclusionReason.UNPARSEABLE`, distinct from "no cwd found"."""
    with file_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in islice(handle, line_bound):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            cwd = _extract_cwd_from_object(obj)
            if cwd is not None:
                return cwd
    return None


def admit_file(
    provider: Provider,
    file_path: Path,
    *,
    now: float,
    roots: tuple[SourceRoot, ...] = AUTHORIZED_ROOTS,
) -> SessionRecord | ExclusionReason:
    """Run one candidate file through the full Stage A admission
    pipeline -- filesystem checks, session-ID extraction, the bounded
    `cwd` scan, and allowlist containment -- in that order. Returns a
    `SessionRecord` on success, or the `ExclusionReason` that fired."""
    meta = stat_admission(file_path, now=now)
    if isinstance(meta, ExclusionReason):
        return meta
    session_id = extract_session_id(provider, file_path)
    if session_id is None:
        return ExclusionReason.UNPARSEABLE
    try:
        cwd = scan_cwd(file_path)
    except OSError:
        return ExclusionReason.UNPARSEABLE
    if cwd is None:
        return ExclusionReason.CWD_NOT_FOUND
    cwd_path = Path(cwd)
    if not cwd_path.is_absolute():
        return ExclusionReason.CWD_NOT_FOUND
    root = containing_root(cwd_path, roots)
    if root is None:
        return ExclusionReason.OUTSIDE_ALLOWLIST
    return build_session_record(
        provider=provider,
        session_id=session_id,
        resolved_cwd=cwd_path.expanduser().resolve(),
        file_path=file_path,
        file_meta=meta,
    )


def enumerate_provider(
    provider: Provider,
    *,
    home: Path | None = None,
    now: float | None = None,
    roots: tuple[SourceRoot, ...] = AUTHORIZED_ROOTS,
) -> tuple[list[SessionRecord], Counter[ExclusionReason]]:
    """Discover every session file for ``provider`` under its real (or
    ``home``-relative test) storage root and admit each one. Returns the
    admitted records and a counter of every exclusion reason -- every
    rejected file is counted, never silently dropped."""
    scan_time = now if now is not None else time.time()
    records: list[SessionRecord] = []
    exclusions: Counter[ExclusionReason] = Counter()
    for file_path in discover_session_files(provider, home=home):
        result = admit_file(provider, file_path, now=scan_time, roots=roots)
        if isinstance(result, SessionRecord):
            records.append(result)
        else:
            exclusions[result] += 1
    return records, exclusions
