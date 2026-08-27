"""Tests for `laconic.k1corpus.providers`. Every fixture is synthetic and
`tmp_path`-scoped; no test reads a real `~/.claude`, `~/.codex`, or
`~/.omp` path."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from laconic.k1corpus.providers import (
    admit_file,
    discover_session_files,
    enumerate_provider,
    extract_session_id,
    scan_cwd,
)
from laconic.k1corpus.stage_a import (
    STAGE_A_ACTIVE_THRESHOLD_SECONDS,
    ExclusionReason,
    Provider,
    SessionRecord,
    SourceRoot,
)

_UUID = "41380cc3-ebf5-45c2-b0c6-c5f071f7a319"


def _write_jsonl(path: Path, lines: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def _age_file(path: Path, *, seconds_old: float) -> None:
    mtime = time.time() - seconds_old
    os.utime(path, (mtime, mtime))


# --- extract_session_id ---------------------------------------------------


def test_extract_session_id_claude_code_uses_filename_stem(tmp_path: Path) -> None:
    path = tmp_path / f"{_UUID}.jsonl"
    assert extract_session_id(Provider.CLAUDE_CODE, path) == f"claude-code:{_UUID}"


def test_extract_session_id_claude_code_rejects_non_uuid_stem(tmp_path: Path) -> None:
    path = tmp_path / "not-a-uuid.jsonl"
    assert extract_session_id(Provider.CLAUDE_CODE, path) is None


def test_extract_session_id_codex_extracts_trailing_uuid(tmp_path: Path) -> None:
    path = tmp_path / f"rollout-2025-11-17T10-36-32-{_UUID}.jsonl"
    assert extract_session_id(Provider.CODEX, path) == f"codex:{_UUID}"


def test_extract_session_id_codex_rejects_missing_uuid(tmp_path: Path) -> None:
    path = tmp_path / "rollout-2025-11-17T10-36-32.jsonl"
    assert extract_session_id(Provider.CODEX, path) is None


def test_extract_session_id_omp_extracts_uuid_after_underscore(tmp_path: Path) -> None:
    path = tmp_path / f"2026-07-26T17-41-03-194Z_{_UUID}.jsonl"
    assert extract_session_id(Provider.OMP, path) == f"omp:{_UUID}"


def test_extract_session_id_omp_rejects_malformed_name(tmp_path: Path) -> None:
    path = tmp_path / "no-underscore-uuid.jsonl"
    assert extract_session_id(Provider.OMP, path) is None


# --- scan_cwd / bounded, allowlisted content read -------------------------


def test_scan_cwd_finds_top_level_cwd_key(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    _write_jsonl(
        path,
        [
            {"type": "mode", "sessionId": "x"},
            {"type": "session", "cwd": "/Users/druk/WorkSpace/AetherForge/laconic", "id": "x"},
        ],
    )
    assert scan_cwd(path) == "/Users/druk/WorkSpace/AetherForge/laconic"


def test_scan_cwd_finds_nested_payload_cwd_key(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "session_meta",
                "payload": {
                    "cwd": "/Users/druk/WorkSpace/AetherForge/laconic",
                    "instructions": "SENTINEL_FREE_TEXT_MUST_NOT_LEAK",
                },
            }
        ],
    )
    assert scan_cwd(path) == "/Users/druk/WorkSpace/AetherForge/laconic"


def test_scan_cwd_never_returns_title_or_instructions_value(tmp_path: Path) -> None:
    path = tmp_path / "no_cwd.jsonl"
    _write_jsonl(
        path,
        [
            {"type": "title", "title": "SENTINEL_TITLE_MUST_NOT_LEAK"},
            {"type": "session_meta", "payload": {"instructions": "SENTINEL_INSTRUCTIONS"}},
        ],
    )
    assert scan_cwd(path) is None


def test_scan_cwd_enforces_line_bound(tmp_path: Path) -> None:
    path = tmp_path / "deep.jsonl"
    lines: list[dict[str, object]] = [{"type": "noop", "n": i} for i in range(10)]
    lines.append({"type": "session", "cwd": "/Users/druk/WorkSpace/AetherForge/laconic"})
    _write_jsonl(path, lines)
    assert scan_cwd(path, line_bound=5) is None
    assert scan_cwd(path, line_bound=20) == "/Users/druk/WorkSpace/AetherForge/laconic"


def test_scan_cwd_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "malformed.jsonl"
    path.write_text(
        "not json at all\n"
        + json.dumps({"cwd": "/Users/druk/WorkSpace/AetherForge/laconic"})
        + "\n",
        encoding="utf-8",
    )
    assert scan_cwd(path) == "/Users/druk/WorkSpace/AetherForge/laconic"


def test_scan_cwd_raises_oserror_for_missing_file(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(OSError):
        scan_cwd(tmp_path / "missing.jsonl")


# --- discover_session_files ------------------------------------------------


def test_discover_session_files_finds_nested_jsonl(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project_dir = home / ".claude" / "projects" / "-Users-druk-WorkSpace-AetherForge-laconic"
    _write_jsonl(project_dir / f"{_UUID}.jsonl", [{"type": "x"}])
    (project_dir / "memory").mkdir(parents=True, exist_ok=True)
    found = list(discover_session_files(Provider.CLAUDE_CODE, home=home))
    assert len(found) == 1
    assert found[0].name == f"{_UUID}.jsonl"


def test_discover_session_files_returns_empty_for_missing_root(tmp_path: Path) -> None:
    home = tmp_path / "empty-home"
    found = list(discover_session_files(Provider.CODEX, home=home))
    assert found == []


# --- admit_file end-to-end pipeline ----------------------------------------


def _authorized_root(tmp_path: Path) -> SourceRoot:
    root_path = tmp_path / "AetherForge"
    root_path.mkdir(parents=True, exist_ok=True)
    return SourceRoot.resolve("root_a", root_path)


def test_admit_file_admits_closed_in_scope_session(tmp_path: Path) -> None:
    root = _authorized_root(tmp_path)
    project = root.path / "laconic"
    file_path = project / f"{_UUID}.jsonl"
    _write_jsonl(file_path, [{"type": "session", "cwd": str(project)}])
    _age_file(file_path, seconds_old=STAGE_A_ACTIVE_THRESHOLD_SECONDS + 60)
    result = admit_file(Provider.CLAUDE_CODE, file_path, now=time.time(), roots=(root,))
    assert isinstance(result, SessionRecord)
    assert result.provider is Provider.CLAUDE_CODE
    assert result.session_id == f"claude-code:{_UUID}"


def test_admit_file_rejects_active_session(tmp_path: Path) -> None:
    root = _authorized_root(tmp_path)
    project = root.path / "laconic"
    file_path = project / f"{_UUID}.jsonl"
    _write_jsonl(file_path, [{"type": "session", "cwd": str(project)}])
    result = admit_file(Provider.CLAUDE_CODE, file_path, now=time.time(), roots=(root,))
    assert result is ExclusionReason.ACTIVE


def test_admit_file_rejects_cwd_outside_allowlist(tmp_path: Path) -> None:
    root = _authorized_root(tmp_path)
    outside = tmp_path / "Retailogists" / "Aldo-BitBucket" / "some-repo"
    outside.mkdir(parents=True)
    file_path = tmp_path / "orphan" / f"{_UUID}.jsonl"
    _write_jsonl(file_path, [{"type": "session", "cwd": str(outside)}])
    _age_file(file_path, seconds_old=STAGE_A_ACTIVE_THRESHOLD_SECONDS + 60)
    result = admit_file(Provider.CLAUDE_CODE, file_path, now=time.time(), roots=(root,))
    assert result is ExclusionReason.OUTSIDE_ALLOWLIST


def test_admit_file_rejects_symlink(tmp_path: Path) -> None:
    root = _authorized_root(tmp_path)
    project = root.path / "laconic"
    real = project / "real.jsonl"
    _write_jsonl(real, [{"type": "session", "cwd": str(project)}])
    _age_file(real, seconds_old=STAGE_A_ACTIVE_THRESHOLD_SECONDS + 60)
    link = project / f"{_UUID}.jsonl"
    link.symlink_to(real)
    result = admit_file(Provider.CLAUDE_CODE, link, now=time.time(), roots=(root,))
    assert result is ExclusionReason.SYMLINK


def test_admit_file_rejects_cwd_not_found(tmp_path: Path) -> None:
    root = _authorized_root(tmp_path)
    file_path = root.path / "laconic" / f"{_UUID}.jsonl"
    _write_jsonl(file_path, [{"type": "mode", "value": "plan"}])
    _age_file(file_path, seconds_old=STAGE_A_ACTIVE_THRESHOLD_SECONDS + 60)
    result = admit_file(Provider.CLAUDE_CODE, file_path, now=time.time(), roots=(root,))
    assert result is ExclusionReason.CWD_NOT_FOUND


def test_admit_file_rejects_unparseable_filename(tmp_path: Path) -> None:
    root = _authorized_root(tmp_path)
    project = root.path / "laconic"
    file_path = project / "not-a-recognized-name.jsonl"
    _write_jsonl(file_path, [{"type": "session", "cwd": str(project)}])
    _age_file(file_path, seconds_old=STAGE_A_ACTIVE_THRESHOLD_SECONDS + 60)
    result = admit_file(Provider.CLAUDE_CODE, file_path, now=time.time(), roots=(root,))
    assert result is ExclusionReason.UNPARSEABLE


# --- enumerate_provider: counts every exclusion, drops nothing silently ---


def test_enumerate_provider_counts_admissions_and_exclusions(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = SourceRoot.resolve("root_a", tmp_path / "AetherForge")
    root.path.mkdir(parents=True, exist_ok=True)
    project = root.path / "laconic"

    admitted_path = home / ".claude" / "projects" / "p1" / f"{_UUID}.jsonl"
    _write_jsonl(admitted_path, [{"type": "session", "cwd": str(project)}])
    _age_file(admitted_path, seconds_old=STAGE_A_ACTIVE_THRESHOLD_SECONDS + 60)

    excluded_uuid = "52481dd4-fca6-56b3-c1d7-d8e082e830fc"
    excluded_path = home / ".claude" / "projects" / "p2" / f"{excluded_uuid}.jsonl"
    _write_jsonl(excluded_path, [{"type": "session", "cwd": "/etc/somewhere"}])
    _age_file(excluded_path, seconds_old=STAGE_A_ACTIVE_THRESHOLD_SECONDS + 60)

    records, exclusions = enumerate_provider(
        Provider.CLAUDE_CODE, home=home, now=time.time(), roots=(root,)
    )
    assert len(records) == 1
    assert records[0].session_id == f"claude-code:{_UUID}"
    assert exclusions[ExclusionReason.OUTSIDE_ALLOWLIST] == 1
    assert sum(exclusions.values()) == 1
