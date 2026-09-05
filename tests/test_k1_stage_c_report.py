"""Protocol-report and CLI wiring tests for K1 Stage C, with no provider call."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from laconic import cli
from laconic.k1corpus.stage_a import Provider
from laconic.k1corpus.stage_b import ManifestSet
from laconic.k1corpus.stage_c import (
    CompletedSession,
    LiveStageCSessionRunner,
    LoadedStageCManifest,
    PairedSessionMetrics,
    ResolvedStageCSession,
    StageCAudit,
    StageCLedger,
    StageCManifestEntry,
    audit_retailogists_exclusions,
)
from laconic.k1corpus.stage_c_report import PROTOCOL_REPORT_FIELDS, generate_stage_c_report
from laconic.observe.audit import read_chain, verify_chain
from laconic.replay.engine import RecordedAction, ReplayTurnCapture, TurnUsage


def _entry(session_id: str = "claude-code:one") -> StageCManifestEntry:
    return StageCManifestEntry(
        set=ManifestSet.CONFIRMATORY,
        provider=Provider.CLAUDE_CODE,
        session_id=session_id,
        project_lineage_id="lineage:self-owned",
    )


def _metrics(*, baseline: float = 1.0, codec_on: float = 0.7) -> PairedSessionMetrics:
    return PairedSessionMetrics(
        baseline_cost_usd=baseline,
        codec_on_cost_usd=codec_on,
        induced_turn_count=2,
        induced_turn_cost_usd=0.1,
        equivalent_turn_count=9,
        compared_turn_count=10,
    )


def test_report_matches_every_protocol_analysis_field_and_aggregates_complete_pairs(
    tmp_path: Path,
) -> None:
    entries = (_entry("claude-code:one"), _entry("claude-code:two"))
    excluded = StageCManifestEntry(
        set=ManifestSet.CONFIRMATORY,
        provider=Provider.CLAUDE_CODE,
        session_id="claude-code:excluded",
        project_lineage_id="lineage:retailogists",
    )
    ledger = StageCLedger(tmp_path / "ledger.json")
    for entry in entries:
        ledger.record_completed(
            CompletedSession(
                session_id=entry.session_id,
                realized_cost_usd=0.7,
                artifact_name=f"{entry.session_id.rsplit(':', 1)[1]}.codec-on.jsonl",
                metrics=_metrics(),
            )
        )

    report = generate_stage_c_report(
        LoadedStageCManifest(entries=entries, excluded_retailogists=(excluded,)),
        selected_set=ManifestSet.CONFIRMATORY,
        ledger=ledger,
    )

    payload = report.to_json()
    assert tuple(payload) == PROTOCOL_REPORT_FIELDS
    assert payload["corpus_composition"] == {
        "set": "confirmatory",
        "selected_sessions": 2,
        "completed_sessions": 2,
        "missing_or_partial_sessions": 0,
        "retailogists_excluded_sessions": 1,
        "retailogists_excluded_lineages": 1,
    }
    cost_totals = payload["cost_totals"]
    assert cost_totals == {
        "baseline_cost_usd": 2.0,
        "codec_on_cost_usd": 1.4,
        "induced_turn_count": 4,
        "induced_turn_cost_usd": 0.2,
        "net_cost_savings_usd": 0.6,
    }
    assert payload["k1"] == {
        "value_pct": 30.0,
        "target_pct": 25.0,
        "kill_threshold_pct": 15.0,
        "disposition": "target_met",
    }
    assert payload["k2"] == {"value_pct": 90.0, "availability": "measured"}
    assert payload["k4"]["availability"] == "not_collected"
    assert payload["k5"]["availability"] == "not_collected"
    assert "self-owned corpus" in payload["representative_scope"]


def test_confirmatory_partial_evidence_never_produces_a_k1_value(tmp_path: Path) -> None:
    entries = (_entry("claude-code:one"), _entry("claude-code:two"))
    ledger = StageCLedger(tmp_path / "ledger.json")
    ledger.record_completed(
        CompletedSession(
            session_id=entries[0].session_id,
            realized_cost_usd=0.7,
            artifact_name="one.codec-on.jsonl",
            metrics=_metrics(),
        )
    )

    payload = generate_stage_c_report(
        LoadedStageCManifest(entries=entries, excluded_retailogists=()),
        selected_set=ManifestSet.CONFIRMATORY,
        ledger=ledger,
    ).to_json()

    assert payload["cost_totals"]["baseline_cost_usd"] is None
    assert payload["k1"]["value_pct"] is None
    assert payload["k1"]["disposition"] == "invalid_partial_paired_evidence"
    assert payload["k2"]["availability"] == "invalid_partial_paired_evidence"


def test_retailogists_exclusions_enter_the_content_free_audit(tmp_path: Path) -> None:
    excluded = StageCManifestEntry(
        set=ManifestSet.CONFIRMATORY,
        provider=Provider.CLAUDE_CODE,
        session_id="claude-code:excluded",
        project_lineage_id="lineage:retailogists",
    )
    audit_path = tmp_path / "audit.jsonl"
    audit_retailogists_exclusions(
        LoadedStageCManifest(entries=(_entry(),), excluded_retailogists=(excluded,)),
        StageCAudit(audit_path),
    )

    chain = read_chain(audit_path)
    verify_chain(chain)
    receipt = chain[0].receipt
    assert receipt["outcome"] == "excluded_retailogists"
    assert receipt["session_id"] == excluded.session_id


def test_stage_c_cli_wires_fake_batch_results_into_json_protocol_report(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    entry = _entry()
    manifest = LoadedStageCManifest(entries=(entry,), excluded_retailogists=())
    monkeypatch.setattr(cli, "load_stage_c_manifest", lambda *_args, **_kwargs: manifest)

    class FakeClient:
        def respond(self, **_kwargs: object) -> tuple[object, ...]:
            raise AssertionError("the fake batch replacement must prevent a provider call")

    monkeypatch.setattr(cli, "_load_client_factory", lambda _spec: FakeClient)

    def fake_batch(*_args: object, **kwargs: object) -> tuple[object, ...]:
        ledger = kwargs["ledger"]
        assert isinstance(ledger, StageCLedger)
        ledger.record_completed(
            CompletedSession(
                session_id=entry.session_id,
                realized_cost_usd=0.7,
                artifact_name="one.codec-on.jsonl",
                metrics=_metrics(),
            )
        )
        return ()

    monkeypatch.setattr(cli, "run_resumable_batch", fake_batch)
    exit_code = cli.main(
        [
            "research",
            "k1",
            "stage-c",
            "run",
            "--set",
            "confirmatory",
            "--spend-cap",
            "1.0",
            "--client",
            "local:factory",
            "--state-dir",
            str(tmp_path / "state"),
            "--format",
            "json",
        ]
    )

    assert exit_code == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert tuple(payload) == PROTOCOL_REPORT_FIELDS
    assert payload["k1"]["disposition"] == "target_met"


def test_live_session_runner_uses_fake_client_and_persists_mode_restricted_artifact(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline.jsonl"
    baseline.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "model": "claude-sonnet-5",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "read",
                                "name": "Read",
                                "input": {"path": "a.py"},
                            }
                        ],
                        "usage": {
                            "input_tokens": 10,
                            "cache_read_input_tokens": 100,
                            "cache_creation_input_tokens": 20,
                            "output_tokens": 10,
                        },
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "read", "content": "x = 1\\n"}
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "model": "claude-sonnet-5",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "edit",
                                "name": "Edit",
                                "input": {"path": "a.py", "old": "x", "new": "y"},
                            }
                        ],
                        "usage": {
                            "input_tokens": 10,
                            "cache_read_input_tokens": 100,
                            "cache_creation_input_tokens": 20,
                            "output_tokens": 10,
                        },
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    class FakeClient:
        closed = False

        def respond(self, **_kwargs: object) -> tuple[ReplayTurnCapture, ...]:
            return (
                ReplayTurnCapture(
                    action=RecordedAction(
                        tool_use_id="proposed",
                        tool_name="Edit",
                        tool_input={"path": "a.py", "old": "x", "new": "y"},
                    ),
                    usage=TurnUsage(
                        model="claude-sonnet-5",
                        input_tokens=10,
                        cache_read=100,
                        cache_write=20,
                        output_tokens=10,
                    ),
                ),
            )

        def close(self) -> None:
            self.closed = True

    client = FakeClient()
    entry = _entry()
    runner = LiveStageCSessionRunner(
        client_factory=lambda: client,
        artifact_dir=tmp_path / "artifacts",
        observation_builder=lambda _baseline: {1: "codec observation"},
    )
    execution = runner.run(
        ResolvedStageCSession(entry=entry, baseline=baseline, model="claude-sonnet-5"),
        cost_cap_usd=1.0,
    )

    assert client.closed is True
    assert execution.metrics is not None
    assert execution.metrics.equivalent_turn_count == 1
    assert execution.metrics.compared_turn_count == 1
    artifact = tmp_path / "artifacts" / execution.artifact_name
    assert artifact.is_file()
    assert stat.S_IMODE((tmp_path / "artifacts").stat().st_mode) == 0o700
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600


def test_live_session_runner_rejects_empty_observation_evidence(tmp_path: Path) -> None:
    baseline = tmp_path / "empty-observations.jsonl"
    baseline.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-5",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "first",
                            "name": "Read",
                            "input": {"path": "a.py"},
                        }
                    ],
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 100,
                        "cache_creation_input_tokens": 20,
                        "output_tokens": 10,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class FakeClient:
        def respond(self, **_kwargs: object) -> tuple[ReplayTurnCapture, ...]:
            raise AssertionError("empty evidence must fail before a client call")

        def close(self) -> None:
            return None

    factory_called = False

    def client_factory() -> FakeClient:
        nonlocal factory_called
        factory_called = True
        return FakeClient()

    runner = LiveStageCSessionRunner(
        client_factory=client_factory,
        artifact_dir=tmp_path / "artifacts",
        observation_builder=lambda _baseline: {},
    )
    with pytest.raises(ValueError, match="no replayable observations"):
        runner.run(
            ResolvedStageCSession(entry=_entry(), baseline=baseline, model="claude-sonnet-5"),
            cost_cap_usd=1.0,
        )
    assert factory_called is False
