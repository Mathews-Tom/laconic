"""Tests for deterministic renderer components."""

from __future__ import annotations

from pathlib import Path

import pytest

import laconic.ledger as ledger_module
from laconic.ledger import Ledger, ObservationKind
from laconic.render.view import assemble


def _ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger.db", "render")


def test_assembly_orders_records_by_turn_then_insertion(tmp_path: Path) -> None:
    with _ledger(tmp_path) as ledger:
        first = ledger.register(ObservationKind.FILE, "a.py", "alpha", "a.py", turn=0)
        second = ledger.register(ObservationKind.COMMAND, "pytest", "passed", "passed", turn=1)
        third = ledger.register(ObservationKind.SEARCH, "needle", "match", "match", turn=1)

        trace = assemble(ledger, 1, 2)

    assert [(entry.turn, entry.record.handle) for entry in trace] == [
        (1, first.handle),
        (2, second.handle),
        (2, third.handle),
    ]


def test_assembly_reads_without_mutating_ledger(tmp_path: Path) -> None:
    with _ledger(tmp_path) as ledger:
        record = ledger.register(ObservationKind.FILE, "a.py", "alpha", "a.py", turn=0)

        trace = assemble(ledger, 1, 1)

        assert ledger.get(record.handle) == record

    assert trace[0].record.handle == record.handle


def test_assembly_uses_stored_size_without_decompressing_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _ledger(tmp_path) as ledger:
        record = ledger.register(ObservationKind.FILE, "a.py", "alpha", "a.py", turn=0)

        def fail_decompression(_: bytes) -> str:
            raise AssertionError("trace assembly decompressed a raw payload")

        monkeypatch.setattr(ledger_module, "decompress_raw", fail_decompression)
        trace = assemble(ledger, 1, 1)

    assert trace[0].record.raw_chars == len(record.raw)


@pytest.mark.parametrize("first,last", [(0, 1), (2, 1)])
def test_assembly_rejects_invalid_display_ranges(tmp_path: Path, first: int, last: int) -> None:
    with _ledger(tmp_path) as ledger:
        with pytest.raises(ValueError):
            assemble(ledger, first, last)
