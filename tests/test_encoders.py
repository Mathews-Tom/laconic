"""The file observation encoder: span resolution and the encoder itself.

Test names carry the ``-k`` selector the milestone's PLANNED STACK uses to
scope each PR's own verification: ``span`` for
:mod:`laconic.codec.span`, ``file`` for
:mod:`laconic.codec.encoders.file`.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from laconic.codec.outline import Outline, Symbol
from laconic.codec.span import (
    DEFAULT_SPAN_BUDGET,
    InvalidRangeError,
    LineRange,
    resolve_span,
)

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
