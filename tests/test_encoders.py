"""The file, fallback, and dispatch observation encoders, plus span
resolution.

Test names carry the ``-k`` selector the milestone's PLANNED STACK uses to
scope each PR's own verification: ``span`` for
:mod:`laconic.codec.span`, ``file`` for
:mod:`laconic.codec.encoders.file`, ``fallback`` for
:mod:`laconic.codec.encoders._elision` and
:mod:`laconic.codec.encoders.fallback` (M5 PR-1, which also introduces
:mod:`laconic.codec.observe`'s dispatch).
"""

from __future__ import annotations

import re
from itertools import groupby

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from laconic.codec.encoders._elision import (
    DEFAULT_KEEP_HEAD,
    DEFAULT_KEEP_TAIL,
    collapse_duplicate_lines,
    elide_middle,
    looks_like_error,
)
from laconic.codec.encoders.fallback import FallbackEncoder
from laconic.codec.encoders.file import FileEncoder
from laconic.codec.observe import ObservationCodec
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


# --- resolve_span: edit_target never-elide rule ---------------------------


def test_span_resolution_edit_target_is_never_elided_even_when_the_outline_suffices() -> None:
    request = {"edit_target": (3, 5)}
    assert resolve_span(request, _WITH_SYMBOLS, total_lines=100) == (LineRange(3, 5),)


def test_span_resolution_edit_target_is_shown_with_no_outline_and_no_other_request() -> None:
    request = {"edit_target": (200, 205)}
    assert resolve_span(request, _NO_SYMBOLS, total_lines=500) == (LineRange(200, 205),)


def test_span_resolution_edit_target_accepts_a_list_pair_too() -> None:
    request = {"edit_target": [3, 5]}
    assert resolve_span(request, _WITH_SYMBOLS, total_lines=100) == (LineRange(3, 5),)


def test_span_resolution_edit_target_unions_with_an_explicit_request() -> None:
    request = {"offset": 1, "limit": 3, "edit_target": (10, 12)}
    assert resolve_span(request, _WITH_SYMBOLS, total_lines=100) == (LineRange(1, 12),)


def test_span_resolution_edit_target_beyond_eof_is_clamped_not_dropped() -> None:
    request = {"edit_target": (48, 60)}
    assert resolve_span(request, _NO_SYMBOLS, total_lines=50) == (LineRange(48, 50),)


@pytest.mark.parametrize(
    "edit_target",
    [None, (), (1,), (1, 2, 3), (0, 5), (5, 2), ("a", "b"), "1-5"],
)
def test_span_resolution_ignores_a_malformed_edit_target(edit_target: object) -> None:
    request = {"edit_target": edit_target}
    assert resolve_span(request, _WITH_SYMBOLS, total_lines=100) == ()


@PROPERTY
@given(start=st.integers(min_value=1, max_value=300), end=st.integers(min_value=1, max_value=300))
def test_span_resolution_edit_target_is_always_covered_by_the_result(start: int, end: int) -> None:
    if end < start:
        start, end = end, start
    total_lines = 200
    request = {"edit_target": (start, end)}
    (resolved,) = resolve_span(request, _WITH_SYMBOLS, total_lines, span_budget=10)
    assert resolved.start <= min(start, total_lines)
    assert resolved.end >= min(end, total_lines)


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


def test_file_encoder_never_elides_a_declared_edit_target() -> None:
    with memory_ledger() as ledger:
        encoder = FileEncoder(ledger)
        # fn_10 sits around lines 31-32; asking for nothing but the outline
        # would normally elide it entirely.
        record = encoder.encode("f.py", PYTHON_SOURCE, {"edit_target": (31, 32)}, turn=0)
        assert "span 31-32:" in record.encoded
        assert "fn_10" in record.encoded


def test_file_encoder_edit_target_survives_alongside_a_fallback_file() -> None:
    with memory_ledger() as ledger:
        encoder = FileEncoder(ledger)
        record = encoder.encode(
            "notes.xyz", UNKNOWN_EXTENSION_SOURCE, {"edit_target": (250, 252)}, turn=0
        )
        assert "span 250-252:" in record.encoded


# --- collapse_duplicate_lines ---------------------------------------------


