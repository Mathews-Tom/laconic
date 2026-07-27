"""Action codec: the anchored-edit model, its materialization, and drift
resilience. ``docs/system-design.md`` §2.4 and §6 M6.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from laconic.codec.act import AnchoredEdit, StaleAnchorError, _resolve_symbol
from laconic.codec.outline import Symbol, TreeSitterOutliner
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


# --- Drift resilience: sequential edits shifting line numbers --------------


def _apply(source: str, tool_input: dict[str, object]) -> str:
    """Simulate a host tool applying a materialized ``{path, old, new}`` edit.

    Asserts the precondition a real first-match host implies: ``old`` must
    be present and name exactly one region, or applying it by replacing
    the first match is itself a silent best-guess. Checked via ``find``/
    ``rfind`` agreement plus a presence check -- ``find`` and ``rfind``
    both return ``-1`` for an absent ``old``, which would otherwise read as
    "unique" and let ``replace`` silently no-op instead of failing on the
    drift this suite exists to catch. ``str.count`` is avoided outright:
    it scans non-overlapping matches only and can under-report a
    self-overlapping periodic ``old`` as unique.
    """
    old = str(tool_input["old"])
    first, last = source.find(old), source.rfind(old)
    assert first != -1, f"materialized old is not present in source: {old!r}"
    assert first == last, f"materialized old is not positionally unique in source: {old!r}"
    return source.replace(old, str(tool_input["new"]), 1)


def test_drift_a_later_anchored_edit_survives_line_drift_from_an_earlier_edit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "widget.py"
    original = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        handle = _register_file(ledger, path, original)

        # Earlier edit in the same session: grows alpha's body, pushing
        # beta several lines further down than the handle's original
        # registration recorded it at.
        first = AnchoredEdit(
            handle=handle,
            anchor="alpha",
            anchor_occurrence=1,
            replacement="def alpha():\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c",
        )
        first_input = first.to_tool_input(ledger)
        assert first_input["old"] == "def alpha():\n    return 1"
        path.write_text(_apply(path.read_text(encoding="utf-8"), first_input), encoding="utf-8")

        # Anchoring on the symbol name, not a line number, is what lets this
        # second edit -- built from the same handle -- still find beta.
        second = AnchoredEdit(
            handle=handle,
            anchor="beta",
            anchor_occurrence=1,
            replacement="def beta():\n    return 42",
        )
        second_input = second.to_tool_input(ledger)

    assert second_input == {
        "path": str(path),
        "old": "def beta():\n    return 2",
        "new": "def beta():\n    return 42",
    }


def test_drift_a_later_edit_sees_an_earlier_edits_own_change_not_the_registered_snapshot(
    tmp_path: Path,
) -> None:
    """Discriminates the freshness mechanism directly: a regression that
    resolved against the ledger's stale first-read snapshot (``record.raw``)
    instead of the current on-disk file would still see beta's *original*
    body here, not what the first edit actually wrote."""
    path = tmp_path / "widget.py"
    original = "def beta():\n    return 1\n"
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        handle = _register_file(ledger, path, original)

        first = AnchoredEdit(
            handle=handle,
            anchor="beta",
            anchor_occurrence=1,
            replacement="def beta():\n    return 2",
        )
        first_input = first.to_tool_input(ledger)
        assert first_input["old"] == "def beta():\n    return 1"
        path.write_text(_apply(path.read_text(encoding="utf-8"), first_input), encoding="utf-8")

        second = AnchoredEdit(
            handle=handle,
            anchor="beta",
            anchor_occurrence=1,
            replacement="def beta():\n    return 3",
        )
        second_input = second.to_tool_input(ledger)

    assert second_input["old"] == "def beta():\n    return 2"


def test_drift_occurrence_disambiguation_survives_line_drift_from_an_earlier_edit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "widget.py"
    original = (
        "def handler():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def middle():\n"
        "    return 0\n"
        "\n"
        "\n"
        "def handler():\n"
        "    return 2\n"
    )
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        handle = _register_file(ledger, path, original)

        grow_middle = AnchoredEdit(
            handle=handle,
            anchor="middle",
            anchor_occurrence=1,
            replacement="def middle():\n    a = 1\n    b = 2\n    return a + b",
        )
        grow_input = grow_middle.to_tool_input(ledger)
        path.write_text(_apply(path.read_text(encoding="utf-8"), grow_input), encoding="utf-8")

        # The second "handler" has drifted further down than its original
        # registration; occurrence=2 must still name it, not the first.
        second_handler = AnchoredEdit(
            handle=handle,
            anchor="handler",
            anchor_occurrence=2,
            replacement="def handler():\n    return 99",
        )
        result = second_handler.to_tool_input(ledger)

    assert result["old"] == "def handler():\n    return 2"


def test_drift_a_stale_anchor_from_an_earlier_removal_fails_loudly_mid_session(
    tmp_path: Path,
) -> None:
    path = tmp_path / "widget.py"
    original = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        handle = _register_file(ledger, path, original)

        remove_beta = AnchoredEdit(
            handle=handle, anchor="beta", anchor_occurrence=1, replacement=""
        )
        remove_input = remove_beta.to_tool_input(ledger)
        path.write_text(_apply(path.read_text(encoding="utf-8"), remove_input), encoding="utf-8")

        stale = AnchoredEdit(handle=handle, anchor="beta", anchor_occurrence=1, replacement="X")
        with pytest.raises(StaleAnchorError, match="beta"):
            stale.to_tool_input(ledger)


# --- Round-trip: codec-materialized edit vs. a direct edit -----------------


def _direct_splice(source: str, symbol: Symbol, replacement: str) -> str:
    """Apply ``replacement`` over ``symbol``'s span by direct line splicing.

    This is the "direct edit" ground truth: what a caller would produce by
    editing the file itself, with no codec involved.
    """
    lines = source.split("\n")
    spliced = lines[: symbol.start_line - 1] + replacement.split("\n") + lines[symbol.end_line :]
    return "\n".join(spliced)


@pytest.mark.parametrize(
    "replacement",
    [
        "def beta():\n    return 99",
        "def beta():\n    a = 1\n    b = 2\n    return a + b",
        "def beta():\n    pass",
    ],
)
def test_round_trip_produces_a_byte_identical_result_to_a_direct_edit(
    tmp_path: Path, replacement: str
) -> None:

    path = tmp_path / "widget.py"
    source = (
        "def alpha():\n    return 1\n\n\n"
        "def beta():\n    return 2\n\n\n"
        "def gamma():\n    return 3\n"
    )
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        handle = _register_file(ledger, path, source)
        edit = AnchoredEdit(
            handle=handle, anchor="beta", anchor_occurrence=1, replacement=replacement
        )
        tool_input = edit.to_tool_input(ledger)

    via_codec = _apply(source, tool_input)

    outline = TreeSitterOutliner().outline(str(path), source)
    symbol = next(symbol for symbol in outline.symbols if symbol.name == "beta")
    direct = _direct_splice(source, symbol, replacement)

    assert via_codec == direct
    assert via_codec.encode("utf-8") == direct.encode("utf-8")
