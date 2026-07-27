"""Structural outlining: the protocol, the safe fallback, and the
tree-sitter-backed extractor.

``docs/system-design.md`` §2.2 mandates that an outliner never fail closed:
"a codec that errors on an unfamiliar language is worse than one that
compresses it badly." The fallback tests below are the property-based proof
of that half of the contract; the tree-sitter tests (added alongside
``TreeSitterOutliner``) cover symbol extraction itself.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from laconic.codec.outline import (
    DEFAULT_RENDER_LIMIT,
    FallbackOutliner,
    Outline,
    Symbol,
    TreeSitterOutliner,
)

ANY_TEXT = st.text(st.characters(), max_size=5_000)
SUBJECTS = st.text(st.characters(), max_size=200)

PROPERTY = settings(deadline=None, max_examples=200)


# --- Symbol -----------------------------------------------------------------


def test_symbol_render_uses_a_bare_line_number_for_a_one_line_symbol() -> None:
    symbol = Symbol(name="TokenError", kind="class", start_line=12, end_line=12)
    assert symbol.render() == "TokenError:12"


def test_symbol_render_uses_a_range_for_a_multi_line_symbol() -> None:
    symbol = Symbol(name="decode_token", kind="function", start_line=31, end_line=58)
    assert symbol.render() == "decode_token:31-58"


def test_symbol_rejects_a_start_line_before_one() -> None:
    with pytest.raises(ValueError, match="start_line"):
        Symbol(name="x", kind="function", start_line=0, end_line=1)


def test_symbol_rejects_an_end_line_before_the_start_line() -> None:
    with pytest.raises(ValueError, match="end_line"):
        Symbol(name="x", kind="function", start_line=5, end_line=4)


# --- Outline.render -----------------------------------------------------------


def test_outline_render_of_no_symbols_is_the_empty_string() -> None:
    outline = Outline(subject="f.py", symbols=(), grammar="python")
    assert outline.render() == ""


def test_outline_render_matches_the_worked_example() -> None:
    """``docs/overview.md`` §3.1's worked example, verbatim."""
    symbols = (
        Symbol("TokenError", "class", 12, 12),
        Symbol("decode_token", "function", 31, 58),
        Symbol("check_token", "function", 61, 94),
        Symbol("refresh", "function", 97, 140),
    )
    outline = Outline(subject="src/auth/tokens.py", symbols=symbols, grammar="python")
    assert outline.render() == (
        "TokenError:12  decode_token:31-58  check_token:61-94  refresh:97-140"
    )


def test_outline_render_truncates_and_counts_the_remainder() -> None:
    symbols = tuple(Symbol(f"fn{i}", "function", i, i) for i in range(1, 13))
    outline = Outline(subject="f.py", symbols=symbols, grammar="python")
    rendered = outline.render(limit=8)
    assert rendered.endswith("[+4 more]")
    assert rendered.count(":") == 8  # the eight shown symbols, not the marker


def test_outline_render_default_limit_matches_the_documented_default() -> None:
    assert DEFAULT_RENDER_LIMIT == 8


def test_outline_render_with_no_limit_shows_every_symbol() -> None:
    symbols = tuple(Symbol(f"fn{i}", "function", i, i) for i in range(1, 13))
    outline = Outline(subject="f.py", symbols=symbols, grammar="python")
    assert "[+" not in outline.render(limit=None)
    assert outline.render(limit=None).count(":") == 12


# --- FallbackOutliner ---------------------------------------------------------


def test_fallback_outliner_returns_no_symbols() -> None:
    outline = FallbackOutliner().outline("f.bin", "anything")
    assert outline.symbols == ()


def test_fallback_outliner_names_no_grammar() -> None:
    outline = FallbackOutliner().outline("f.bin", "anything")
    assert outline.grammar is None


def test_fallback_outliner_preserves_the_subject() -> None:
    outline = FallbackOutliner().outline("weird/path.xyz", "content")
    assert outline.subject == "weird/path.xyz"


