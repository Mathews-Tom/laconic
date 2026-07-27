"""The file observation encoder: span resolution and the encoder itself.

Test names carry the ``-k`` selector the milestone's PLANNED STACK uses to
scope each PR's own verification: ``span`` for
:mod:`laconic.codec.span`, ``file`` for
:mod:`laconic.codec.encoders.file`.
"""

from __future__ import annotations

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from laconic.codec.encoders.file import FileEncoder
from laconic.codec.outline import Outline, Symbol
from laconic.codec.span import (
    DEFAULT_SPAN_BUDGET,
    InvalidRangeError,
    LineRange,
    resolve_span,
)
from laconic.ledger import Ledger, ObservationKind

PROPERTY = settings(deadline=None, max_examples=200)

_NO_SYMBOLS = Outline(subject="f.txt", symbols=(), grammar=None)
_WITH_SYMBOLS = Outline(
    subject="f.py",
    symbols=(Symbol("foo", "function", 3, 5),),
    grammar="python",
)


# --- LineRange -----------------------------------------------------------


def test_line_range_rejects_a_start_below_one() -> None:
    with pytest.raises(InvalidRangeError, match="start"):
        LineRange(0, 1)


def test_line_range_rejects_an_end_before_the_start() -> None:
    with pytest.raises(InvalidRangeError, match="end"):
        LineRange(5, 4)


def test_line_range_allows_a_single_line() -> None:
    assert LineRange(3, 3) == LineRange(3, 3)


def test_line_range_union_covers_both_ranges() -> None:
    assert LineRange(10, 20).union(LineRange(5, 15)) == LineRange(5, 20)


def test_line_range_union_of_disjoint_ranges_covers_the_gap_between_them() -> None:
    assert LineRange(1, 3).union(LineRange(10, 12)) == LineRange(1, 12)


# --- resolve_span: no explicit request ------------------------------------


def test_span_resolution_defers_to_the_outline_when_it_has_symbols() -> None:
    assert resolve_span({}, _WITH_SYMBOLS, total_lines=100) == ()


def test_span_resolution_falls_back_to_a_bounded_window_with_no_symbols() -> None:
    ranges = resolve_span({}, _NO_SYMBOLS, total_lines=500)
    assert ranges == (LineRange(1, DEFAULT_SPAN_BUDGET),)


def test_span_resolution_fallback_window_never_exceeds_the_file() -> None:
    ranges = resolve_span({}, _NO_SYMBOLS, total_lines=10)
    assert ranges == (LineRange(1, 10),)


def test_span_resolution_fallback_window_respects_a_custom_budget() -> None:
    ranges = resolve_span({}, _NO_SYMBOLS, total_lines=500, span_budget=20)
    assert ranges == (LineRange(1, 20),)


def test_span_resolution_of_an_empty_file_is_empty_regardless_of_the_outline() -> None:
    assert resolve_span({}, _NO_SYMBOLS, total_lines=0) == ()
    assert resolve_span({}, _WITH_SYMBOLS, total_lines=0) == ()


# --- resolve_span: explicit offset/limit ------------------------------------


def test_span_resolution_honors_an_explicit_offset_and_limit() -> None:
    request = {"offset": 10, "limit": 5}
    assert resolve_span(request, _WITH_SYMBOLS, total_lines=100) == (LineRange(10, 14),)


def test_span_resolution_honors_an_explicit_request_even_when_the_outline_suffices() -> None:
    """The rationale in ``docs/system-design.md`` §2.2 is that outline-only
    encoding is a *savings*, not a rule: an explicit request always wins."""
    request = {"offset": 1, "limit": 3}
    assert resolve_span(request, _WITH_SYMBOLS, total_lines=100) == (LineRange(1, 3),)


def test_span_resolution_with_only_an_offset_reads_to_end_of_file() -> None:
    request = {"offset": 40}
    assert resolve_span(request, _NO_SYMBOLS, total_lines=50) == (LineRange(40, 50),)


def test_span_resolution_with_only_a_limit_defaults_the_offset_to_one() -> None:
    request = {"limit": 5}
    assert resolve_span(request, _NO_SYMBOLS, total_lines=50) == (LineRange(1, 5),)


def test_span_resolution_clamps_a_limit_larger_than_the_file() -> None:
    """The corpus's own ``Read`` calls commonly ask for ``limit: 120`` against
    files far shorter than that; the request is not malformed, the file is
    just small."""
    request = {"path": "f.py", "limit": 120}
    assert resolve_span(request, _NO_SYMBOLS, total_lines=18) == (LineRange(1, 18),)


