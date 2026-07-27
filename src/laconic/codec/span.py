"""Span resolution: what part of a file must be shown verbatim.

``docs/system-design.md`` §2.2's ``FileEncoder.encode`` sketch resolves a
span from three inputs — the request, the outline, and the file's line
count — before it decides whether the structural summary alone answers the
request or a region of source must also be materialized. This module is
that resolution, factored out so it can be tested against the outline and
line-count axes independently of the encoder that renders the result.

Two request keys are understood, both optional and both 1-based:

- ``offset`` — the first line the caller explicitly asked to see.
- ``limit`` — how many lines from ``offset`` (default 1) the caller asked
  to see.

Malformed or out-of-range values are treated as absent rather than raised:
``request`` carries values from the tool call that produced the raw
observation, and a request the codec cannot make sense of must still
degrade to *something* useful rather than abort the encoding.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from laconic.codec.outline import Outline

#: Lines shown when no outline symbols exist to make the summary useful on
#: their own and the caller asked for nothing specific. Matches the
#: ``span_budget: int = 120`` default in ``docs/system-design.md`` §2.2's
#: ``FileEncoder`` sketch.
DEFAULT_SPAN_BUDGET = 120

REQUEST_OFFSET_KEY = "offset"
REQUEST_LIMIT_KEY = "limit"


class InvalidRangeError(ValueError):
    """Raised when a range is constructed with an invalid start or end."""


@dataclass(frozen=True, slots=True)
class LineRange:
    """A 1-based, inclusive line range."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 1:
            raise InvalidRangeError(f"start must be >= 1, got {self.start}")
        if self.end < self.start:
            raise InvalidRangeError(f"end {self.end} precedes start {self.start}")

    def union(self, other: LineRange) -> LineRange:
        """The smallest range covering both ``self`` and ``other``."""
        return LineRange(min(self.start, other.start), max(self.end, other.end))


def _bounded_int(value: object, *, minimum: int) -> int | None:
    """Return ``value`` as an int at least ``minimum``, or ``None``.

    ``bool`` is rejected even though it is an ``int`` subclass: a stray
    ``True``/``False`` in a request is not a line number. There is no upper
    bound here — a ``limit`` larger than the file, or the far end of an
    ``offset``/``limit`` pair, is a normal request (a host tool's default
    read size commonly exceeds a small file) and is clamped by the caller,
    not rejected here.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < minimum:
        return None
    return value


def _requested_range(request: Mapping[str, object], total_lines: int) -> LineRange | None:
    """The range ``offset``/``limit`` in ``request`` name, if any and valid."""
    offset = _bounded_int(request.get(REQUEST_OFFSET_KEY, 1), minimum=1)
    if offset is None or offset > total_lines:
        return None
    if REQUEST_LIMIT_KEY not in request:
        return LineRange(offset, total_lines) if REQUEST_OFFSET_KEY in request else None
    limit = _bounded_int(request.get(REQUEST_LIMIT_KEY), minimum=1)
    if limit is None:
        return None
    end = min(offset + limit - 1, total_lines)
    return LineRange(offset, end)


def resolve_span(
    request: Mapping[str, object],
    outline: Outline,
    total_lines: int,
    *,
    span_budget: int = DEFAULT_SPAN_BUDGET,
) -> tuple[LineRange, ...]:
    """Resolve which lines of the raw file must be shown verbatim.

    Returns an empty tuple when the structural summary alone answers the
    request — the caller asked for nothing specific and the outline carries
    symbols. Returns exactly one range for an explicit ``offset``/``limit``
    request. Returns a positional fallback window, bounded by
    ``span_budget``, when the outline carries no symbols at all: with no
    structural information to summarize, showing nothing would make the
    encoding useless, so this degrades to head-of-file span scoping —
    ``docs/system-design.md`` §2.2's "degrade to head/tail span scoping
    rather than failing" for the no-grammar case.
    """
    if total_lines <= 0:
        return ()

    requested = _requested_range(request, total_lines)
    if requested is not None:
        return (requested,)

    if outline.symbols:
        return ()

    return (LineRange(1, min(span_budget, total_lines)),)
