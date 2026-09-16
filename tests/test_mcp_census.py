"""Frozen MCP census population, privacy, mechanism, and gate behavior."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from laconic.cli import EXIT_OK, main
from laconic.ledger import Ledger
from laconic.mcp_census.cli import CensusExecutionError, execute_census
from laconic.mcp_census.manifest import PUBLIC_KEYS, freeze_manifest, load_manifest
from laconic.mcp_census.report import (
    GO_DISPOSITION,
    HOLD_PREFIX,
    OpportunityPrivacyError,
    build_report,
    validate_disposition,
)

START = "2026-09-13T00:00:00Z"
END = datetime(2026, 9, 14, tzinfo=UTC)


def _write(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def _long_text(label: str = "payload") -> str:
    return "\n".join(f"{label} line {index}: " + "x" * 80 for index in range(240))


_TOP_LEVEL_SAME = object()


def _omp_session(
    root: Path,
    session: str,
    results: list[tuple[str, str, bool]],
) -> Path:
    records: list[dict[str, Any]] = [
        {"type": "session", "id": session, "cwd": "/private/repo", "timestamp": START}
    ]
    for index, (tool_name, text, is_error) in enumerate(results, start=1):
        records.append(
            {
                "type": "message",
                "timestamp": START,
                "message": {
                    "role": "toolResult",
                    "toolCallId": f"call-{index}",
                    "toolName": tool_name,
                    "isError": is_error,
                    "content": [{"type": "text", "text": text}],
                    "details": {"private": "never serialized"},
                    "timestamp": START,
                },
            }
        )
    path = root / f"2026-09-13T00-00-00-000Z_{session}.jsonl"
    _write(path, records)
    return path


def _claude_session(
    root: Path,
    session: str,
    *,
    tool_name: str = "mcp__server__tool",
    result: object = "text result",
    is_error: bool = False,
    top_level_result: object = _TOP_LEVEL_SAME,
) -> Path:
    path = root / "project" / f"{session}.jsonl"
    _write(
        path,
        [
            {
                "type": "assistant",
                "sessionId": session,
                "timestamp": START,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tool-1", "name": tool_name, "input": {}}
                    ],
                },
            },
            {
                "type": "user",
                "sessionId": session,
                "session_id": session,
                "timestamp": START,
                "toolUseResult": (
                    result if top_level_result is _TOP_LEVEL_SAME else top_level_result
                ),
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "is_error": is_error,
                            "content": result,
                        }
                    ],
                },
            },
        ],
    )
    return path


def _manifest(tmp_path: Path, omp: Path, claude: Path):
    return freeze_manifest(
        tmp_path / "manifest.json",
        source_roots={"omp": omp, "claude_code": claude},
        exclusive_end=END,
    )


def _go_population(tmp_path: Path) -> tuple[Path, Path]:
    omp = tmp_path / "omp"
    claude = tmp_path / "claude"
    claude.mkdir()
    for session_number in range(10):
        session = f"00000000-0000-0000-0000-{session_number:012d}"
        _omp_session(
            omp,
            session,
            [
                (
                    "mcp__server__tool",
                    _long_text(f"s{session_number}-{index}"),
                    False,
                )
                for index in range(10)
            ],
        )
    return omp, claude


def test_census_freezes_before_parsing_and_produces_exact_key_go(tmp_path: Path) -> None:
    omp, claude = _go_population(tmp_path)
    output = tmp_path / "out"

    artifacts = execute_census(output, omp_root=omp, claude_root=claude)

    assert artifacts.report.payload["disposition"] == GO_DISPOSITION
    assert artifacts.report.payload["predicates"] == {
        "evidence_floor": True,
        "materiality": True,
        "emission": True,
        "aggregate_reduction": True,
        "recoverability": True,
        "validity": True,
    }
    assert set(artifacts.report.disposition) == set(PUBLIC_KEYS)
    assert artifacts.report.disposition["population"]["eligible_mcp_results"] == 100
    assert artifacts.report.payload["provider_calls"] == 0
    assert load_manifest(artifacts.manifest_path).sha256 == artifacts.manifest.sha256
    manifest_text = artifacts.manifest_path.read_text(encoding="utf-8")
    assert str(omp) not in manifest_text
    assert "00000000-0000-0000-0000-000000000000" not in manifest_text
    assert "mcp__server__tool" not in artifacts.report.to_json()
    assert "00000000-0000-0000-0000-000000000000" not in artifacts.report.to_json()
    assert "never serialized" not in artifacts.report.to_json()
    assert "/private/repo" not in artifacts.report.to_json()
    assert "server" not in json.dumps(artifacts.report.disposition["population"])


def test_both_hosts_admit_only_successful_nonempty_single_text(tmp_path: Path) -> None:
    omp = tmp_path / "omp"
    claude = tmp_path / "claude"
    _omp_session(
        omp,
        "00000000-0000-0000-0000-000000000001",
        [
            ("mcp__one_tool", _long_text(), False),
            ("mcp__one__error", "error", True),
            ("Read", "ordinary candidate", False),
        ],
    )
    _claude_session(
        claude,
        "00000000-0000-0000-0000-000000000002",
        result=_long_text("claude"),
        top_level_result={"private": "transport metadata"},
    )
    _claude_session(
        claude,
        "00000000-0000-0000-0000-000000000003",
        result=[{"type": "text", "text": "a"}, {"type": "image", "data": "secret"}],
    )

    report = build_report(_manifest(tmp_path, omp, claude)).payload

    assert report["population"]["candidate_results"] == 3
    assert report["population"]["mcp_results"] == 4
    assert report["population"]["eligible_mcp_results"] == 2
    assert report["population"]["exclusions"]["error"] == 1
    assert report["population"]["exclusions"]["mixed_content"] == 1
    assert report["population"]["by_host"]["omp"]["eligible_mcp_results"] == 1
    assert report["population"]["by_host"]["claude_code"]["eligible_mcp_results"] == 1


def test_evidence_floor_and_materiality_fail_without_redefining_population(tmp_path: Path) -> None:
    omp = tmp_path / "omp"
    claude = tmp_path / "claude"
    claude.mkdir()
    _omp_session(
        omp,
        "00000000-0000-0000-0000-000000000001",
        [
            ("mcp__one__tool", _long_text("mcp"), False),
            ("Read", _long_text("ordinary") * 20, False),
        ],
    )

    report = build_report(_manifest(tmp_path, omp, claude)).payload

    assert report["predicates"]["evidence_floor"] is False
    assert report["predicates"]["materiality"] is False
    assert report["failed_predicates"][:2] == ["evidence_floor", "materiality"]
    assert report["disposition"].startswith(HOLD_PREFIX)


def test_short_results_fail_emission_and_aggregate_reduction(tmp_path: Path) -> None:
    omp = tmp_path / "omp"
    claude = tmp_path / "claude"
    claude.mkdir()
    for session_number in range(10):
        session = f"00000000-0000-0000-0000-{session_number:012d}"
        _omp_session(omp, session, [("mcp__one__tool", "tiny", False) for _ in range(10)])

    report = build_report(_manifest(tmp_path, omp, claude)).payload

    assert report["predicates"]["evidence_floor"] is True
    assert report["predicates"]["materiality"] is True
    assert report["predicates"]["emission"] is False
    assert report["predicates"]["aggregate_reduction"] is False


def test_recovery_mismatch_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    omp, claude = _go_population(tmp_path)
    original = Ledger.expand

    def mismatching_expand(self: Ledger, reference: str) -> str:
        return original(self, reference) + "changed"

    monkeypatch.setattr(Ledger, "expand", mismatching_expand)

    report = build_report(_manifest(tmp_path, omp, claude)).payload

    assert report["probe"]["recovery_mismatches"] == 100
    assert report["predicates"]["recoverability"] is False
    assert report["predicates"]["aggregate_reduction"] is False


def test_source_mutation_fails_validity_without_hiding_other_predicates(tmp_path: Path) -> None:
    omp, claude = _go_population(tmp_path)
    manifest = _manifest(tmp_path, omp, claude)
    source = next(omp.glob("*.jsonl"))
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    report = build_report(manifest).payload

    assert report["source_unchanged"] is False
    assert report["predicates"]["validity"] is False
    assert report["failed_predicates"] == ["validity"]


def test_public_privacy_gate_rejects_unknown_fields_and_weakened_limits(tmp_path: Path) -> None:
    omp, claude = _go_population(tmp_path)
    public = build_report(_manifest(tmp_path, omp, claude)).disposition

    unknown = {**public, "path": "/private/repo"}
    with pytest.raises(OpportunityPrivacyError, match="exactly"):
        validate_disposition(unknown)

    weakened = {**public, "limitations": ["offline_fallback_probe_not_runtime_support"]}
    for forbidden in ("mcp__private_server__read", "00000000-0000-0000-0000-000000000001"):
        leaked = {**public, "census_id": forbidden}
        with pytest.raises(OpportunityPrivacyError, match="identity"):
            validate_disposition(leaked)
    with pytest.raises(OpportunityPrivacyError, match="limitations"):
        validate_disposition(weakened)


def test_cli_executes_offline_and_prints_only_public_json(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    omp = tmp_path / "omp"
    claude = tmp_path / "claude"
    claude.mkdir()
    _omp_session(
        omp,
        "00000000-0000-0000-0000-000000000001",
        [("mcp__one__tool", _long_text(), False)],
    )
    output = tmp_path / "out"

    assert (
        main(
            [
                "research",
                "mcp-census",
                "--omp-root",
                str(omp),
                "--claude-root",
                str(claude),
                "--output",
                str(output),
                "--format",
                "json",
            ]
        )
        == EXIT_OK
    )

    public = json.loads(capsys.readouterr().out)
    assert set(public) == set(PUBLIC_KEYS)
    assert public["disposition"].startswith(HOLD_PREFIX)
    assert (output / "manifest.json").is_file()
    assert (output / "opportunity-report.json").is_file()
    assert (output / "mcp-disposition.json").is_file()


def test_execution_is_single_use_and_refuses_output_inside_sources(tmp_path: Path) -> None:
    omp, claude = _go_population(tmp_path)
    output = tmp_path / "out"
    execute_census(output, omp_root=omp, claude_root=claude)

    with pytest.raises(CensusExecutionError, match="already exists"):
        execute_census(output, omp_root=omp, claude_root=claude)
    with pytest.raises(CensusExecutionError, match="inside a source root"):
        execute_census(omp / "report", omp_root=omp, claude_root=claude)
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    with pytest.raises(CensusExecutionError):
        execute_census(blocked_parent / "out", omp_root=omp, claude_root=claude)
