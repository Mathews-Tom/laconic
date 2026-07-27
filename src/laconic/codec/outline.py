"""Structural outlining: symbol extraction for the file observation encoder.

``docs/system-design.md`` §2.2 names the contract: an :class:`Outliner` turns
one file's raw source into a list of top-level symbols with their line
ranges, so :class:`~laconic.codec.encoders.file.FileEncoder` can show a
structural summary instead of a whole-file dump. The interface is a
:class:`Protocol` because the tree-sitter-backed implementation
(``TreeSitterOutliner``, added on top of this module) and this module's
:class:`FallbackOutliner` must be interchangeable: whichever one a
``FileEncoder`` holds, it never raises and it always returns an
:class:`Outline`, empty or not.

``FallbackOutliner`` is the mandated safety net from §2.2: "a codec that
errors on an unfamiliar language is worse than one that compresses it
badly." It carries no structural knowledge of any language and never fails,
so a file type this build cannot parse still gets a well-formed (if empty)
outline rather than an exception.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Symbol:
    """One structural element of a file: a function, class, or similar.

    Line numbers are 1-based and inclusive, matching the convention the
    handle ledger's span syntax already uses (``docs/system-design.md``
    §2.1's ``F3:61-94``).
    """

    name: str
    kind: str
    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        if self.start_line < 1:
            raise ValueError(f"start_line must be >= 1, got {self.start_line}")
        if self.end_line < self.start_line:
            raise ValueError(f"end_line {self.end_line} precedes start_line {self.start_line}")

    def render(self) -> str:
        """Render as ``name:12`` for a one-line symbol, ``name:31-58`` otherwise."""
        if self.start_line == self.end_line:
            return f"{self.name}:{self.start_line}"
        return f"{self.name}:{self.start_line}-{self.end_line}"


#: How many symbols :meth:`Outline.render` shows before summarizing the rest.
#: Matches the worked example in ``docs/system-design.md`` §2.2
#: (``outline.render(limit=8)``).
DEFAULT_RENDER_LIMIT = 8


@dataclass(frozen=True, slots=True)
class Outline:
    """The structural summary an :class:`Outliner` produced for one file.

    ``grammar`` names the language grammar that produced ``symbols``, or is
    ``None`` when no structural information is available (unrecognized file
    type, or a parser that degraded to the fallback). An empty ``symbols``
    tuple is a valid, well-formed outline either way — it means "nothing to
    summarize", not "something went wrong".
    """

    subject: str
    symbols: tuple[Symbol, ...]
    grammar: str | None

    def render(self, *, limit: int | None = DEFAULT_RENDER_LIMIT) -> str:
        """Render symbols as ``name:12  other:31-58  [+N more]``.

        Symbols are shown in ``self.symbols`` order (callers are expected to
        pass symbols already sorted by position). An empty outline renders
        as the empty string.
        """
        shown = self.symbols if limit is None else self.symbols[:limit]
        parts = [symbol.render() for symbol in shown]
        remaining = len(self.symbols) - len(shown)
        if remaining > 0:
            parts.append(f"[+{remaining} more]")
        return "  ".join(parts)


class Outliner(Protocol):
    """Extracts a structural :class:`Outline` from one file's raw source."""

    def outline(self, subject: str, raw: str) -> Outline: ...


class FallbackOutliner:
    """The mandated safety net: always succeeds, never carries structure.

    Used directly for any file type this build has no grammar for, and as
    the escape hatch a structural outliner degrades to when parsing a
    recognized file type still goes wrong. Either way the caller gets a
    valid, empty :class:`Outline` rather than an exception.
    """

    def outline(self, subject: str, raw: str) -> Outline:
        return Outline(subject=subject, symbols=(), grammar=None)
