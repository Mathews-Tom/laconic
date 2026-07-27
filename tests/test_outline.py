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