def test_collapse_duplicate_lines_of_empty_input_is_empty() -> None:
    assert collapse_duplicate_lines([]) == []


def test_collapse_duplicate_lines_leaves_a_run_with_no_duplicates_untouched() -> None:
    assert collapse_duplicate_lines(["a", "b", "c"]) == ["a", "b", "c"]


def test_collapse_duplicate_lines_marks_a_run_of_consecutive_duplicates() -> None:
    collapsed = collapse_duplicate_lines(["ok", "ok", "ok", "next"])
    assert collapsed == ["ok  [x3]", "next"]


def test_collapse_duplicate_lines_does_not_merge_non_adjacent_duplicates() -> None:
    """A repeat separated by unrelated output is two occurrences, not one."""
    collapsed = collapse_duplicate_lines(["ok", "busy", "ok"])
    assert collapsed == ["ok", "busy", "ok"]


_LABELS = st.sampled_from(["L0", "L1", "L2", "L3"])
_COUNT_SUFFIX = re.compile(r"  \[x\d+\]$")


@PROPERTY
@given(lines=st.lists(_LABELS, min_size=0, max_size=30))
def test_collapse_duplicate_lines_preserves_ordering_of_surviving_lines(lines: list[str]) -> None:
    collapsed = collapse_duplicate_lines(lines)
    survivors = [_COUNT_SUFFIX.sub("", line) for line in collapsed]
    assert survivors == [key for key, _ in groupby(lines)]


@PROPERTY
@given(lines=st.lists(_LABELS, min_size=0, max_size=30))
def test_collapse_duplicate_lines_never_grows_the_line_count(lines: list[str]) -> None:
    assert len(collapse_duplicate_lines(lines)) <= len(lines)


# --- looks_like_error -------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Traceback (most recent call last):",
        '  File "app.py", line 10, in <module>',
        "ValueError: bad input",
        "my.module.CustomError: something went wrong",
        "FAILED tests/test_x.py::test_y - AssertionError: assert 1 == 2",
        "ERROR tests/test_broken.py::test_fixture - ValueError: fixture blew up",
        "E       assert 1 == 2",
        "AssertionError: boom",
        "KeyboardInterrupt",
        "socket.timeout: timed out after 5s",
        "fatal: not a git repository",
        "panic: runtime error: index out of range",
        "error: could not compile crate",
        "error[E0308]: mismatched types",
        "npm ERR! code ELIFECYCLE",
        "Segmentation fault (core dumped)",
        "Permission denied",
        "make: *** [Makefile:12: all] Error 2",
        "undefined reference to `foo'",
        "stderr: connection refused",
        "  | Traceback (most recent call last):",
        '  |   File "/app/x.py", line 3, in <module>',
        "  | ExceptionGroup: eg (1 sub-exception)",
        "tests/test_x.py::test_thing FAILED  [ 50%]",
    ],
)
def test_looks_like_error_recognizes_every_protected_category(line: str) -> None:
    assert looks_like_error(line)


@pytest.mark.parametrize("line", ["build 1", "compile 2", "ok", "", "1,024 lines written"])
def test_looks_like_error_does_not_flag_ordinary_output(line: str) -> None:
    assert not looks_like_error(line)


# --- elide_middle -----------------------------------------------------------


def test_elide_middle_keeps_everything_verbatim_when_short() -> None:
    lines = [f"line {i}" for i in range(10)]
    result = elide_middle(lines, keep_head=40, keep_tail=40)
    assert result.elided is False
    assert result.text == "\n".join(lines)


def test_elide_middle_marks_the_elided_region() -> None:
    lines = [f"line {i}" for i in range(200)]
    result = elide_middle(lines, keep_head=5, keep_tail=5)
    assert result.elided is True
    assert "190 lines elided" in result.text
    assert "line 0" in result.text
    assert "line 199" in result.text


def test_elide_middle_extracts_an_error_from_the_elided_middle() -> None:
    lines = (
        [f"line {i}" for i in range(50)] + ["ValueError: boom"] + [f"line {i}" for i in range(50)]
    )
    result = elide_middle(lines, keep_head=5, keep_tail=5)
    assert "ValueError: boom" in result.text
    assert "error lines from the elided region" in result.text


