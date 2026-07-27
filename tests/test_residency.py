"""Residency manager: break-even arithmetic, append-only default, gated
compaction with audit logging, and dry-run preview. ``docs/system-design.md``
§2.3, §5.1, §9.4 and ``docs/overview.md`` §3.3.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from laconic.ledger import Ledger, ObservationKind
from laconic.residency import CompactionDecision, ResidencyManager, breakeven_turns

# --- Break-even arithmetic --------------------------------------------------


@pytest.mark.parametrize(
    ("prefix_after", "delta", "expected"),
    [
        (40_000, 60_000, 8.3),
        (60_000, 40_000, 18.8),
        (80_000, 20_000, 50.0),
    ],
)
def test_breakeven_reproduces_the_documented_table(
    prefix_after: int, delta: int, expected: float
) -> None:
    """``docs/system-design.md`` §2.3's three-row table, exactly."""
    assert round(breakeven_turns(prefix_after, delta), 1) == expected


def test_breakeven_reduces_to_the_published_closed_form() -> None:
    """12.5 * prefix_after / delta at the current cache pricing."""
    assert breakeven_turns(60_000, 40_000) == pytest.approx(12.5 * 60_000 / 40_000)


@pytest.mark.parametrize("delta", [0, -1, -60_000])
def test_breakeven_of_a_non_shrinking_rewrite_is_infinite(delta: int) -> None:
    """A rewrite that does not shrink the prefix never pays for itself."""
    assert breakeven_turns(40_000, delta) == float("inf")


def test_breakeven_scales_linearly_with_prefix_after() -> None:
    assert breakeven_turns(80_000, 40_000) == pytest.approx(2 * breakeven_turns(40_000, 40_000))


def test_breakeven_scales_inversely_with_delta() -> None:
    assert breakeven_turns(40_000, 80_000) == pytest.approx(breakeven_turns(40_000, 40_000) / 2)


# --- Append-only default policy ---------------------------------------------


def _observations_snapshot(db_path: Path) -> list[tuple[object, ...]]:
    """Read every column of every observation row, as an outside observer."""
    db = sqlite3.connect(db_path)
    try:
        return db.execute("SELECT * FROM observations ORDER BY rowid").fetchall()
    finally:
        db.close()


@pytest.fixture
def populated_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "ledger.db"
    with Ledger(db_path, "s1") as ledger:
        ledger.register(ObservationKind.FILE, "src/a.py", "line one\n" * 400, "F1 outline", 1)
        ledger.register(ObservationKind.COMMAND, "pytest -q", "collected 3 items", "B1", 2)
    return db_path


def test_append_only_declines_regardless_of_the_arithmetic(populated_db: Path) -> None:
    """Even inputs that clear break-even do not flip a default-mode verdict."""
    with Ledger(populated_db, "s1") as ledger:
        manager = ResidencyManager(ledger)
        decision = manager.evaluate_compaction(
            turn=10, prefix_before=100_000, prefix_after=40_000, projected_turns=1_000
        )
    assert decision.accepted is False
    assert "append-only" in decision.reason


def test_append_only_still_reports_the_breakeven_arithmetic(populated_db: Path) -> None:
    """A decline is still auditable: the arithmetic is computed, not omitted."""
    with Ledger(populated_db, "s1") as ledger:
        manager = ResidencyManager(ledger)
        decision = manager.evaluate_compaction(
            turn=1, prefix_before=100_000, prefix_after=40_000, projected_turns=None
        )
    assert decision.breakeven_turns == pytest.approx(breakeven_turns(40_000, 60_000))


def test_append_only_never_mutates_an_existing_prefix_entry(populated_db: Path) -> None:
    """The acceptance line, checked directly against the stored rows."""
    before = _observations_snapshot(populated_db)
    with Ledger(populated_db, "s1") as ledger:
        ResidencyManager(ledger).evaluate_compaction(
            turn=99, prefix_before=200_000, prefix_after=10_000, projected_turns=500
        )
    assert _observations_snapshot(populated_db) == before


@settings(deadline=None, max_examples=100)
@given(
    turn=st.integers(min_value=0, max_value=10_000),
    prefix_before=st.integers(min_value=0, max_value=10_000_000),
    prefix_after=st.integers(min_value=0, max_value=10_000_000),
    projected_turns=st.one_of(st.none(), st.integers(min_value=0, max_value=10_000)),
)
def test_append_only_never_mutates_a_prefix_entry_for_any_input(
    tmp_path_factory: pytest.TempPathFactory,
    turn: int,
    prefix_before: int,
    prefix_after: int,
    projected_turns: int | None,
) -> None:
    """Property version: no generated input opens a write path to
    ``observations``, including ones an operator might plausibly pass by
    mistake (a shrinking, a growing, or an unchanged prefix)."""
    db_path = tmp_path_factory.mktemp("residency") / "ledger.db"
    with Ledger(db_path, "s1") as ledger:
        ledger.register(ObservationKind.FILE, "a.py", "body", "F1", 1)
    before = _observations_snapshot(db_path)
    with Ledger(db_path, "s1") as ledger:
        manager = ResidencyManager(ledger)
        manager.evaluate_compaction(turn, prefix_before, prefix_after, projected_turns)
    assert _observations_snapshot(db_path) == before


def test_append_only_rejects_a_negative_prefix_size(populated_db: Path) -> None:
    with Ledger(populated_db, "s1") as ledger:
        with pytest.raises(ValueError, match="prefix_before"):
            ResidencyManager(ledger).evaluate_compaction(1, -1, 0, None)


def test_append_only_rejects_a_negative_turn(populated_db: Path) -> None:
    with Ledger(populated_db, "s1") as ledger:
        with pytest.raises(ValueError, match="turn"):
            ResidencyManager(ledger).evaluate_compaction(-1, 100, 50, None)


def test_a_compaction_decision_is_frozen(populated_db: Path) -> None:
    with Ledger(populated_db, "s1") as ledger:
        decision = ResidencyManager(ledger).evaluate_compaction(1, 100, 50, None)
    with pytest.raises(AttributeError):
        decision.accepted = True  # type: ignore[misc]


def test_a_compaction_decisions_delta_is_before_minus_after() -> None:
    decision = CompactionDecision(
        turn=1,
        prefix_before=100,
        prefix_after=40,
        breakeven_turns=1.0,
        projected_turns=None,
        accepted=False,
        reason="x",
    )
    assert decision.delta == 60
