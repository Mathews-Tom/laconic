"""End-to-end test for `laconic k1 stage-b build-manifest` via
`laconic.cli`. No test touches a real `~/.claude`/`~/.codex`/`~/.omp`
path -- the CLI's `build_session_manifest` call is monkeypatched to a
thin wrapper pinning `home`/`roots` to synthetic fixtures, exercising
the exact same production code path with test-only inputs."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import laconic.cli as cli_module
from laconic.cli import EXIT_K1_STAGE_B_TOTALS_MISMATCH, EXIT_OK, build_parser
from laconic.k1corpus.stage_a import (
    STAGE_A_ACTIVE_THRESHOLD_SECONDS,
    SourceRoot,
    project_lineage_id,
)
from laconic.k1corpus.stage_b import FrozenCorpus, ManifestEntry
from laconic.k1corpus.stage_b import build_session_manifest as real_build_session_manifest


def _write_session(home: Path, subpath: tuple[str, ...], filename: str, cwd: str) -> Path:
    path = home.joinpath(*subpath, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"type": "session", "cwd": cwd, "pad": "x" * (12 * 1024)})
    path.write_text(line + "\n", encoding="utf-8")
    return path


def _pin_build_session_manifest(
    monkeypatch: pytest.MonkeyPatch, *, home: Path, root: SourceRoot
) -> None:
    def _patched(frozen: FrozenCorpus) -> tuple[ManifestEntry, ...]:
        return real_build_session_manifest(frozen, home=home, roots=(root,))

    monkeypatch.setattr(cli_module, "build_session_manifest", _patched)


def test_build_manifest_cli_writes_manifest_and_returns_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor = time.time()
    home = tmp_path / "home"
    workspace = tmp_path / "AetherForge"
    project = workspace / "laconic"
    uuid = "41380cc3-ebf5-45c2-b0c6-c5f071f7a319"
    path = _write_session(home, (".claude", "projects", "laconic"), f"{uuid}.jsonl", str(project))
    old_mtime = anchor - (STAGE_A_ACTIVE_THRESHOLD_SECONDS + 3600)
    os.utime(path, (old_mtime, old_mtime))

    lineage = project_lineage_id(project.resolve())
    corpus_manifest = {
        "frozen_at": anchor,
        "design_set": [],
        "confirmatory_set": [lineage],
        "rules": {
            "time_window_days": 180,
            "size_band_exclusion": ["xs"],
            "per_lineage_session_cap": 10,
        },
        "totals": {
            "eligible_lineages": 1,
            "eligible_sessions_precap": 1,
            "design_lineages": 0,
            "design_sessions_postcap": 0,
            "confirmatory_lineages": 1,
            "confirmatory_sessions_postcap": 1,
        },
    }
    corpus_manifest_path = tmp_path / "corpus_manifest.json"
    corpus_manifest_path.write_text(json.dumps(corpus_manifest), encoding="utf-8")

    root = SourceRoot.resolve("root_a", workspace)
    _pin_build_session_manifest(monkeypatch, home=home, root=root)

    parser = build_parser()
    out_path = tmp_path / "session_manifest.json"
    args = parser.parse_args(
        [
            "k1",
            "stage-b",
            "build-manifest",
            "--corpus-manifest",
            str(corpus_manifest_path),
            "--out",
            str(out_path),
        ]
    )
    exit_code = args.handler(args)

    assert exit_code == EXIT_OK
    assert out_path.exists()
    payload = json.loads(out_path.read_text())
    assert len(payload) == 1
    assert payload[0]["set"] == "confirmatory"
    assert payload[0]["session_id"] == f"claude-code:{uuid}"


def test_build_manifest_cli_returns_mismatch_exit_code_on_bad_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "AetherForge"
    root = SourceRoot.resolve("root_a", workspace)
    workspace.mkdir(parents=True)
    _pin_build_session_manifest(monkeypatch, home=home, root=root)

    corpus_manifest = {
        "frozen_at": time.time(),
        "design_set": [],
        "confirmatory_set": [],
        "rules": {
            "time_window_days": 180,
            "size_band_exclusion": ["xs"],
            "per_lineage_session_cap": 10,
        },
        "totals": {
            "eligible_lineages": 999,
            "eligible_sessions_precap": 999,
            "design_lineages": 0,
            "design_sessions_postcap": 0,
            "confirmatory_lineages": 0,
            "confirmatory_sessions_postcap": 0,
        },
    }
    corpus_manifest_path = tmp_path / "corpus_manifest.json"
    corpus_manifest_path.write_text(json.dumps(corpus_manifest), encoding="utf-8")

    parser = build_parser()
    out_path = tmp_path / "session_manifest.json"
    args = parser.parse_args(
        [
            "k1",
            "stage-b",
            "build-manifest",
            "--corpus-manifest",
            str(corpus_manifest_path),
            "--out",
            str(out_path),
        ]
    )
    exit_code = args.handler(args)
    assert exit_code == EXIT_K1_STAGE_B_TOTALS_MISMATCH
    assert not out_path.exists()


def test_build_manifest_cli_returns_mismatch_exit_code_on_missing_corpus_manifest(
    tmp_path: Path,
) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "k1",
            "stage-b",
            "build-manifest",
            "--corpus-manifest",
            str(tmp_path / "does-not-exist.json"),
        ]
    )
    exit_code = args.handler(args)
    assert exit_code == EXIT_K1_STAGE_B_TOTALS_MISMATCH


def test_build_manifest_cli_parses_nested_subcommands() -> None:
    parser = build_parser()
    namespace = parser.parse_args(["k1", "stage-b", "build-manifest"])
    assert namespace.k1_command == "stage-b"
    assert namespace.k1_stage_b_command == "build-manifest"