def test_elide_middle_caps_the_shown_errors_and_reports_the_remainder() -> None:
    errors = [f"ValueError: boom {i}" for i in range(30)]
    lines = [f"line {i}" for i in range(20)] + errors + [f"line {i}" for i in range(20)]
    result = elide_middle(lines, keep_head=5, keep_tail=5, max_errors=10)
    for error in errors[:10]:
        assert error in result.text
    assert "further error lines omitted" in result.text


_FILLER_WORDS = (
    "build",
    "compile",
    "link",
    "package",
    "install",
    "download",
    "cache",
    "resolve",
    "index",
    "queue",
)
#: Deliberately DIFFERENT concrete strings than
#: ``test_looks_like_error_recognizes_every_protected_category``'s
#: parametrize list above, covering the same categories with different
#: instances — a property test whose corpus is identical to the
#: classifier's own design corpus proves only that the engine keeps what
#: the classifier flags, not that the classifier's coverage is right.
_ERROR_SAMPLES = (
    "Traceback (most recent call last):",
    '  File "/app/src/handler.py", line 42, in process',
    "ConnectionError: could not reach host",
    "FAILED tests/integration/test_api.py::test_retry - TimeoutError: request timed out",
    "E           AssertionError: expected 200, got 500",
    "fatal: unable to access repository",
    "error[E0433]: failed to resolve module",
    "npm ERR! code ENOENT",
    "Segmentation fault (core dumped)",
    "make: *** [Makefile:20: build] Error 1",
    "stderr: unexpected EOF",
)
_FILLER_LINE = st.builds(
    lambda word, n: f"{word} {n}",
    st.sampled_from(_FILLER_WORDS),
    st.integers(min_value=0, max_value=999),
)


@PROPERTY
@given(
    before=st.lists(_FILLER_LINE, min_size=0, max_size=150),
    after=st.lists(_FILLER_LINE, min_size=0, max_size=150),
    error=st.sampled_from(_ERROR_SAMPLES),
)
def test_elide_middle_never_drops_an_injected_error_line_at_any_position(
    before: list[str], after: list[str], error: str
) -> None:
    lines = [*before, error, *after]
    result = elide_middle(lines, keep_head=DEFAULT_KEEP_HEAD, keep_tail=DEFAULT_KEEP_TAIL)
    assert error in result.text


@PROPERTY
@given(lines=st.lists(_FILLER_LINE, min_size=0, max_size=300))
def test_elide_middle_never_raises_for_filler_only_input(lines: list[str]) -> None:
    elide_middle(lines)


@PROPERTY
@given(
    error_count=st.integers(min_value=1, max_value=30),
    max_errors=st.integers(min_value=1, max_value=30),
)
def test_elide_middle_shows_exactly_min_of_error_count_and_cap(
    error_count: int, max_errors: int
) -> None:
    errors = [f"ValueError: boom {i}" for i in range(error_count)]
    lines = [f"line {i}" for i in range(20)] + errors + [f"line {i}" for i in range(20)]
    result = elide_middle(lines, keep_head=5, keep_tail=5, max_errors=max_errors)
    shown = min(error_count, max_errors)
    for error in errors[:shown]:
        assert error in result.text
    if error_count > max_errors:
        assert "further error lines omitted" in result.text
    else:
        assert "further error lines omitted" not in result.text


# --- FallbackEncoder ---------------------------------------------------------


def test_fallback_encoder_registers_the_observation_under_the_other_kind() -> None:
    with memory_ledger() as ledger:
        record = FallbackEncoder(ledger).encode("SomeTool", "hello", {}, turn=0)
        assert record.kind == ObservationKind.OTHER
        assert record.handle.startswith("X")


def test_fallback_encoder_encoding_is_recoverable_via_the_ledger() -> None:
    with memory_ledger() as ledger:
        raw = "\n".join(f"line {i}" for i in range(200))
        record = FallbackEncoder(ledger).encode("SomeTool", raw, {}, turn=0)
        assert ledger.expand(record.handle) == raw


def test_fallback_encoder_elides_a_long_middle_with_a_marker() -> None:
    with memory_ledger() as ledger:
        raw = "\n".join(f"line {i}" for i in range(500))
        record = FallbackEncoder(ledger).encode("SomeTool", raw, {}, turn=0)
        assert "lines elided" in record.encoded
        assert record.encoded_chars < record.raw_chars


