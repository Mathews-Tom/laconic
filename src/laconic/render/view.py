"""Read-only assembly of a structural trace from ledger observations."""

from __future__ import annotations

from dataclasses import dataclass

from laconic.ledger import Ledger, TraceRecord


@dataclass(frozen=True, slots=True)
class TraceEntry:
    """One structural observation with its one-based display turn."""

    turn: int
    record: TraceRecord


def assemble(ledger: Ledger, first_turn: int, last_turn: int) -> tuple[TraceEntry, ...]:
    """Assemble an inclusive, one-based display range without mutating ``ledger``.

    Ledger turns are zero-based because they are recorded as events arrive;
    people request turns as ``1-5`` at the CLI. The conversion lives here so
    every renderer receives a consistently numbered trace.
    """
    if first_turn < 1:
        raise ValueError(f"first turn must be at least 1: {first_turn}")
    if last_turn < first_turn:
        raise ValueError(f"last turn must be at least first turn: {last_turn} < {first_turn}")
    return tuple(
        TraceEntry(turn=record.turn + 1, record=record)
        for record in ledger.trace_records(first_turn - 1, last_turn - 1)
    )
