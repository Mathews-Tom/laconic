"""Runtime decision persistence, invariants, migration, and privacy."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

import pytest

from laconic.ledger import (
    SCHEMA_VERSION,
    DuplicateRuntimeDecisionError,
    DuplicateRuntimeExpansionError,
    Ledger,
    ObservationKind,
)


def _record_emitted(ledger: Ledger, *, sequence: int = 1, request_id: str = "req-1") -> None:
    ledger.record_runtime_decision(
        sequence=sequence,
        request_id=request_id,
        tool_name="Read",
        outcome="emitted",
        reason="smaller_envelope",
        candidate_reference="session-1/F1",
        raw_chars=1_000,
        visible_chars=120,
        latency_ms=4.25,
        created_at=10.0,
    )


def test_emitted_and_pass_through_decisions_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with Ledger(path, "session-1") as ledger:
        _record_emitted(ledger)
        ledger.record_runtime_decision(
            sequence=2,
            request_id="req-2",
            tool_name="Bash",
            outcome="pass_through",
            reason="not_smaller",
            candidate_reference="session-1/B1",
            raw_chars=40,
            visible_chars=40,
            latency_ms=1.5,
            created_at=11.0,
        )

    with Ledger(path, "session-1") as reopened:
        decisions = reopened.runtime_decisions()

    assert [(item.sequence, item.outcome, item.reason) for item in decisions] == [
        (1, "emitted", "smaller_envelope"),
        (2, "pass_through", "not_smaller"),
    ]
    assert decisions[0].candidate_reference == "session-1/F1"
    assert decisions[1].candidate_reference == "session-1/B1"


def test_expansion_metrics_survive_reopen_and_reject_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with Ledger(path, "session-1") as ledger:
        ledger.record_runtime_expansion(
            request_id="expand-full",
            reference="session-1/F1",
            span=False,
            created_at=12.0,
        )
        ledger.record_runtime_expansion(
            request_id="expand-span",
            reference="session-1/F1:2-3",
            span=True,
            created_at=13.0,
        )
        with pytest.raises(DuplicateRuntimeExpansionError, match="expand-full"):
            ledger.record_runtime_expansion(
                request_id="expand-full",
                reference="session-1/F1",
                span=False,
            )

    with Ledger(path, "session-1") as reopened:
        assert reopened.runtime_expansion_counts() == (1, 1)


def test_runtime_decisions_are_scoped_to_the_bound_session(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with Ledger(path, "session-1") as first, Ledger(path, "session-2") as second:
        _record_emitted(first)
        _record_emitted(second)
        assert [item.request_id for item in first.runtime_decisions()] == ["req-1"]
        assert [item.request_id for item in second.runtime_decisions()] == ["req-1"]


def test_duplicate_sequence_or_request_id_fails_without_overwriting(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db", "session-1") as ledger:
        _record_emitted(ledger)
        with pytest.raises(DuplicateRuntimeDecisionError, match="sequence 1"):
            _record_emitted(ledger, request_id="different")
        with pytest.raises(DuplicateRuntimeDecisionError, match="request 'req-1'"):
            _record_emitted(ledger, sequence=2)

        assert len(ledger.runtime_decisions()) == 1


@pytest.mark.parametrize(
    ("outcome", "candidate_reference", "raw_chars", "visible_chars", "message"),
    [
        ("emitted", None, 100, 50, "requires a candidate reference"),
        ("emitted", "session-1/F1", 100, 100, "strictly smaller"),
        ("pass_through", None, 100, 99, "unchanged visible length"),
    ],
)
def test_invalid_runtime_decisions_leave_no_row(
    tmp_path: Path,
    outcome: Literal["emitted", "pass_through"],
    candidate_reference: str | None,
    raw_chars: int,
    visible_chars: int,
    message: str,
) -> None:
    with Ledger(tmp_path / "ledger.db", "session-1") as ledger:
        with pytest.raises(ValueError, match=message):
            ledger.record_runtime_decision(
                sequence=1,
                request_id="req-1",
                tool_name="Read",
                outcome=outcome,
                reason="test",
                candidate_reference=candidate_reference,
                raw_chars=raw_chars,
                visible_chars=visible_chars,
                latency_ms=1.0,
            )
        assert ledger.runtime_decisions() == ()


def test_runtime_decision_rows_never_store_raw_content_or_subjects(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    secret = "secret raw observation"
    with Ledger(path, "session-1") as ledger:
        ledger.register(ObservationKind.FILE, "private/path.py", secret, "encoded", 1)
        _record_emitted(ledger)

    with sqlite3.connect(path) as database:
        columns = [item[1] for item in database.execute("PRAGMA table_info(runtime_decisions)")]
        row = database.execute("SELECT * FROM runtime_decisions").fetchone()

    assert "raw" not in columns
    assert "subject" not in columns
    assert secret not in repr(row)
    assert "private/path.py" not in repr(row)


def test_a_v2_database_adds_runtime_decisions_without_losing_rows(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with Ledger(path, "session-1") as ledger:
        record = ledger.register(ObservationKind.FILE, "a.py", "exact raw", "encoded", 1)
    with sqlite3.connect(path) as database, database:
        database.execute("DROP TABLE runtime_decisions")
        database.execute("DROP TABLE runtime_expansions")
        database.execute("PRAGMA user_version = 2")

    with Ledger(path, "session-1") as migrated:
        assert migrated.expand(record.handle) == "exact raw"
        _record_emitted(migrated)
        assert migrated.runtime_expansion_counts() == (0, 0)

    with sqlite3.connect(path) as database:
        assert database.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert database.execute("SELECT count(*) FROM runtime_decisions").fetchone() == (1,)