def test_fallback_encoder_never_elides_short_output() -> None:
    with memory_ledger() as ledger:
        raw = "hello\nworld"
        record = FallbackEncoder(ledger).encode("SomeTool", raw, {}, turn=0)
        assert record.encoded == raw


def test_fallback_encoder_is_deterministic_across_encoder_instances() -> None:
    with memory_ledger() as first_ledger, memory_ledger() as second_ledger:
        raw = "\n".join(f"line {i}" for i in range(300))
        first = FallbackEncoder(first_ledger).encode("SomeTool", raw, {}, turn=0)
        second = FallbackEncoder(second_ledger).encode("SomeTool", raw, {}, turn=0)
        assert first.encoded == second.encoded


@PROPERTY
@example(raw="\ud800")
@example(raw="hello \udc00 world")
@given(raw=st.text(st.characters(), max_size=2000))
def test_fallback_encoder_never_raises_for_arbitrary_content(raw: str) -> None:
    with memory_ledger() as ledger:
        FallbackEncoder(ledger).encode("SomeTool", raw, {}, turn=0)


@PROPERTY
@given(
    before=st.lists(_FILLER_LINE, min_size=0, max_size=150),
    after=st.lists(_FILLER_LINE, min_size=0, max_size=150),
    error=st.sampled_from(_ERROR_SAMPLES),
)
def test_fallback_encoder_never_drops_an_injected_error_line(
    before: list[str], after: list[str], error: str
) -> None:
    raw = "\n".join([*before, error, *after])
    with memory_ledger() as ledger:
        record = FallbackEncoder(ledger).encode("SomeTool", raw, {}, turn=0)
        assert error in record.encoded


def test_fallback_encoder_shows_a_non_zero_exit_header() -> None:
    with memory_ledger() as ledger:
        record = FallbackEncoder(ledger).encode("SomeTool", "done", {"exit_code": 101}, turn=0)
        assert record.encoded == "exit 101\ndone"


def test_fallback_encoder_omits_the_exit_header_when_exit_code_is_zero() -> None:
    with memory_ledger() as ledger:
        record = FallbackEncoder(ledger).encode("SomeTool", "done", {"exit_code": 0}, turn=0)
        assert record.encoded == "done"


@pytest.mark.parametrize("exit_code", [True, "1", 1.0, None])
def test_fallback_encoder_ignores_a_malformed_exit_code(exit_code: object) -> None:
    with memory_ledger() as ledger:
        record = FallbackEncoder(ledger).encode(
            "SomeTool", "done", {"exit_code": exit_code}, turn=0
        )
        assert record.encoded == "done"


@PROPERTY
@given(exit_code=st.integers(min_value=1, max_value=255))
def test_fallback_encoder_never_drops_a_non_zero_exit_code_regardless_of_elision(
    exit_code: int,
) -> None:
    raw = "\n".join(f"line {i}" for i in range(500))
    with memory_ledger() as ledger:
        record = FallbackEncoder(ledger).encode("SomeTool", raw, {"exit_code": exit_code}, turn=0)
        assert f"exit {exit_code}" in record.encoded


# --- ObservationCodec dispatch -----------------------------------------------


def test_observation_codec_dispatches_read_to_the_file_encoder() -> None:
    with memory_ledger() as ledger:
        record = ObservationCodec(ledger).encode("Read", "f.py", PYTHON_SOURCE, {}, turn=0)
        assert record.kind == ObservationKind.FILE


def test_observation_codec_dispatches_an_unrecognized_tool_to_the_fallback_encoder() -> None:
    with memory_ledger() as ledger:
        record = ObservationCodec(ledger).encode(
            "SomeFutureTool", "subject", "raw text", {}, turn=0
        )
        assert record.kind == ObservationKind.OTHER


@PROPERTY
@given(tool_name=st.text(max_size=30), raw=st.text(st.characters(), max_size=500))
def test_observation_codec_never_raises_for_an_unrecognized_tool_name(
    tool_name: str, raw: str
) -> None:
    with memory_ledger() as ledger:
        ObservationCodec(ledger).encode(tool_name, "subject", raw, {}, turn=0)