def test_span_resolution_clamps_an_offset_plus_limit_reaching_past_eof() -> None:
    request = {"offset": 45, "limit": 20}
    assert resolve_span(request, _NO_SYMBOLS, total_lines=50) == (LineRange(45, 50),)


def test_span_resolution_treats_an_offset_past_eof_as_no_request() -> None:
    request = {"offset": 500}
    assert resolve_span(request, _WITH_SYMBOLS, total_lines=50) == ()
    assert resolve_span(request, _NO_SYMBOLS, total_lines=50) == (
        LineRange(1, min(DEFAULT_SPAN_BUDGET, 50)),
    )


@pytest.mark.parametrize("offset", [0, -1, "3", 3.0, None])
def test_span_resolution_ignores_a_malformed_offset(offset: object) -> None:
    request = {"offset": offset, "limit": 5}
    assert resolve_span(request, _WITH_SYMBOLS, total_lines=100) == ()


@pytest.mark.parametrize("limit", [0, -5, "3", 3.0, True])
def test_span_resolution_ignores_a_malformed_limit(limit: object) -> None:
    request = {"offset": 1, "limit": limit}
    assert resolve_span(request, _WITH_SYMBOLS, total_lines=100) == ()


def test_span_resolution_ignores_a_boolean_offset() -> None:
    """``bool`` is an ``int`` subclass in Python; ``True`` is not a line 1."""
    request = {"offset": True, "limit": 5}
    assert resolve_span(request, _WITH_SYMBOLS, total_lines=100) == ()


def test_span_resolution_never_raises_for_unrecognized_request_keys() -> None:
    request = {"path": "f.py", "recursive": True, "encoding": "utf-8"}
    assert resolve_span(request, _WITH_SYMBOLS, total_lines=100) == ()


# --- properties ---------------------------------------------------------


@PROPERTY
@given(
    offset=st.integers(min_value=1, max_value=1000), limit=st.integers(min_value=1, max_value=1000)
)
def test_span_resolution_result_never_exceeds_the_file(offset: int, limit: int) -> None:
    total_lines = 200
    ranges = resolve_span({"offset": offset, "limit": limit}, _NO_SYMBOLS, total_lines)
    for line_range in ranges:
        assert 1 <= line_range.start <= line_range.end <= total_lines


@PROPERTY
@given(total_lines=st.integers(min_value=0, max_value=2000))
def test_span_resolution_is_deterministic(total_lines: int) -> None:
    request = {"limit": 40}
    first = resolve_span(request, _NO_SYMBOLS, total_lines)
    second = resolve_span(request, _NO_SYMBOLS, total_lines)
    assert first == second


# --- FileEncoder ----------------------------------------------------------

PYTHON_SOURCE = "\n".join(f"def fn_{i}(x):\n    return x + {i}\n" for i in range(30)).rstrip("\n")

UNKNOWN_EXTENSION_SOURCE = "\n".join(f"line {i} of an unrecognized format" for i in range(300))


def memory_ledger() -> Ledger:
    return Ledger(":memory:", "s1")


def test_file_encoder_registers_the_observation_with_the_ledger() -> None:
    with memory_ledger() as ledger:
        encoder = FileEncoder(ledger)
        record = encoder.encode("f.py", PYTHON_SOURCE, {}, turn=0)
        assert record.kind == ObservationKind.FILE
        assert record.subject == "f.py"
        assert record.raw == PYTHON_SOURCE


def test_file_encoder_encoding_is_recoverable_via_the_ledger() -> None:
    with memory_ledger() as ledger:
        encoder = FileEncoder(ledger)
        record = encoder.encode("f.py", PYTHON_SOURCE, {}, turn=0)
        assert ledger.expand(record.handle) == PYTHON_SOURCE


def test_file_encoder_a_shown_span_matches_what_expand_returns_for_it() -> None:
    with memory_ledger() as ledger:
        encoder = FileEncoder(ledger)
        record = encoder.encode("f.py", PYTHON_SOURCE, {"offset": 4, "limit": 3}, turn=0)
        assert "span 4-6:" in record.encoded
        shown = record.encoded.rsplit("span 4-6:\n", 1)[1]
        de_indented = "\n".join(line.removeprefix("    ") for line in shown.split("\n"))
        assert de_indented == ledger.expand(f"{record.handle}:4-6")


