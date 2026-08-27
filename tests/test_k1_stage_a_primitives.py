"""Tests for `laconic.k1corpus.stage_a`: schema, allowlist containment,
and the file-admission gate. No test in this module touches a real
`~/.claude`, `~/.codex`, or `~/.omp` path -- every root and file is
synthetic and `tmp_path`-scoped."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from laconic.k1corpus.stage_a import (
    STAGE_A_ACTIVE_THRESHOLD_SECONDS,
    AgeBand,
    ClosureStatus,
    ExclusionReason,
    FileMeta,
    Provider,
    SizeBand,
    SourceRoot,
    age_band,
    build_session_record,
    containing_root,
    project_lineage_id,
    provenance_hash,
    size_band,
    stat_admission,
)


def _root(tmp_path: Path, name: str) -> SourceRoot:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return SourceRoot.resolve(f"root_{name}", path)


# --- size_band boundaries ---------------------------------------------


@pytest.mark.parametrize(
    ("size_bytes", "expected"),
    [
        (0, SizeBand.XS),
        (10 * 1024 - 1, SizeBand.XS),
        (10 * 1024, SizeBand.S),
        (100 * 1024 - 1, SizeBand.S),
        (100 * 1024, SizeBand.M),
        (1024 * 1024 - 1, SizeBand.M),
        (1024 * 1024, SizeBand.L),
        (10 * 1024 * 1024 - 1, SizeBand.L),
        (10 * 1024 * 1024, SizeBand.XL),
        (10 * 1024 * 1024 * 10, SizeBand.XL),
    ],
)
def test_size_band_boundaries(size_bytes: int, expected: SizeBand) -> None:
    assert size_band(size_bytes) is expected


# --- age_band boundaries ------------------------------------------------


@pytest.mark.parametrize(
    ("age_seconds", "expected"),
    [
        (0, AgeBand.LT_1D),
        (86400 - 1, AgeBand.LT_1D),
        (86400, AgeBand.D1_7),
        (7 * 86400 - 1, AgeBand.D1_7),
        (7 * 86400, AgeBand.D7_30),
        (30 * 86400 - 1, AgeBand.D7_30),
        (30 * 86400, AgeBand.D30_90),
        (90 * 86400 - 1, AgeBand.D30_90),
        (90 * 86400, AgeBand.GT_90D),
        (365 * 86400, AgeBand.GT_90D),
    ],
)
def test_age_band_boundaries(age_seconds: float, expected: AgeBand) -> None:
    assert age_band(age_seconds) is expected


# --- provenance_hash / project_lineage_id --------------------------------


def test_provenance_hash_is_deterministic_and_content_free(tmp_path: Path) -> None:
    file_path = tmp_path / "a" / "session.jsonl"
    first = provenance_hash(provider=Provider.CODEX, file_path=file_path, size_bytes=10, mtime_ns=1)
    second = provenance_hash(
        provider=Provider.CODEX, file_path=file_path, size_bytes=10, mtime_ns=1
    )
    assert first == second
    assert len(first) == 64  # sha256 hex digest


def test_provenance_hash_changes_with_identity_metadata(tmp_path: Path) -> None:
    file_path = tmp_path / "a" / "session.jsonl"
    baseline = provenance_hash(
        provider=Provider.CODEX, file_path=file_path, size_bytes=10, mtime_ns=1
    )
    different_size = provenance_hash(
        provider=Provider.CODEX, file_path=file_path, size_bytes=11, mtime_ns=1
    )
    different_mtime = provenance_hash(
        provider=Provider.CODEX, file_path=file_path, size_bytes=10, mtime_ns=2
    )
    different_provider = provenance_hash(
        provider=Provider.OMP, file_path=file_path, size_bytes=10, mtime_ns=1
    )
    assert len({baseline, different_size, different_mtime, different_provider}) == 4


def test_project_lineage_id_is_opaque_and_stable(tmp_path: Path) -> None:
    project_a = tmp_path / "AetherForge" / "laconic"
    project_b = tmp_path / "AetherForge" / "archex"
    id_a_first = project_lineage_id(project_a)
    id_a_second = project_lineage_id(project_a)
    id_b = project_lineage_id(project_b)
    assert id_a_first == id_a_second
    assert id_a_first != id_b
    assert str(project_a) not in id_a_first
    assert id_a_first.startswith("lineage:")


# --- containing_root / allowlist containment -----------------------------


def test_containing_root_matches_root_itself(tmp_path: Path) -> None:
    root = _root(tmp_path, "AetherForge")
    assert containing_root(root.path, (root,)) == root


def test_containing_root_matches_nested_project(tmp_path: Path) -> None:
    root = _root(tmp_path, "AetherForge")
    project = root.path / "laconic"
    project.mkdir()
    assert containing_root(project, (root,)) == root


def test_containing_root_rejects_sibling_name_collision(tmp_path: Path) -> None:
    root = _root(tmp_path, "AetherForge")
    sibling = tmp_path / "AetherForgeX"
    sibling.mkdir()
    assert containing_root(sibling, (root,)) is None


def test_containing_root_rejects_unrelated_path(tmp_path: Path) -> None:
    root = _root(tmp_path, "AetherForge")
    unrelated = tmp_path / "Retailogists" / "Aldo-BitBucket" / "some-repo"
    unrelated.mkdir(parents=True)
    assert containing_root(unrelated, (root,)) is None


def test_containing_root_checks_every_configured_root(tmp_path: Path) -> None:
    root_a = _root(tmp_path, "AetherForge")
    root_b = _root(tmp_path, "GitHub")
    project = root_b.path / "armory"
    project.mkdir()
    assert containing_root(project, (root_a, root_b)) == root_b


# --- stat_admission: symlink / regular-file / active ---------------------


def test_stat_admission_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real.jsonl"
    target.write_text("{}\n")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    result = stat_admission(link, now=time.time())
    assert result is ExclusionReason.SYMLINK


def test_stat_admission_rejects_directory(tmp_path: Path) -> None:
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    result = stat_admission(directory, now=time.time())
    assert result is ExclusionReason.NOT_A_REGULAR_FILE


def test_stat_admission_rejects_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.jsonl"
    result = stat_admission(missing, now=time.time())
    assert result is ExclusionReason.NOT_A_REGULAR_FILE


def test_stat_admission_rejects_file_one_second_inside_active_threshold(tmp_path: Path) -> None:
    path = tmp_path / "recent.jsonl"
    path.write_text("{}\n")
    now = time.time()
    mtime = now - (STAGE_A_ACTIVE_THRESHOLD_SECONDS - 1)
    os.utime(path, (mtime, mtime))
    result = stat_admission(path, now=now)
    assert result is ExclusionReason.ACTIVE


def test_stat_admission_admits_file_one_second_past_active_threshold(tmp_path: Path) -> None:
    path = tmp_path / "closed.jsonl"
    path.write_text("{}\n")
    now = time.time()
    mtime = now - (STAGE_A_ACTIVE_THRESHOLD_SECONDS + 1)
    os.utime(path, (mtime, mtime))
    result = stat_admission(path, now=now)
    assert isinstance(result, FileMeta)
    assert result.age_seconds > STAGE_A_ACTIVE_THRESHOLD_SECONDS


def test_stat_admission_admits_at_exact_threshold_boundary_as_active(tmp_path: Path) -> None:
    # age_seconds < threshold excludes; age_seconds == threshold does not.
    path = tmp_path / "boundary.jsonl"
    path.write_text("{}\n")
    now = time.time()
    mtime = now - STAGE_A_ACTIVE_THRESHOLD_SECONDS
    os.utime(path, (mtime, mtime))
    result = stat_admission(path, now=now)
    assert isinstance(result, FileMeta)


# --- build_session_record -------------------------------------------------


def test_build_session_record_never_emits_active_closure_status(tmp_path: Path) -> None:
    file_path = tmp_path / "session.jsonl"
    meta = FileMeta(size_bytes=42, mtime_ns=123, age_seconds=100_000)
    record = build_session_record(
        provider=Provider.CLAUDE_CODE,
        session_id="claude-code:abc-123",
        resolved_cwd=tmp_path / "AetherForge" / "laconic",
        file_path=file_path,
        file_meta=meta,
    )
    assert record.closure_status is ClosureStatus.CLOSED
    payload = record.to_json()
    assert payload["closure_status"] == "closed"
    assert "path" not in payload
    assert str(tmp_path) not in payload["project_lineage_id"]


def test_build_session_record_bands_match_input_metadata(tmp_path: Path) -> None:
    file_path = tmp_path / "session.jsonl"
    meta = FileMeta(size_bytes=50 * 1024, mtime_ns=1, age_seconds=2 * 86400)
    record = build_session_record(
        provider=Provider.OMP,
        session_id="omp:xyz",
        resolved_cwd=tmp_path / "AetherForge" / "laconic",
        file_path=file_path,
        file_meta=meta,
    )
    assert record.size_band is SizeBand.S
    assert record.age_band is AgeBand.D1_7