def test_fallback_outline_renders_as_the_empty_string() -> None:
    outline = FallbackOutliner().outline("f.bin", "content")
    assert outline.render() == ""


@PROPERTY
@given(subject=SUBJECTS, raw=ANY_TEXT)
def test_fallback_outliner_never_raises_on_arbitrary_input(subject: str, raw: str) -> None:
    outline = FallbackOutliner().outline(subject, raw)
    assert outline.symbols == ()
    assert outline.grammar is None


@PROPERTY
@given(raw=ANY_TEXT)
def test_fallback_outliner_is_deterministic(raw: str) -> None:
    outliner = FallbackOutliner()
    first = outliner.outline("f.bin", raw)
    second = outliner.outline("f.bin", raw)
    assert first == second


def test_fallback_outliner_never_raises_on_binary_looking_content() -> None:
    """Not just "weird text" — content that looks nothing like source at all."""
    raw = "\x00\x01\x02\xff" * 100 + "\ufffd" * 50
    outline = FallbackOutliner().outline("f.exe", raw)
    assert outline.symbols == ()


# --- TreeSitterOutliner -------------------------------------------------------


PYTHON_SOURCE = """\
class TokenError(Exception):
    pass


def decode_token(raw):
    return raw


class Codec:
    def encode(self, x):
        return x

    def decode(self, x):
        return x
"""

JAVASCRIPT_SOURCE = """\
function foo(x) {
  return x;
}

class Widget {
  method() {
    return 1;
  }
}

function* gen() {
  yield 1;
}
"""

TYPESCRIPT_SOURCE = """\
interface Shape {
  area(): number;
}

type Point = { x: number; y: number };

enum Color {
  Red,
  Green,
}

function area(shape: Shape): number {
  return shape.area();
}

class Circle implements Shape {
  area(): number {
    return 1;
  }
}
"""

TSX_SOURCE = """\
function Widget(props: { label: string }) {
  return <div>{props.label}</div>;
}
"""

GO_SOURCE = """\
package main

func Foo(x int) int {
    return x
}

type Bar struct {
    X int
}

func (b *Bar) Method() int {
    return b.X
}
"""

RUST_SOURCE = """\
struct Foo {
    x: i32,
}

trait Greet {
    fn hello(&self) -> i32;
}

impl Foo {
    fn method(&self) -> i32 {
        self.x
    }
}

fn free_fn(x: i32) -> i32 {
    x
}

enum Color {
    Red,
    Green,
}
"""


def test_python_outline_extracts_classes_functions_and_methods() -> None:
    outline = TreeSitterOutliner().outline("f.py", PYTHON_SOURCE)
    assert outline.grammar == "python"
    names = {s.name for s in outline.symbols}
    assert names == {"TokenError", "decode_token", "Codec", "encode", "decode"}


def test_python_outline_symbols_are_sorted_by_start_line() -> None:
    outline = TreeSitterOutliner().outline("f.py", PYTHON_SOURCE)
    starts = [s.start_line for s in outline.symbols]
    assert starts == sorted(starts)


def test_python_outline_line_ranges_are_exact() -> None:
    outline = TreeSitterOutliner().outline("f.py", PYTHON_SOURCE)
    by_name = {s.name: s for s in outline.symbols}
    assert by_name["TokenError"].start_line == 1
    assert by_name["TokenError"].end_line == 2
    assert by_name["decode_token"].start_line == 5
    assert by_name["decode_token"].end_line == 6


def test_javascript_outline_extracts_functions_classes_and_methods() -> None:
    outline = TreeSitterOutliner().outline("f.js", JAVASCRIPT_SOURCE)
    assert outline.grammar == "javascript"
    names = {s.name for s in outline.symbols}
    assert names == {"foo", "Widget", "method", "gen"}


def test_javascript_grammar_covers_mjs_cjs_and_jsx_extensions() -> None:
    for extension in ("mjs", "cjs", "jsx"):
        outline = TreeSitterOutliner().outline(f"f.{extension}", "function foo() {}")
        assert outline.grammar == "javascript"
        assert {s.name for s in outline.symbols} == {"foo"}


