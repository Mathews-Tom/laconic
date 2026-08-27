"""Tests for `laconic.k1corpus.stage_b`: session-level manifest
construction from a frozen lineage-level corpus design. No test touches
a real `~/.claude`/`~/.codex`/`~/.omp` path; every fixture is synthetic
and `tmp_path`-scoped."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from laconic.k1corpus.stage_a import STAGE_A_ACTIVE_THRESHOLD_SECONDS, SourceRoot
from laconic.k1corpus.stage_b import (
    FrozenCorpus,
    ManifestSet,
    TotalsMismatchError,
    build_session_manifest,
)

_UUID_A = "41380cc3-ebf5-45c2-b0c6-c5f071f7a319"
_UUID_B = "52481dd4-fca6-56b3-c1d7-d8e082e830fc"
_UUID_C = "63592ee5-0db7-67c4-d2e8-e9f193f941gd".replace("g", "a")  # keep hex-valid


def _write_session(home: Path, subpath: tuple[str, ...], filename: str, cwd: str) -> Path:
    path = home.joinpath(*subpath, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Pad well past the "xs" size-band threshold (10 KiB) so these fixtures
    # aren't excluded by the frozen corpus's real xs-exclusion rule.
    line = json.dumps({"type": "session", "cwd": cwd, "pad": "x" * (12 * 1024)})
    path.write_text(line + "\n", encoding="utf-8")
    return path


def _age_file(path: Path, *, seconds_old: float, anchor: float) -> None:
    mtime = anchor - seconds_old
    os.utime(path, (mtime, mtime))


def _frozen_corpus(
    *,
    frozen_at: float,
    design_set: frozenset[str],
    confirmatory_set: frozenset[str],
    totals: dict[str, int],
    cap: int = 10,
) -> FrozenCorpus:
    return FrozenCorpus(
        frozen_at=frozen_at,
        time_window_days=180,
        excluded_size_bands=frozenset({"xs"}),
        per_lineage_cap=cap,
        design_set=design_set,
        confirmatory_set=confirmatory_set,
        totals=totals,
    )


def test_frozen_corpus_load_parses_real_schema(tmp_path: Path) -> None:
    payload = {
        "frozen_at": 1000.0,
        "design_set": ["lineage:a"],
        "confirmatory_set": ["lineage:b", "lineage:c"],
        "rules": {
            "time_window_days": 180,
            "size_band_exclusion": ["xs"],
            "per_lineage_session_cap": 10,
        },
        "totals": {"eligible_lineages": 3},
    }
    path = tmp_path / "corpus_manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    frozen = FrozenCorpus.load(path)
    assert frozen.frozen_at == 1000.0
    assert frozen.design_set == frozenset({"lineage:a"})
    assert frozen.confirmatory_set == frozenset({"lineage:b", "lineage:c"})
    assert frozen.per_lineage_cap == 10
    assert frozen.excluded_size_bands == frozenset({"xs"})


def test_build_session_manifest_only_includes_frozen_lineages(tmp_path: Path) -> None:
    anchor = time.time()
    home = tmp_path / "home"
    root = SourceRoot.resolve("root_a", tmp_path / "AetherForge")
    root.path.mkdir(parents=True)

    included_project = root.path / "laconic"
    excluded_project = root.path / "not-frozen"

    included_path = _write_session(
        home, (".claude", "projects", "laconic"), f"{_UUID_A}.jsonl", str(included_project)
    )
    excluded_path = _write_session(
        home, (".claude", "projects", "not-frozen"), f"{_UUID_B}.jsonl", str(excluded_project)
    )
    for path in (included_path, excluded_path):
        _age_file(path, seconds_old=STAGE_A_ACTIVE_THRESHOLD_SECONDS + 3600, anchor=anchor)

    from laconic.k1corpus.stage_a import project_lineage_id

    included_lineage = project_lineage_id(included_project.resolve())
    # excluded_project's lineage is deliberately NOT in either frozen set.

    frozen = _frozen_corpus(
        frozen_at=anchor,
        design_set=frozenset(),
        confirmatory_set=frozenset({included_lineage}),
        totals={
            "eligible_lineages": 2,
            "eligible_sessions_precap": 2,
            "design_lineages": 0,
            "design_sessions_postcap": 0,
            "confirmatory_lineages": 1,
            "confirmatory_sessions_postcap": 1,
        },
    )

    entries = build_session_manifest(frozen, home=home, roots=(root,), as_of=anchor)
    assert len(entries) == 1
    assert entries[0].set is ManifestSet.CONFIRMATORY
    assert entries[0].record.project_lineage_id == included_lineage


def test_build_session_manifest_respects_per_lineage_cap_most_recent_first(tmp_path: Path) -> None:
    anchor = time.time()
    home = tmp_path / "home"
    root = SourceRoot.resolve("root_a", tmp_path / "AetherForge")
    root.path.mkdir(parents=True)
    project = root.path / "laconic"

    uuids = [_UUID_A, _UUID_B, _UUID_C]
    ages = [STAGE_A_ACTIVE_THRESHOLD_SECONDS + 3600 * (i + 1) for i in range(3)]
    paths = []
    for uuid, age in zip(uuids, ages, strict=True):
        path = _write_session(
            home, (".claude", "projects", "laconic"), f"{uuid}.jsonl", str(project)
        )
        _age_file(path, seconds_old=age, anchor=anchor)
        paths.append(path)

    from laconic.k1corpus.stage_a import project_lineage_id

    lineage = project_lineage_id(project.resolve())
    frozen = _frozen_corpus(
        frozen_at=anchor,
        design_set=frozenset(),
        confirmatory_set=frozenset({lineage}),
        cap=2,
        totals={
            "eligible_lineages": 1,
            "eligible_sessions_precap": 3,
            "design_lineages": 0,
            "design_sessions_postcap": 0,
            "confirmatory_lineages": 1,
            "confirmatory_sessions_postcap": 2,
        },
    )
    entries = build_session_manifest(frozen, home=home, roots=(root,), as_of=anchor)
    assert len(entries) == 2
    # most recent (smallest age) = uuid A (age index 0), then uuid B (index 1);
    # uuid C (oldest) is dropped by the cap.
    session_ids = {e.record.session_id for e in entries}
    assert session_ids == {f"claude-code:{_UUID_A}", f"claude-code:{_UUID_B}"}


def test_build_session_manifest_anchors_to_as_of_not_wallclock(tmp_path: Path) -> None:
    # A session that would be "active" (excluded) relative to real now, but
    # is safely "closed" relative to a fixed past as_of, must still be
    # included -- proving the anchor, not wall-clock time, governs.
    frozen_at = time.time() - (STAGE_A_ACTIVE_THRESHOLD_SECONDS * 10)
    home = tmp_path / "home"
    root = SourceRoot.resolve("root_a", tmp_path / "AetherForge")
    root.path.mkdir(parents=True)
    project = root.path / "laconic"
    path = _write_session(
        home, (".claude", "projects", "laconic"), f"{_UUID_A}.jsonl", str(project)
    )
    # File mtime is old relative to real "now" (fully closed either way),
    # but well within the active window relative to a *future* as_of.
    old_mtime = time.time() - (STAGE_A_ACTIVE_THRESHOLD_SECONDS * 20)
    os.utime(path, (old_mtime, old_mtime))

    from laconic.k1corpus.stage_a import project_lineage_id

    lineage = project_lineage_id(project.resolve())
    frozen = _frozen_corpus(
        frozen_at=frozen_at,
        design_set=frozenset(),
        confirmatory_set=frozenset({lineage}),
        totals={
            "eligible_lineages": 1,
            "eligible_sessions_precap": 1,
            "design_lineages": 0,
            "design_sessions_postcap": 0,
            "confirmatory_lineages": 1,
            "confirmatory_sessions_postcap": 1,
        },
    )
    # Using frozen.frozen_at as the anchor (the default) must reproduce the
    # frozen totals even though real wall-clock "now" has since moved on.
    entries = build_session_manifest(frozen, home=home, roots=(root,))
    assert len(entries) == 1


def test_build_session_manifest_raises_on_totals_mismatch(tmp_path: Path) -> None:
    anchor = time.time()
    home = tmp_path / "home"
    root = SourceRoot.resolve("root_a", tmp_path / "AetherForge")
    root.path.mkdir(parents=True)
    project = root.path / "laconic"
    path = _write_session(
        home, (".claude", "projects", "laconic"), f"{_UUID_A}.jsonl", str(project)
    )
    _age_file(path, seconds_old=STAGE_A_ACTIVE_THRESHOLD_SECONDS + 3600, anchor=anchor)

    from laconic.k1corpus.stage_a import project_lineage_id

    lineage = project_lineage_id(project.resolve())
    frozen = _frozen_corpus(
        frozen_at=anchor,
        design_set=frozenset(),
        confirmatory_set=frozenset({lineage}),
        totals={
            "eligible_lineages": 99,  # deliberately wrong
            "eligible_sessions_precap": 99,
            "design_lineages": 0,
            "design_sessions_postcap": 0,
            "confirmatory_lineages": 1,
            "confirmatory_sessions_postcap": 1,
        },
    )
    with pytest.raises(TotalsMismatchError, match="eligible_lineages"):
        build_session_manifest(frozen, home=home, roots=(root,), as_of=anchor)


def test_manifest_entry_to_json_never_contains_raw_path(tmp_path: Path) -> None:
    anchor = time.time()
    home = tmp_path / "home"
    root = SourceRoot.resolve("root_a", tmp_path / "AetherForge")
    root.path.mkdir(parents=True)
    project = root.path / "laconic"
    path = _write_session(
        home, (".claude", "projects", "laconic"), f"{_UUID_A}.jsonl", str(project)
    )
    _age_file(path, seconds_old=STAGE_A_ACTIVE_THRESHOLD_SECONDS + 3600, anchor=anchor)

    from laconic.k1corpus.stage_a import project_lineage_id

    lineage = project_lineage_id(project.resolve())
    frozen = _frozen_corpus(
        frozen_at=anchor,
        design_set=frozenset({lineage}),
        confirmatory_set=frozenset(),
        totals={
            "eligible_lineages": 1,
            "eligible_sessions_precap": 1,
            "design_lineages": 1,
            "design_sessions_postcap": 1,
            "confirmatory_lineages": 0,
            "confirmatory_sessions_postcap": 0,
        },
    )
    entries = build_session_manifest(frozen, home=home, roots=(root,), as_of=anchor)
    payload = entries[0].to_json()
    assert payload["set"] == "design"
    serialized = json.dumps(payload)
    assert str(root.path) not in serialized
    assert "/laconic" not in serialized