def test_file_encoder_outline_alone_answers_when_no_span_is_requested() -> None:
    with memory_ledger() as ledger:
        encoder = FileEncoder(ledger)
        record = encoder.encode("f.py", PYTHON_SOURCE, {}, turn=0)
        assert "outline:" in record.encoded
        assert "span" not in record.encoded


def test_file_encoder_encoded_volume_is_materially_below_raw_for_a_whale_read() -> None:
    with memory_ledger() as ledger:
        encoder = FileEncoder(ledger)
        record = encoder.encode("f.py", PYTHON_SOURCE, {}, turn=0)
        assert record.encoded_chars < record.raw_chars * 0.5


def test_file_encoder_degrades_to_head_scoping_for_an_unrecognized_extension() -> None:
    with memory_ledger() as ledger:
        encoder = FileEncoder(ledger)
        record = encoder.encode("notes.xyz", UNKNOWN_EXTENSION_SOURCE, {}, turn=0)
        assert "outline:" not in record.encoded
        assert "span 1-" in record.encoded
        assert record.encoded_chars < record.raw_chars


def test_file_encoder_never_raises_for_an_unrecognized_extension() -> None:
    with memory_ledger() as ledger:
        FileEncoder(ledger).encode("notes.xyz", UNKNOWN_EXTENSION_SOURCE, {}, turn=0)


def test_file_encoder_is_deterministic_across_encoder_instances() -> None:
    with memory_ledger() as first_ledger, memory_ledger() as second_ledger:
        first = FileEncoder(first_ledger).encode("f.py", PYTHON_SOURCE, {}, turn=0)
        second = FileEncoder(second_ledger).encode("f.py", PYTHON_SOURCE, {}, turn=0)
        assert first.encoded == second.encoded


def test_file_encoder_handles_empty_content_without_raising() -> None:
    with memory_ledger() as ledger:
        record = FileEncoder(ledger).encode("empty.py", "", {}, turn=0)
        assert record.raw == ""
        assert ledger.expand(record.handle) == ""


def test_file_encoder_reuses_the_span_budget_for_fallback_files() -> None:
    with memory_ledger() as ledger:
        encoder = FileEncoder(ledger, span_budget=10)
        record = encoder.encode("notes.xyz", UNKNOWN_EXTENSION_SOURCE, {}, turn=0)
        assert "span 1-10:" in record.encoded


def test_file_encoder_never_raises_on_a_lone_surrogate() -> None:
    """Regression: a lone UTF-16 surrogate is a legal ``str`` code point but
    is not valid UTF-8. Embedding it verbatim into the presentational
    ``encoded`` text made ``Ledger.register`` raise ``ValueError`` from its
    storability check (``laconic.ledger._require_storable``), even though
    ``raw`` itself is stored losslessly via ``surrogatepass``. Hypothesis's
    default sampling at ``st.text(st.characters(), max_size=2000)`` rarely
    drew a lone surrogate, so the property test below passed without ever
    reaching this case — this concrete example and the pinned
    ``@example`` below guarantee it is exercised every run."""
    with memory_ledger() as ledger:
        record = FileEncoder(ledger).encode("f.py", "\ud800", {}, turn=0)
        assert "\ud800" not in record.encoded
        assert ledger.expand(record.handle) == "\ud800"


def test_file_encoder_never_raises_on_a_surrogate_inside_real_source() -> None:
    with memory_ledger() as ledger:
        raw = "def foo(x):\n    return x  # \udc00 stray low surrogate\n"
        record = FileEncoder(ledger).encode("f.py", raw, {"offset": 1, "limit": 2}, turn=0)
        assert "\udc00" not in record.encoded
        assert ledger.expand(record.handle) == raw


@PROPERTY
@example(raw="\ud800")
@example(raw="hello \udc00 world")
@given(raw=st.text(st.characters(), max_size=2000))
def test_file_encoder_never_raises_for_arbitrary_content(raw: str) -> None:
    with memory_ledger() as ledger:
        FileEncoder(ledger).encode("f.py", raw, {}, turn=0)


@PROPERTY
@given(raw=st.text(st.characters(codec="utf-8"), max_size=500))
def test_file_encoder_encoding_always_recovers_the_exact_raw_payload(raw: str) -> None:
    with memory_ledger() as ledger:
        record = FileEncoder(ledger).encode("f.py", raw, {}, turn=0)
        assert ledger.expand(record.handle) == raw
