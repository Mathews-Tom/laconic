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

from laconic.ledger import DuplicateCompactionError, Ledger, ObservationKind
from laconic.residency import (
    CompactionDecision,
    ResidencyManager,
    ResidencyMode,
    breakeven_turns,
    estimate_remaining_turns,
)

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
    prefix_after=st.integers(min_value=0, max_value=10_000_000),
    shrink=st.integers(min_value=0, max_value=10_000_000),
    projected_turns=st.one_of(st.none(), st.integers(min_value=0, max_value=10_000)),
)
def test_append_only_never_mutates_a_prefix_entry_for_any_input(
    tmp_path_factory: pytest.TempPathFactory,
    turn: int,
    prefix_after: int,
    shrink: int,
    projected_turns: int | None,
) -> None:
    """Property version: no generated input opens a write path to
    ``observations``, including ones an operator might plausibly pass by
    mistake (a shrinking or an unchanged prefix — a growing one is rejected
    outright, and is covered separately)."""
    prefix_before = prefix_after + shrink
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


# --- Opt-in compaction with audit logging -----------------------------------


def _compaction_rows(db_path: Path) -> list[tuple[object, ...]]:
    db = sqlite3.connect(db_path)
    try:
        return db.execute(
            "SELECT turn, prefix_before, prefix_after, breakeven_turns, projected_turns, "
            "accepted, applied, reason FROM compactions ORDER BY turn"
        ).fetchall()
    finally:
        db.close()


def test_compaction_is_accepted_once_projected_turns_clears_breakeven(populated_db: Path) -> None:
    """breakeven_turns(40_000, 60_000) is ~8.3; 9 projected turns clears it."""
    with Ledger(populated_db, "s1") as ledger:
        manager = ResidencyManager(ledger, mode=ResidencyMode.COMPACT)
        decision = manager.evaluate_compaction(
            turn=1, prefix_before=100_000, prefix_after=40_000, projected_turns=9
        )
    assert decision.accepted is True
    assert "clears" in decision.reason


def test_compaction_is_declined_below_breakeven(populated_db: Path) -> None:
    """8 projected turns does not clear the ~8.3-turn break-even."""
    with Ledger(populated_db, "s1") as ledger:
        manager = ResidencyManager(ledger, mode=ResidencyMode.COMPACT)
        decision = manager.evaluate_compaction(
            turn=1, prefix_before=100_000, prefix_after=40_000, projected_turns=8
        )
    assert decision.accepted is False
    assert "below" in decision.reason


def test_compaction_accepts_exactly_at_the_breakeven_boundary(populated_db: Path) -> None:
    """``docs/system-design.md`` §2.3's flowchart accepts at ``<=``, not only
    strictly below: 90,000/40,000 has an exact 10.0-turn break-even."""
    with Ledger(populated_db, "s1") as ledger:
        manager = ResidencyManager(ledger, mode=ResidencyMode.COMPACT)
        at_breakeven = manager.evaluate_compaction(
            turn=1, prefix_before=90_000, prefix_after=40_000, projected_turns=10
        )
        just_below = manager.evaluate_compaction(
            turn=2, prefix_before=90_000, prefix_after=40_000, projected_turns=9
        )
    assert at_breakeven.breakeven_turns == pytest.approx(10.0)
    assert at_breakeven.accepted is True
    assert just_below.accepted is False


def test_compaction_is_declined_without_a_session_length_estimate(populated_db: Path) -> None:
    """Even arithmetic that would clearly pay off must not be guessed."""
    with Ledger(populated_db, "s1") as ledger:
        manager = ResidencyManager(ledger, mode=ResidencyMode.COMPACT)
        decision = manager.evaluate_compaction(
            turn=1, prefix_before=1_000_000, prefix_after=1_000, projected_turns=None
        )
    assert decision.accepted is False
    assert decision.reason == "no session-length estimate available"
    [row] = _compaction_rows(populated_db)
    assert row[4:7] == (None, 0, 0)  # projected_turns, accepted, applied


def test_compaction_rejects_a_prefix_that_would_grow(populated_db: Path) -> None:
    """Not a compaction at all — a caller bug, not something to decline."""
    with Ledger(populated_db, "s1") as ledger:
        with pytest.raises(ValueError, match="prefix_after"):
            ResidencyManager(ledger).evaluate_compaction(1, 10, 100, None)


def test_compaction_never_mutates_an_existing_prefix_entry(populated_db: Path) -> None:
    """The append-only invariant holds for compact mode too — M7 decides and
    logs; it never applies. Applying inside a live session is M12's."""
    before = _observations_snapshot(populated_db)
    with Ledger(populated_db, "s1") as ledger:
        manager = ResidencyManager(ledger, mode=ResidencyMode.COMPACT)
        manager.evaluate_compaction(
            turn=1, prefix_before=100_000, prefix_after=40_000, projected_turns=9
        )
    assert _observations_snapshot(populated_db) == before


