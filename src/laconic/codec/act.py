"""Action codec: edits re-expressed as deltas anchored to ledger handles and
symbol names, instead of restated file content.

``docs/system-design.md`` §2.4 names the rationale: edits are the second
largest tool-argument channel by volume (``Edit``: 1,755,266 chars across
1,218 calls), and restating the region around an edit on every call is what
this module replaces. Anchoring on a symbol name rather than a line number is
a correctness feature before it is a compression feature — in a session that
edits a file repeatedly, line numbers drift with every earlier edit and
symbol names usually do not.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from laconic.codec.outline import Outliner, Symbol, TreeSitterOutliner
from laconic.ledger import RAW_ENCODING, RAW_ERRORS, Ledger, ObservationKind, UnknownHandleError


class StaleAnchorError(ValueError):
    """Raised when an anchor does not resolve against a file's current state.

    Covers every failure shape named in ``docs/system-design.md`` §6 milestone's
    acceptance text: a symbol that no longer exists at all, one that exists
    but not at the requested occurrence (a prior edit removed one of several
    same-named matches), and a file type this build cannot outline at all
    (unlike the observation codec, the action codec cannot fail open here —
    see :func:`AnchoredEdit.to_tool_input`). Every case means the region this
    edit anchored to is not there anymore, and applying it would mean
    guessing at a target — the exact risk the milestone's "never apply a
    best-guess anchor" rule forbids.
    """


def _require_positive_occurrence(occurrence: int) -> None:
    if occurrence < 1:
        raise ValueError(f"anchor_occurrence must be >= 1, got {occurrence}")


def _resolve_symbol(symbols: Sequence[Symbol], anchor: str, occurrence: int) -> Symbol:
    """Return the ``occurrence``-th (1-based) symbol named ``anchor``.

    ``symbols`` must already be in document order — :class:`~laconic.codec.
    outline.Outline` sorts by ``(start_line, end_line, name)``, so occurrence
    numbering is deterministic and never depends on iteration order. There is
    no default occurrence and no closest-match fallback: an index that does
    not name a real match raises rather than picking one, which is what
    makes "resolves via explicit occurrence index or fails loudly" true by
    construction rather than by a runtime heuristic. Validated here too,
    not only at :class:`AnchoredEdit` construction, so this function keeps
    its own contract regardless of caller.
    """
    _require_positive_occurrence(occurrence)
    matches = [symbol for symbol in symbols if symbol.name == anchor]
    if occurrence > len(matches):
        raise StaleAnchorError(
            f"anchor {anchor!r} occurrence {occurrence} not found "
            f"({len(matches)} occurrence(s) present)"
        )
    return matches[occurrence - 1]


def _unique_span(lines: Sequence[str], start: int, end: int, replacement: str) -> tuple[str, str]:
    """Widen ``[start, end)`` (0-based, half-open) until it identifies
    exactly one region of the file, carrying ``replacement`` in lockstep.

    ``to_tool_input``'s ``old`` string is how a host applies this edit —
    typically by locating and replacing its first match. If two same-named
    symbols happen to share a byte-identical body, the resolved symbol's own
    span alone is not positionally unique, and a first-match host would
    silently apply the edit to whichever occurrence comes first regardless
    of which one ``anchor_occurrence`` actually resolved — reintroducing,
    at the application boundary, the exact best-guess risk this module
    exists to forbid at resolution time. Widening borrows unchanged context
    lines from either side until the slice is unique; the same borrowed
    lines are carried through unchanged in the returned ``new``, so the
    applied result changes only ``[start, end)`` and stays byte-identical to
    a direct edit of that span alone.

    Termination is guaranteed without an explicit bound: once ``lead == 0``
    and ``trail == len(lines)`` the candidate slice is the whole file, which
    trivially occurs in itself exactly once.

    Uniqueness is tested with ``find`` against ``rfind`` (the leftmost
    occurrence's index equals the rightmost's), not ``str.count`` --
    ``count`` scans non-overlapping matches only, so for self-overlapping
    periodic content (three or more identical, identically-separated
    symbol bodies, for instance) it can under-count and report a slice as
    unique when a second, overlapping occurrence still exists. ``find``/
    ``rfind`` each independently locate any occurrence regardless of
    overlap, so equality between them is a true single-occurrence test.
    """
    whole = "\n".join(lines)
    lead, trail = start, end
    while True:
        old = "\n".join(lines[lead:trail])
        if whole.find(old) == whole.rfind(old):
            new = "\n".join([*lines[lead:start], replacement, *lines[end:trail]])
            return old, new
        if lead > 0:
            lead -= 1
        elif trail < len(lines):
            trail += 1


@dataclass(frozen=True, slots=True)
class AnchoredEdit:
    """One edit, expressed as a delta anchored to a ledger handle and symbol.

    ``handle`` names the ledger record for the file being edited (``"F3"``).
    ``anchor`` is the symbol's name, not a line number: ``"check_token"``,
    not ``61``. ``anchor_occurrence`` is the 1-based, required index into the
    matches for ``anchor`` in document order — required, with no default, so
    every construction site must say explicitly which occurrence it means
    rather than relying on an implicit "first match" guess.
    ``replacement`` is the full text that should stand in place of the
    anchored symbol's current body -- the definition node's own lines,
    decorators and any code sharing its first or last physical line
    excluded, exactly as :class:`~laconic.codec.outline.Symbol` reports it.
    """

    handle: str
    anchor: str
    anchor_occurrence: int
    replacement: str

    def __post_init__(self) -> None:
        _require_positive_occurrence(self.anchor_occurrence)

    def to_tool_input(
        self, ledger: Ledger, *, outliner: Outliner | None = None
    ) -> dict[str, object]:
        """Materialize a real edit against the file's current on-disk state.

        Resolves ``handle`` to its subject path, reads that file's *current*
        bytes — not the ledger record's original-read snapshot, since an
        earlier edit in the same session may already have changed the file
        on disk without re-registering a new observation — re-outlines it,
        and locates the anchored symbol. ``outliner`` defaults to
        :class:`~laconic.codec.outline.TreeSitterOutliner`, matching
        :class:`~laconic.codec.encoders.file.FileEncoder`'s convention.

        The returned ``old`` names a region that is positionally unique in
        the current file — widened past the symbol's own span via
        :func:`_unique_span` when a same-named, same-bodied symbol would
        otherwise make it ambiguous to a first-match host. The result's
        ``{"path", "old", "new"}`` shape matches what ``tests/corpus``'s
        fixture sessions already establish for this repository's edit tool
        contract.

        Raises :class:`~laconic.ledger.UnknownHandleError` for a handle this
        session never minted, :class:`ValueError` when the handle names a
        non-file observation, and :class:`StaleAnchorError` when the anchor
        does not resolve against the file's current state — including a
        file type this build cannot outline at all, which the observation
        codec tolerates by design (:class:`~laconic.codec.outline.
        TreeSitterOutliner` degrades to an empty outline rather than
        raising) but the action codec cannot: an edit with nothing to anchor
        to is exactly the "never apply a best-guess anchor" case. A disk
        read failure propagates as :class:`StaleAnchorError` naming the
        handle and path. Every path here is a loud failure, never a silent
        best-guess application.
        """
        record = ledger.get(self.handle)
        if record is None:
            raise UnknownHandleError(f"unknown handle: {self.handle}")
        if record.kind is not ObservationKind.FILE:
            raise ValueError(
                f"handle {self.handle} is a {record.kind.name} observation, not a file"
            )
        try:
            current = Path(record.subject).read_bytes().decode(RAW_ENCODING, RAW_ERRORS)
        except OSError as error:
            raise StaleAnchorError(
                f"cannot read {record.subject} for handle {self.handle}: {error}"
            ) from error
        except UnicodeDecodeError as error:
            raise StaleAnchorError(
                f"{record.subject} is not decodable as {RAW_ENCODING}: {error}"
            ) from error
        resolved_outliner = outliner if outliner is not None else TreeSitterOutliner()
        outline = resolved_outliner.outline(record.subject, current)
        if outline.grammar is None:
            raise StaleAnchorError(
                f"no structural grammar for {record.subject}; "
                "symbol anchors require an outlinable file type"
            )
        symbol = _resolve_symbol(outline.symbols, self.anchor, self.anchor_occurrence)
        # split("\n"), never splitlines(): must agree line-for-line with
        # ledger._select_lines and encoders/file.py's span rendering, both of
        # which use the same literal-"\n" convention for the same reason.
        lines = current.split("\n")
        old, new = _unique_span(lines, symbol.start_line - 1, symbol.end_line, self.replacement)
        return {"path": record.subject, "old": old, "new": new}
