"""Action codec: the anchored-edit model, its materialization, and drift
resilience. ``docs/system-design.md`` §2.4 and §6 M6.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from laconic.codec.act import AnchoredEdit, StaleAnchorError, _resolve_symbol
from laconic.codec.outline import Symbol
from laconic.ledger import Ledger, ObservationKind, UnknownHandleError

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


@pytest.mark.parametrize("occurrence", [0, -1, -100])
def test_model_resolve_symbol_rejects_a_non_positive_occurrence_directly(
    occurrence: int,
) -> None:
    """The guard lives in ``_resolve_symbol`` itself, not only in the
    dataclass that is its only caller today -- a helper whose contract is
    "never picks" must hold that contract regardless of caller."""
    symbol = Symbol(name="handler", kind="function", start_line=1, end_line=5)
    with pytest.raises(ValueError, match="anchor_occurrence"):
        _resolve_symbol([symbol], "handler", occurrence)


# --- Materialization: to_tool_input against current file state -------------


def _register_file(ledger: Ledger, path: Path, source: str) -> str:
    path.write_text(source, encoding="utf-8")
    record = ledger.register(ObservationKind.FILE, str(path), source, "enc", 1)
    return record.handle


def test_materialize_replaces_a_uniquely_named_symbol(tmp_path: Path) -> None:
    path = tmp_path / "widget.py"
    source = "def handler():\n    return 1\n\n\ndef other():\n    return 2\n"
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        handle = _register_file(ledger, path, source)
        edit = AnchoredEdit(
            handle=handle,
            anchor="handler",
            anchor_occurrence=1,
            replacement="def handler():\n    return 99",
        )
        result = edit.to_tool_input(ledger)
    assert result == {
        "path": str(path),
        "old": "def handler():\n    return 1",
        "new": "def handler():\n    return 99",
    }


def test_materialize_disambiguates_by_explicit_occurrence(tmp_path: Path) -> None:
    path = tmp_path / "widget.py"
    source = "def handler():\n    return 1\n\n\ndef handler():\n    return 2\n"
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        handle = _register_file(ledger, path, source)
        first = AnchoredEdit(handle=handle, anchor="handler", anchor_occurrence=1, replacement="X")
        second = AnchoredEdit(handle=handle, anchor="handler", anchor_occurrence=2, replacement="Y")
        assert first.to_tool_input(ledger)["old"] == "def handler():\n    return 1"
        assert second.to_tool_input(ledger)["old"] == "def handler():\n    return 2"


def test_materialize_of_an_unknown_handle_raises(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        edit = AnchoredEdit(handle="F9", anchor="handler", anchor_occurrence=1, replacement="X")
        with pytest.raises(UnknownHandleError, match="F9"):
            edit.to_tool_input(ledger)


def test_materialize_of_a_symbol_no_longer_present_raises_stale(tmp_path: Path) -> None:
    """The current on-disk file governs, not the ledger's original snapshot."""
    path = tmp_path / "widget.py"
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        handle = _register_file(ledger, path, "def handler():\n    return 1\n")
        # A prior edit already changed the file on disk without a new
        # observation being registered -- exactly the drift scenario this
        # method exists to survive.
        path.write_text("def renamed():\n    return 1\n", encoding="utf-8")
        edit = AnchoredEdit(handle=handle, anchor="handler", anchor_occurrence=1, replacement="X")
        with pytest.raises(StaleAnchorError, match="handler"):
            edit.to_tool_input(ledger)


def test_materialize_of_an_occurrence_beyond_the_current_matches_raises_stale(
    tmp_path: Path,
) -> None:
    path = tmp_path / "widget.py"
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        handle = _register_file(ledger, path, "def handler():\n    return 1\n")
        edit = AnchoredEdit(handle=handle, anchor="handler", anchor_occurrence=2, replacement="X")
        with pytest.raises(StaleAnchorError, match="handler"):
            edit.to_tool_input(ledger)


def test_materialize_of_a_non_file_handle_raises(tmp_path: Path) -> None:
    """Only a FILE observation names a filesystem path worth reading."""
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        record = ledger.register(ObservationKind.COMMAND, "uv run pytest -q", "raw", "enc", 1)
        edit = AnchoredEdit(
            handle=record.handle, anchor="handler", anchor_occurrence=1, replacement="X"
        )
        with pytest.raises(ValueError, match=f"{record.handle}.*COMMAND"):
            edit.to_tool_input(ledger)