@settings(deadline=None, max_examples=100)
@given(
    turn=st.integers(min_value=0, max_value=10_000),
    prefix_after=st.integers(min_value=0, max_value=10_000_000),
    shrink=st.integers(min_value=0, max_value=10_000_000),
    projected_turns=st.one_of(st.none(), st.integers(min_value=0, max_value=10_000)),
)
def test_compact_mode_never_mutates_a_prefix_entry_for_any_input(
    tmp_path_factory: pytest.TempPathFactory,
    turn: int,
    prefix_after: int,
    shrink: int,
    projected_turns: int | None,
) -> None:
    """The immutability property, widened to compact mode's accept path —
    exactly where a future "apply the compaction" bug (the M7/M12 boundary)
    would land. Accepting only ever writes to ``compactions``."""
    prefix_before = prefix_after + shrink
    db_path = tmp_path_factory.mktemp("residency-compact") / "ledger.db"
    with Ledger(db_path, "s1") as ledger:
        ledger.register(ObservationKind.FILE, "a.py", "body", "F1", 1)
    before = _observations_snapshot(db_path)
    with Ledger(db_path, "s1") as ledger:
        manager = ResidencyManager(ledger, mode=ResidencyMode.COMPACT)
        manager.evaluate_compaction(turn, prefix_before, prefix_after, projected_turns)
    assert _observations_snapshot(db_path) == before


def test_every_compaction_decision_is_logged_with_its_arithmetic(populated_db: Path) -> None:
    """Accept and decline both leave a reconstructible audit row."""
    with Ledger(populated_db, "s1") as ledger:
        manager = ResidencyManager(ledger, mode=ResidencyMode.COMPACT)
        manager.evaluate_compaction(
            turn=1, prefix_before=100_000, prefix_after=40_000, projected_turns=9
        )
        manager.evaluate_compaction(
            turn=2, prefix_before=100_000, prefix_after=40_000, projected_turns=1
        )
    accepted_row, declined_row = _compaction_rows(populated_db)
    assert accepted_row[0:3] == (1, 100_000, 40_000)
    assert (accepted_row[5], accepted_row[6]) == (1, 0)  # accepted, never applied by M7
    assert declined_row[0:3] == (2, 100_000, 40_000)
    assert (declined_row[5], declined_row[6]) == (0, 0)  # declined


def test_distinct_decline_reasons_produce_distinguishable_audit_rows(populated_db: Path) -> None:
    """Regression guard: three declines for three different reasons must not
    collapse onto identical rows — the entire point of persisting ``reason``
    (``DEVELOPMENT_PLAN.md`` §6 M7: "declined attempts with reasons")."""
    with Ledger(populated_db, "s1") as ledger:
        ResidencyManager(ledger).evaluate_compaction(  # append-only mode
            turn=1, prefix_before=100_000, prefix_after=40_000, projected_turns=9
        )
        compact = ResidencyManager(ledger, mode=ResidencyMode.COMPACT)
        compact.evaluate_compaction(
            turn=2, prefix_before=100_000, prefix_after=40_000, projected_turns=None
        )
        compact.evaluate_compaction(
            turn=3, prefix_before=100_000, prefix_after=40_000, projected_turns=1
        )
    reasons = {row[7] for row in _compaction_rows(populated_db)}
    assert len(reasons) == 3


def test_an_append_only_decision_is_also_logged(populated_db: Path) -> None:
    """The default mode's declines are auditable too, not just compact mode's."""
    with Ledger(populated_db, "s1") as ledger:
        ResidencyManager(ledger).evaluate_compaction(
            turn=7, prefix_before=100_000, prefix_after=40_000, projected_turns=9
        )
    [row] = _compaction_rows(populated_db)
    turn, prefix_before, prefix_after, breakeven, projected_turns, accepted, applied, reason = row
    assert (turn, prefix_before, prefix_after, projected_turns, accepted, applied) == (
        7,
        100_000,
        40_000,
        9,
        0,
        0,
    )
    assert breakeven == pytest.approx(8.333333333333334)
    assert reason == "append-only mode: compaction is opt-in and not enabled"


def test_a_second_decision_for_the_same_turn_raises(populated_db: Path) -> None:
    """One verdict per session and turn; a repeat must not silently overwrite it."""
    with Ledger(populated_db, "s1") as ledger:
        manager = ResidencyManager(ledger, mode=ResidencyMode.COMPACT)
        manager.evaluate_compaction(
            turn=1, prefix_before=100_000, prefix_after=40_000, projected_turns=9
        )
        with pytest.raises(DuplicateCompactionError, match="turn 1"):
            manager.evaluate_compaction(
                turn=1, prefix_before=200_000, prefix_after=40_000, projected_turns=9
            )


# --- Session-length estimation ----------------------------------------------


def test_estimate_remaining_turns_of_an_empty_distribution_is_unavailable() -> None:
    assert estimate_remaining_turns(current_turn=5, session_lengths=[]) is None


def test_estimate_remaining_turns_uses_the_median_session_length() -> None:
    assert estimate_remaining_turns(current_turn=10, session_lengths=[20, 30, 40]) == 20


