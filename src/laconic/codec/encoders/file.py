"""File observation encoder: structural outline plus the requested span.

``docs/system-design.md`` §2.2 names this the single largest lever: "reads
are whale-distributed... returning a whole file to answer a question about
one function pays for that file on every subsequent turn." This module
replaces a whole-file read with a structural summary plus whatever span the
request, the outline, or the no-grammar fallback in
:mod:`laconic.codec.span` resolve, and registers the pair with the handle
ledger so every elided line stays recoverable through
:meth:`~laconic.ledger.Ledger.expand`.

Line numbers throughout are computed by splitting on a literal ``"\\n"``,
matching :func:`laconic.ledger._select_lines` exactly. Python's
``str.splitlines()`` treats more characters as line breaks and drops
information a trailing newline carries, which would make a span this module
reports (``span 61-94``) disagree with what ``ledger.expand("F3:61-94")``
actually returns for the same file.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from laconic.codec.outline import DEFAULT_RENDER_LIMIT, Outline, Outliner, TreeSitterOutliner
from laconic.codec.span import DEFAULT_SPAN_BUDGET, LineRange, resolve_span
from laconic.ledger import Ledger, ObservationKind, Record

#: A lone UTF-16 surrogate is a legal Python ``str`` code point (it can
#: arrive via ``surrogateescape``-decoded bytes or plain fuzz input) but is
#: not valid UTF-8, and the ledger's ``subject``/``encoded`` columns must be
#: storable UTF-8 text — ``Ledger.register`` raises ``ValueError`` otherwise
#: (``laconic.ledger._require_storable``). ``raw`` tolerates this via
#: ``surrogatepass`` because it is stored as a compressed BLOB, never
#: re-encoded to strict UTF-8; the presentational ``encoded`` text this
#: module embeds ``raw`` fragments into has no such escape hatch, so a file
#: containing a lone surrogate must not be able to make ``encode`` raise.
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _storable(text: str) -> str:
    """Replace any lone surrogate with U+FFFD so the ledger can store this."""
    return _LONE_SURROGATE.sub("\ufffd", text)


_BODY_INDENT = "    "


def _indent(text: str) -> str:
    if not text:
        return text
    return "\n".join(f"{_BODY_INDENT}{line}" for line in text.split("\n"))


def _render(
    subject: str,
    total_lines: int,
    outline: Outline,
    ranges: tuple[LineRange, ...],
    lines: list[str],
) -> str:
    head = f"{subject}  {total_lines:,} lines"
    if outline.symbols:
        head += f"\n  outline: {outline.render(limit=DEFAULT_RENDER_LIMIT)}"
    if not ranges:
        return head  # the outline alone answers the request

    parts = [head]
    previous_end = 0
    for line_range in ranges:
        gap = line_range.start - previous_end - 1
        if previous_end and gap > 0:
            parts.append(f"  [... {gap} lines elided — expand the handle for the full range]")
        body = "\n".join(lines[line_range.start - 1 : line_range.end])
        parts.append(f"  span {line_range.start}-{line_range.end}:\n{_indent(body)}")
        previous_end = line_range.end
    return "\n".join(parts)


class FileEncoder:
    """Re-encodes a whole-file read as an outline plus span, ledger-backed.

    ``outliner`` defaults to :class:`~laconic.codec.outline.TreeSitterOutliner`,
    which already degrades to the safe fallback for any unrecognized
    extension or internal parse failure, so ``encode`` never raises on that
    account either. Every call registers the encoding with ``ledger`` under
    :attr:`~laconic.ledger.ObservationKind.FILE` and returns the resulting
    :class:`~laconic.ledger.Record` — its ``.raw`` and ``.handle`` are what
    make the elision reversible, and ``.encoded`` is what the caller shows
    the model.
    """

    def __init__(
        self,
        ledger: Ledger,
        outliner: Outliner | None = None,
        *,
        span_budget: int = DEFAULT_SPAN_BUDGET,
    ) -> None:
        self._ledger = ledger
        self._outliner = outliner if outliner is not None else TreeSitterOutliner()
        self._span_budget = span_budget

    def encode(
        self,
        subject: str,
        raw: str,
        request: Mapping[str, object],
        *,
        turn: int,
    ) -> Record:
        lines = raw.split("\n")
        total_lines = len(lines)
        outline = self._outliner.outline(subject, raw)
        ranges = resolve_span(request, outline, total_lines, span_budget=self._span_budget)
        encoded = _render(subject, total_lines, outline, ranges, lines)
        return self._ledger.register(
            ObservationKind.FILE, _storable(subject), raw, _storable(encoded), turn
        )