def test_materialize_of_an_unoutlinable_file_type_raises_stale(tmp_path: Path) -> None:
    """A grammar-less file has nothing to anchor to; this codec must not
    silently degrade to a headless outline the way the observation codec
    does -- an edit with no structure to anchor is a best-guess by
    definition."""
    path = tmp_path / "widget.txt"
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        handle = _register_file(ledger, path, "def handler():\n    return 1\n")
        edit = AnchoredEdit(handle=handle, anchor="handler", anchor_occurrence=1, replacement="X")
        with pytest.raises(StaleAnchorError, match="grammar"):
            edit.to_tool_input(ledger)


def test_materialize_of_a_deleted_file_raises_stale(tmp_path: Path) -> None:
    path = tmp_path / "widget.py"
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        handle = _register_file(ledger, path, "def handler():\n    return 1\n")
        path.unlink()
        edit = AnchoredEdit(handle=handle, anchor="handler", anchor_occurrence=1, replacement="X")
        with pytest.raises(StaleAnchorError, match=str(path)):
            edit.to_tool_input(ledger)


def test_materialize_of_undecodable_bytes_raises_stale(tmp_path: Path) -> None:
    """A lone invalid UTF-8 start byte -- not a lone surrogate, which
    RAW_ERRORS = "surrogatepass" tolerates -- must still fail loudly rather
    than propagate a bare UnicodeDecodeError."""
    path = tmp_path / "widget.py"
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        handle = _register_file(ledger, path, "def handler():\n    return 1\n")
        path.write_bytes(b"\xff\xfe not valid utf-8")
        edit = AnchoredEdit(handle=handle, anchor="handler", anchor_occurrence=1, replacement="X")
        with pytest.raises(StaleAnchorError, match="not decodable"):
            edit.to_tool_input(ledger)


@pytest.mark.parametrize(
    ("count", "occurrence"),
    [(2, 2), (3, 3)],
)
def test_materialize_disambiguates_byte_identical_bodies_by_occurrence(
    tmp_path: Path, count: int, occurrence: int
) -> None:
    """Same-named symbols with byte-identical bodies must still resolve to
    distinct, positionally unambiguous edits -- otherwise a first-match
    host silently applies the edit to whichever occurrence comes first,
    regardless of which one anchor_occurrence actually resolved. Covers
    both a pair (count=2) and a self-overlapping run of three (count=3),
    the case a non-overlapping-aware uniqueness check under-counts."""
    path = tmp_path / "widget.py"
    block = "def f():\n    return 1\n\n\n"
    source = block * (count - 1) + "def f():\n    return 1\n"
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        handle = _register_file(ledger, path, source)
        edit = AnchoredEdit(
            handle=handle,
            anchor="f",
            anchor_occurrence=occurrence,
            replacement="def f():\n    return 2",
        )
        result = edit.to_tool_input(ledger)

    old = str(result["old"])
    new = str(result["new"])
    # A true positional-uniqueness check: find and rfind agree iff old
    # occurs at exactly one position, including overlapping candidates
    # non-overlapping str.count would under-report.
    assert source.find(old) == source.rfind(old), "materialized old must be positionally unique"
    applied = source.replace(old, new, 1)
    expected = block * (count - 1) + "def f():\n    return 2\n"
    assert applied == expected


def test_materialize_uses_an_injected_outliner(tmp_path: Path) -> None:
    """``outliner`` is keyword-only and optional, matching FileEncoder's
    convention, so a test double can stand in for TreeSitterOutliner."""
    from laconic.codec.outline import FallbackOutliner

    path = tmp_path / "widget.py"
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        handle = _register_file(ledger, path, "def handler():\n    return 1\n")
        edit = AnchoredEdit(handle=handle, anchor="handler", anchor_occurrence=1, replacement="X")
        with pytest.raises(StaleAnchorError):
            # FallbackOutliner never reports structure, so even a present
            # symbol resolves as unoutlinable through it.
            edit.to_tool_input(ledger, outliner=FallbackOutliner())