def test_estimate_remaining_turns_is_unavailable_past_the_median() -> None:
    """A session already longer than the typical one has nothing left to
    project, so this declines rather than returning zero or a negative
    number a caller might otherwise pass straight into a compaction check."""
    assert estimate_remaining_turns(current_turn=100, session_lengths=[20, 30, 40]) is None


def test_estimate_remaining_turns_floors_a_fractional_median() -> None:
    """An even-length distribution's median is a .5 value; this floors
    rather than rounds, so it never over-projects the session."""
    assert estimate_remaining_turns(current_turn=0, session_lengths=[9, 10]) == 9


def test_estimate_remaining_turns_rejects_a_negative_turn() -> None:
    with pytest.raises(ValueError, match="current_turn"):
        estimate_remaining_turns(current_turn=-1, session_lengths=[10])


def test_estimate_remaining_turns_rejects_a_non_positive_session_length() -> None:
    with pytest.raises(ValueError, match="session lengths"):
        estimate_remaining_turns(current_turn=1, session_lengths=[10, 0])


def test_a_compaction_decision_uses_the_estimated_remaining_turns(populated_db: Path) -> None:
    """The estimator's output feeds evaluate_compaction end to end."""
    estimate = estimate_remaining_turns(current_turn=1, session_lengths=[10, 10, 10])
    with Ledger(populated_db, "s1") as ledger:
        manager = ResidencyManager(ledger, mode=ResidencyMode.COMPACT)
        decision = manager.evaluate_compaction(
            turn=1, prefix_before=100_000, prefix_after=40_000, projected_turns=estimate
        )
    assert decision.projected_turns == 9
    assert decision.accepted is True


# --- Dry-run reporting -------------------------------------------------------


def test_dryrun_reports_the_same_verdict_as_evaluate_compaction(populated_db: Path) -> None:
    with Ledger(populated_db, "s1") as ledger:
        manager = ResidencyManager(ledger, mode=ResidencyMode.COMPACT)
        preview = manager.dry_run(
            turn=1, prefix_before=100_000, prefix_after=40_000, projected_turns=9
        )
        real = manager.evaluate_compaction(
            turn=1, prefix_before=100_000, prefix_after=40_000, projected_turns=9
        )
    assert preview == real
    assert preview.accepted is True
    assert preview.breakeven_turns == pytest.approx(breakeven_turns(40_000, 60_000))
    assert "clears" in preview.reason


def test_dryrun_reports_a_would_be_decline_identically_to_the_real_decision(
    populated_db: Path,
) -> None:
    with Ledger(populated_db, "s1") as ledger:
        manager = ResidencyManager(ledger, mode=ResidencyMode.COMPACT)
        preview = manager.dry_run(
            turn=1, prefix_before=100_000, prefix_after=40_000, projected_turns=8
        )
        real = manager.evaluate_compaction(
            turn=1, prefix_before=100_000, prefix_after=40_000, projected_turns=8
        )
    assert preview == real
    assert preview.accepted is False


def test_dryrun_writes_no_audit_row(populated_db: Path) -> None:
    """Even an accepted preview leaves the compactions table untouched."""
    with Ledger(populated_db, "s1") as ledger:
        manager = ResidencyManager(ledger, mode=ResidencyMode.COMPACT)
        manager.dry_run(turn=1, prefix_before=100_000, prefix_after=40_000, projected_turns=9)
    assert _compaction_rows(populated_db) == []


def test_dryrun_never_mutates_an_existing_prefix_entry(populated_db: Path) -> None:
    before = _observations_snapshot(populated_db)
    with Ledger(populated_db, "s1") as ledger:
        manager = ResidencyManager(ledger, mode=ResidencyMode.COMPACT)
        manager.dry_run(turn=1, prefix_before=100_000, prefix_after=40_000, projected_turns=9)
    assert _observations_snapshot(populated_db) == before


def test_dryrun_in_append_only_mode_still_shows_the_arithmetic(populated_db: Path) -> None:
    """A preview stays informative even when the mode alone forces a decline."""
    with Ledger(populated_db, "s1") as ledger:
        preview = ResidencyManager(ledger).dry_run(
            turn=1, prefix_before=100_000, prefix_after=40_000, projected_turns=9
        )
    assert preview.accepted is False
    assert preview.breakeven_turns == pytest.approx(breakeven_turns(40_000, 60_000))


def test_dryrun_followed_by_the_real_decision_still_logs_exactly_once(populated_db: Path) -> None:
    """A preview must not consume the one audit slot a turn gets."""
    with Ledger(populated_db, "s1") as ledger:
        manager = ResidencyManager(ledger, mode=ResidencyMode.COMPACT)
        manager.dry_run(turn=1, prefix_before=100_000, prefix_after=40_000, projected_turns=9)
        manager.evaluate_compaction(
            turn=1, prefix_before=100_000, prefix_after=40_000, projected_turns=9
        )
    assert len(_compaction_rows(populated_db)) == 1
