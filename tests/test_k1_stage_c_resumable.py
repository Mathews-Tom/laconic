"""Durable ledger and audit tests for K1 Stage C with no provider process."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from laconic.k1corpus.stage_a import Provider
from laconic.k1corpus.stage_b import ManifestSet
from laconic.k1corpus.stage_c import (
    ChargedAttempt,
    CompletedSession,
    PartialSessionCostError,
    ResolvedStageCSession,
    SessionExecution,
    StageCAudit,
    StageCLedger,
    StageCManifestEntry,
    run_resumable_batch,
)
from laconic.observe.audit import read_chain, verify_chain


@dataclass
class FakeDurableRunner:
    calls: list[str] = field(default_factory=list)
    fail_for: set[str] = field(default_factory=set)

    def run(self, session: ResolvedStageCSession, *, cost_cap_usd: float) -> SessionExecution:
        self.calls.append(session.entry.session_id)
        if session.entry.session_id in self.fail_for:
            raise RuntimeError("synthetic client failure")
        return SessionExecution(
            realized_cost_usd=0.2,
            artifact_name=f"{session.entry.session_id.rsplit(':', 1)[1]}.codec-on.jsonl",
            turn_count=2,
            induced_turn_count=1,
        )


def _entries() -> tuple[StageCManifestEntry, ...]:
    return tuple(
        StageCManifestEntry(
            set=ManifestSet.DESIGN,
            provider=Provider.CLAUDE_CODE,
            session_id=f"claude-code:{index}",
            project_lineage_id="lineage:allowed",
        )
        for index in range(3)
    )


def _resolve(tmp_path: Path):
    paths = {
        entry.session_id: tmp_path / f"{index}.jsonl" for index, entry in enumerate(_entries())
    }
    for path in paths.values():
        path.write_text("synthetic baseline", encoding="utf-8")
    return lambda _provider, session_id: paths[session_id]


def test_mid_batch_kill_then_resume_skips_completed_session_without_second_spend(
    tmp_path: Path,
) -> None:
    entries = _entries()
    resolver = _resolve(tmp_path)
    ledger_path = tmp_path / "state" / "ledger.json"
    audit_path = tmp_path / "state" / "audit.jsonl"
    runner = FakeDurableRunner()

    def kill_after_first(completed: CompletedSession) -> None:
        assert completed.session_id == entries[0].session_id
        raise KeyboardInterrupt("simulated process kill")

    with pytest.raises(KeyboardInterrupt, match="simulated process kill"):
        run_resumable_batch(
            entries,
            spend_cap_usd=1.0,
            runner=runner,
            ledger=StageCLedger(ledger_path),
            audit=StageCAudit(audit_path),
            resolver=resolver,
            model_resolver=lambda _path: "claude-sonnet-5",
            after_completion=kill_after_first,
        )

    persisted = StageCLedger(ledger_path)
    assert persisted.completed[entries[0].session_id].realized_cost_usd == pytest.approx(0.2)
    resumed = run_resumable_batch(
        entries,
        spend_cap_usd=1.0,
        runner=runner,
        ledger=persisted,
        audit=StageCAudit(audit_path),
        resolver=resolver,
        model_resolver=lambda _path: "claude-sonnet-5",
    )

    assert runner.calls == [entry.session_id for entry in entries]
    assert resumed[0].outcome == "skipped_completed"
    assert {item.session_id for item in StageCLedger(ledger_path).completed.values()} == {
        entry.session_id for entry in entries
    }
    chain = read_chain(audit_path)
    verify_chain(chain)
    assert [entry.receipt["outcome"] for entry in chain] == ["completed"] * 3


def test_ledger_and_audit_are_mode_restricted_and_content_free(tmp_path: Path) -> None:
    entries = _entries()[:1]
    resolver = _resolve(tmp_path)
    root = tmp_path / "private"
    ledger_path = root / "ledger.json"
    audit_path = root / "audit.jsonl"

    run_resumable_batch(
        entries,
        spend_cap_usd=1.0,
        runner=FakeDurableRunner(),
        ledger=StageCLedger(ledger_path),
        audit=StageCAudit(audit_path),
        resolver=resolver,
        model_resolver=lambda _path: "claude-sonnet-5",
    )

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(ledger_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(audit_path.stat().st_mode) == 0o600
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    receipt = payload["receipt"]
    assert set(receipt) == {
        "session_id",
        "set",
        "lineage_id",
        "provider",
        "model",
        "outcome",
        "realized_cost_usd",
        "turn_count",
        "induced_turn_count",
        "artifact_name",
        "error_class",
    }
    assert "synthetic baseline" not in audit_path.read_text(encoding="utf-8")


def test_client_error_is_isolated_and_next_session_completes(tmp_path: Path) -> None:
    entries = _entries()[:2]
    runner = FakeDurableRunner(fail_for={entries[0].session_id})
    ledger_path = tmp_path / "ledger.json"
    audit_path = tmp_path / "audit.jsonl"

    outcomes = run_resumable_batch(
        entries,
        spend_cap_usd=1.0,
        runner=runner,
        ledger=StageCLedger(ledger_path),
        audit=StageCAudit(audit_path),
        resolver=_resolve(tmp_path),
        model_resolver=lambda _path: "claude-sonnet-5",
    )

    assert [outcome.outcome for outcome in outcomes] == ["client_error", "completed"]
    assert runner.calls == [entry.session_id for entry in entries]
    assert set(StageCLedger(ledger_path).completed) == {entries[1].session_id}
    receipts = [entry.receipt for entry in read_chain(audit_path)]
    assert [receipt["error_class"] for receipt in receipts] == ["RuntimeError", None]


def test_partial_session_charge_stops_batch_without_losing_realized_spend(tmp_path: Path) -> None:
    entries = _entries()[:2]
    ledger_path = tmp_path / "ledger.json"
    audit_path = tmp_path / "audit.jsonl"

    @dataclass
    class CappedRunner:
        calls: list[str] = field(default_factory=list)

        def run(self, session: ResolvedStageCSession, *, cost_cap_usd: float) -> SessionExecution:
            self.calls.append(session.entry.session_id)
            raise PartialSessionCostError(
                realized_cost_usd=cost_cap_usd + 0.1,
                artifact_name="partial.codec-on.jsonl",
            )

    runner = CappedRunner()
    outcomes = run_resumable_batch(
        entries,
        spend_cap_usd=0.2,
        runner=runner,
        ledger=StageCLedger(ledger_path),
        audit=StageCAudit(audit_path),
        resolver=_resolve(tmp_path),
        model_resolver=lambda _path: "claude-sonnet-5",
    )

    assert [entry.outcome for entry in outcomes] == ["cost_cap_exceeded"]
    assert runner.calls == [entries[0].session_id]
    reloaded = StageCLedger(ledger_path)
    assert reloaded.completed == {}
    assert reloaded.charges == (
        ChargedAttempt(
            session_id=entries[0].session_id,
            realized_cost_usd=pytest.approx(0.3),
            artifact_name="partial.codec-on.jsonl",
        ),
    )
    assert reloaded.realized_cost_usd == pytest.approx(0.3)
