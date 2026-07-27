"""Action codec: the anchored-edit model, its materialization, and drift
resilience. ``docs/system-design.md`` §2.4 and §6 M6.
"""

from __future__ import annotations

import pytest

from laconic.codec.act import AnchoredEdit, StaleAnchorError, _resolve_symbol
from laconic.codec.outline import Symbol

# --- Model: AnchoredEdit construction and pure symbol resolution -----------


@pytest.mark.parametrize("occurrence", [0, -1, -100])
def test_model_a_non_positive_occurrence_is_rejected(occurrence: int) -> None:
    with pytest.raises(ValueError, match="anchor_occurrence"):
        AnchoredEdit(
            handle="F1", anchor="check_token", anchor_occurrence=occurrence, replacement="x"
        )


def test_model_a_positive_occurrence_constructs() -> None:
    edit = AnchoredEdit(handle="F1", anchor="check_token", anchor_occurrence=1, replacement="x")
    assert edit.anchor_occurrence == 1


def test_model_is_frozen() -> None:
    edit = AnchoredEdit(handle="F1", anchor="check_token", anchor_occurrence=1, replacement="x")
    with pytest.raises(AttributeError):
        edit.replacement = "y"  # type: ignore[misc]


def test_model_resolves_the_sole_occurrence() -> None:
    symbol = Symbol(name="check_token", kind="function", start_line=10, end_line=20)
    assert _resolve_symbol([symbol], "check_token", 1) is symbol


def test_model_resolves_a_later_occurrence_by_explicit_index() -> None:
    first = Symbol(name="handler", kind="function", start_line=1, end_line=5)
    second = Symbol(name="handler", kind="function", start_line=10, end_line=15)
    assert _resolve_symbol([first, second], "handler", 2) is second


def test_model_ignores_symbols_with_a_different_name() -> None:
    other = Symbol(name="other", kind="function", start_line=1, end_line=5)
    target = Symbol(name="handler", kind="function", start_line=10, end_line=15)
    assert _resolve_symbol([other, target], "handler", 1) is target


def test_model_a_missing_symbol_raises_stale() -> None:
    with pytest.raises(StaleAnchorError, match="check_token"):
        _resolve_symbol([], "check_token", 1)


def test_model_an_occurrence_beyond_the_available_matches_raises_stale() -> None:
    only = Symbol(name="handler", kind="function", start_line=1, end_line=5)
    with pytest.raises(StaleAnchorError, match="handler"):
        _resolve_symbol([only], "handler", 2)


def test_model_the_stale_error_names_how_many_occurrences_are_present() -> None:
    only = Symbol(name="handler", kind="function", start_line=1, end_line=5)
    with pytest.raises(StaleAnchorError, match=r"1 occurrence\(s\) present"):
        _resolve_symbol([only], "handler", 2)


def test_model_occurrence_numbering_follows_document_order_not_argument_order() -> None:
    """Symbols arrive pre-sorted by ``Outline``; resolution trusts that order."""
    earlier = Symbol(name="handler", kind="function", start_line=1, end_line=5)
    later = Symbol(name="handler", kind="function", start_line=50, end_line=55)
    assert _resolve_symbol([earlier, later], "handler", 1) is earlier
    assert _resolve_symbol([earlier, later], "handler", 2) is later
