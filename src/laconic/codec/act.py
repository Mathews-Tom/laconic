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

from laconic.codec.outline import Symbol


class StaleAnchorError(ValueError):
    """Raised when an anchor does not resolve against a file's current state.

    Covers both failure shapes named in ``docs/system-design.md`` §6 M6's
    acceptance text: a symbol that no longer exists at all, and one that
    exists but not at the requested occurrence (a prior edit removed one of
    several same-named matches). Either way, the region this edit anchored
    to is not there anymore, and applying it would mean guessing at a target
    — the exact risk the milestone's "never apply a best-guess anchor" rule
    forbids.
    """


def _resolve_symbol(symbols: Sequence[Symbol], anchor: str, occurrence: int) -> Symbol:
    """Return the ``occurrence``-th (1-based) symbol named ``anchor``.

    ``symbols`` must already be in document order — :class:`~laconic.codec.
    outline.Outline` sorts by ``(start_line, end_line, name)``, so occurrence
    numbering is deterministic and never depends on iteration order. There is
    no default occurrence and no closest-match fallback: an index that does
    not name a real match raises rather than picking one, which is what
    makes "resolves via explicit occurrence index or fails loudly" true by
    construction rather than by a runtime heuristic.
    """
    matches = [symbol for symbol in symbols if symbol.name == anchor]
    if occurrence > len(matches):
        raise StaleAnchorError(
            f"anchor {anchor!r} occurrence {occurrence} not found "
            f"({len(matches)} occurrence(s) present)"
        )
    return matches[occurrence - 1]


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
    anchored symbol's current body.
    """

    handle: str
    anchor: str
    anchor_occurrence: int
    replacement: str

    def __post_init__(self) -> None:
        if self.anchor_occurrence < 1:
            raise ValueError(f"anchor_occurrence must be >= 1, got {self.anchor_occurrence}")
