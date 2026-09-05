"""End-to-end test for `laconic research k1 stage-a scan` via `laconic.cli`, and a
direct test of `scan_all_providers`'s test-only `home`/`roots` overrides.
No test touches a real `~/.claude`/`~/.codex`/`~/.omp` path."""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path

import pytest

import laconic.cli as cli_module
from laconic.cli import EXIT_K1_STAGE_A_STOP, EXIT_OK, build_parser
from laconic.k1corpus.report import Disposition, StageAReport, build_report, scan_all_providers
from laconic.k1corpus.stage_a import STAGE_A_ACTIVE_THRESHOLD_SECONDS, Provider, SourceRoot


def _write_session(home: Path, provider_subpath: tuple[str, ...], filename: str, cwd: str) -> Path:
    path = home.joinpath(*provider_subpath, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "session", "cwd": cwd}) + "\n", encoding="utf-8")
    return path


def test_scan_all_providers_stops_with_only_claude_code_and_two_lineages(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = SourceRoot.resolve("root_a", tmp_path / "AetherForge")
    root.path.mkdir(parents=True)

    for name, project in (
        ("41380cc3-ebf5-45c2-b0c6-c5f071f7a319", "laconic"),
        ("52481dd4-fca6-56b3-c1d7-d8e082e830fc", "archex"),
    ):
        cwd = str(root.path / project)
        path = _write_session(home, (".claude", "projects", project), f"{name}.jsonl", cwd)
        old_time = time.time() - (STAGE_A_ACTIVE_THRESHOLD_SECONDS + 3600)
        os.utime(path, (old_time, old_time))

    report = scan_all_providers(home=home, roots=(root,), now=time.time())
    assert len(report.records) == 2
    assert report.disposition is Disposition.STOP  # single provider, only 2 lineages


def test_k1_stage_a_scan_cli_writes_ledger_and_returns_stop_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_scan_all_providers() -> StageAReport:
        per_provider = {
            Provider.CLAUDE_CODE: ([], Counter()),
            Provider.CODEX: ([], Counter()),
            Provider.OMP: ([], Counter()),
        }
        return build_report(time.time(), per_provider)

    monkeypatch.setattr(cli_module, "scan_all_providers", _fake_scan_all_providers)

    parser = build_parser()
    out_path = tmp_path / "ledger.json"
    args = parser.parse_args(["research", "k1", "stage-a", "scan", "--out", str(out_path)])
    exit_code = args.handler(args)

    assert exit_code == EXIT_K1_STAGE_A_STOP
    assert exit_code != EXIT_OK
    assert out_path.exists()
    payload = json.loads(out_path.read_text())
    assert payload["disposition"] == Disposition.STOP.value


def test_k1_stage_a_scan_cli_parses_nested_subcommands() -> None:
    parser = build_parser()
    namespace = parser.parse_args(
        ["research", "k1", "stage-a", "scan", "--out", "/tmp/does-not-run.json"]
    )
    assert namespace.k1_command == "stage-a"
    assert namespace.k1_stage_a_command == "scan"
