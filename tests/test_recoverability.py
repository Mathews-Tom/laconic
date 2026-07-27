"""The recoverability invariant, property-based.

Compression is lossy in presentation and lossless in reach: whatever an
encoder removes from what the model sees must still come back byte-exactly
through the ledger. Every property here is generated rather than hand-picked,
because a recoverability hole is a silent correctness bug in every encoder
built on top of this store.

Payloads range over every code point, including lone surrogates, since raw
content is stored as compressed bytes. Subjects and encodings stay inside
UTF-8: they are SQL ``TEXT`` columns and a surrogate there is rejected at the
driver, loudly, at write time.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from laconic.ledger import Ledger, ObservationKind, UnknownHandleError

#: Text over every code point, which exercises the storage bijection but is
#: single-line about 98% of the time: a newline is one draw out of a million.
PAYLOADS = st.text(st.characters(), max_size=2_000)

#: Text with a line structure, so span arithmetic is actually reached. Both
#: strategies feed the span properties; neither alone is enough.
LINES = st.text(st.characters(exclude_characters="\n"), max_size=60)
LINED_PAYLOADS = st.lists(LINES, min_size=1, max_size=12).map("\n".join)
SPANNED = st.one_of(LINED_PAYLOADS, PAYLOADS)

UTF8_TEXT = st.text(st.characters(codec="utf-8"), max_size=200)
KINDS = st.sampled_from(list(ObservationKind))
TURNS = st.integers(min_value=0, max_value=10_000)
OBSERVATIONS = st.tuples(KINDS, UTF8_TEXT, PAYLOADS, UTF8_TEXT, TURNS)

INVARIANT = settings(deadline=None, max_examples=200)


def memory_ledger() -> Ledger:
    """A throwaway ledger. Hypothesis needs a fresh one per example."""
    return Ledger(":memory:", "s1")


@INVARIANT
@given(kind=KINDS, subject=UTF8_TEXT, raw=PAYLOADS, encoded=UTF8_TEXT, turn=TURNS)
def test_a_bare_handle_expands_to_exactly_what_was_registered(
    kind: ObservationKind, subject: str, raw: str, encoded: str, turn: int
) -> None:
    with memory_ledger() as ledger:
        record = ledger.register(kind, subject, raw, encoded, turn)
        assert ledger.expand(record.handle) == raw


@INVARIANT
@given(raw=SPANNED, data=st.data())
def test_a_span_expands_to_exactly_those_lines(raw: str, data: st.DataObject) -> None:
    lines = raw.split("\n")
    first = data.draw(st.integers(min_value=1, max_value=len(lines)))
    last = data.draw(st.integers(min_value=first, max_value=len(lines)))
    with memory_ledger() as ledger:
        record = ledger.register(ObservationKind.FILE, "a.py", raw, "outline", 1)
        expanded = ledger.expand(f"{record.handle}:{first}-{last}")
    assert expanded.split("\n") == lines[first - 1 : last]


@INVARIANT
@given(raw=SPANNED, data=st.data())
def test_two_adjacent_spans_reassemble_the_payload(raw: str, data: st.DataObject) -> None:
    """Nothing falls between the spans, and nothing is duplicated across them."""
    lines = raw.split("\n")
    cut = data.draw(st.integers(min_value=1, max_value=len(lines)))
    with memory_ledger() as ledger:
        handle = ledger.register(ObservationKind.FILE, "a.py", raw, "outline", 1).handle
        head = ledger.expand(f"{handle}:1-{cut}")
        if cut == len(lines):
            assert head == raw
            return
        tail = ledger.expand(f"{handle}:{cut + 1}-{len(lines)}")
    assert f"{head}\n{tail}" == raw


@INVARIANT
@given(raw=SPANNED)
def test_every_line_is_individually_recoverable(raw: str) -> None:
    with memory_ledger() as ledger:
        handle = ledger.register(ObservationKind.FILE, "a.py", raw, "outline", 1).handle
        lines = [
            ledger.expand(f"{handle}:{number}-{number}") for number in range(1, raw.count("\n") + 2)
        ]
    assert "\n".join(lines) == raw


@INVARIANT
@given(observations=st.lists(OBSERVATIONS, min_size=1, max_size=12))
def test_every_handle_in_a_session_recovers_its_own_payload(
    observations: list[tuple[ObservationKind, str, str, str, int]],
) -> None:
    """Later registrations must not overwrite or leak into earlier handles."""
    with memory_ledger() as ledger:
        registered = [ledger.register(*observation) for observation in observations]
        for record in registered:
            assert ledger.expand(record.handle) == record.raw
        for record, observation in zip(registered, observations, strict=True):
            # A reused handle carries the payload it was minted for, which is
            # byte-identical to the one that was re-observed.
            assert record.raw == observation[2]


@INVARIANT
@given(observations=st.lists(OBSERVATIONS, min_size=1, max_size=8))
def test_recovery_survives_closing_and_reopening_the_ledger(
    tmp_path_factory: pytest.TempPathFactory,
    observations: list[tuple[ObservationKind, str, str, str, int]],
) -> None:
    db_path = tmp_path_factory.mktemp("ledger") / "ledger.db"
    with Ledger(db_path, "s1") as ledger:
        expected = {
            record.handle: record.raw
            for record in (ledger.register(*observation) for observation in observations)
        }
    with Ledger(db_path, "s1") as reopened:
        for handle, raw in expected.items():
            assert reopened.expand(handle) == raw


@INVARIANT
@given(raw=PAYLOADS, handle=st.text(st.characters(codec="utf-8"), max_size=8))
def test_an_unminted_handle_always_raises(raw: str, handle: str) -> None:
    with memory_ledger() as ledger:
        minted = ledger.register(ObservationKind.FILE, "a.py", raw, "outline", 1).handle
        unminted = handle.partition(":")[0]
        if unminted == minted:
            return
        with pytest.raises(UnknownHandleError):
            ledger.expand(unminted)


def test_the_invariant_holds_for_a_realistic_source_file() -> None:
    """One concrete case, so the suite reads as well as it generates."""
    raw = Path(__file__).read_text()
    with memory_ledger() as ledger:
        handle = ledger.register(ObservationKind.FILE, __file__, raw, "outline", 1).handle
        assert ledger.expand(handle) == raw
        assert ledger.expand(f"{handle}:1-1") == raw.split("\n")[0]