def test_typescript_outline_extracts_interfaces_types_enums_and_functions() -> None:
    outline = TreeSitterOutliner().outline("f.ts", TYPESCRIPT_SOURCE)
    assert outline.grammar == "typescript"
    names = {s.name for s in outline.symbols}
    assert names == {"Shape", "Point", "Color", "area", "Circle"}


def test_tsx_outline_parses_jsx_syntax_a_plain_typescript_grammar_rejects() -> None:
    outline = TreeSitterOutliner().outline("f.tsx", TSX_SOURCE)
    assert outline.grammar == "tsx"
    assert {s.name for s in outline.symbols} == {"Widget"}


def test_go_outline_extracts_functions_methods_and_types() -> None:
    outline = TreeSitterOutliner().outline("f.go", GO_SOURCE)
    assert outline.grammar == "go"
    names = {s.name for s in outline.symbols}
    assert names == {"Foo", "Bar", "Method"}


def test_rust_outline_extracts_functions_structs_traits_and_enums() -> None:
    outline = TreeSitterOutliner().outline("f.rs", RUST_SOURCE)
    assert outline.grammar == "rust"
    names = {s.name for s in outline.symbols}
    # impl blocks carry no "name" field and are not themselves a symbol, but
    # the method inside one is still reached by the full-tree walk. Trait
    # method signatures (no body) are ``function_signature_item``, also
    # mapped to "function".
    assert names == {"Foo", "Greet", "hello", "method", "free_fn", "Color"}


def test_an_unregistered_extension_degrades_to_the_fallback() -> None:
    outline = TreeSitterOutliner().outline("f.brainfuck", "+[-->-[>>+>-----<<]<--<---]")
    assert outline.symbols == ()
    assert outline.grammar is None


def test_a_subject_with_no_extension_degrades_to_the_fallback() -> None:
    outline = TreeSitterOutliner().outline("Makefile", "all:\n\techo hi\n")
    assert outline.symbols == ()
    assert outline.grammar is None


def test_tree_sitter_outliner_is_deterministic() -> None:
    outliner = TreeSitterOutliner()
    first = outliner.outline("f.py", PYTHON_SOURCE)
    second = outliner.outline("f.py", PYTHON_SOURCE)
    assert first == second


def test_repeated_parses_on_one_outliner_never_report_out_of_range_lines() -> None:
    """Regression guard for a reproduced bug: reusing one ``Parser`` across
    many ``.parse()`` calls silently corrupted later parses' line numbers
    into garbage (values in the billions) with no exception raised, once
    enough prior parse trees existed for CPython's cyclic GC to run.
    :class:`TreeSitterOutliner` now builds a fresh ``Parser`` per call; this
    exercises the same one-instance, many-calls, moderately-large-input
    shape that reproduced the corruption against the fixture corpus.
    """
    outliner = TreeSitterOutliner()
    source = "\n".join(f"def fn_{i}(x):\n    return x + {i}\n" for i in range(200))
    total_lines = len(source.split("\n"))
    for _ in range(40):
        outline = outliner.outline("f.py", source)
        assert len(outline.symbols) == 200
        for symbol in outline.symbols:
            assert 1 <= symbol.start_line <= symbol.end_line <= total_lines


@PROPERTY
@given(raw=ANY_TEXT)
def test_the_python_outliner_never_raises_on_arbitrary_text(raw: str) -> None:
    TreeSitterOutliner().outline("f.py", raw)


@PROPERTY
@given(raw=ANY_TEXT, extension=st.sampled_from(["py", "js", "ts", "tsx", "go", "rs"]))
def test_every_registered_grammar_never_raises_on_arbitrary_text(raw: str, extension: str) -> None:
    TreeSitterOutliner().outline(f"f.{extension}", raw)


@PROPERTY
@given(subject=SUBJECTS, raw=ANY_TEXT)
def test_the_tree_sitter_outliner_never_raises_on_any_subject(subject: str, raw: str) -> None:
    TreeSitterOutliner().outline(subject, raw)
