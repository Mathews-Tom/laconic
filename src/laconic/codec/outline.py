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

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

import tree_sitter_go as _ts_go
import tree_sitter_javascript as _ts_javascript
import tree_sitter_python as _ts_python
import tree_sitter_rust as _ts_rust
import tree_sitter_typescript as _ts_typescript
from tree_sitter import Language, Parser, Tree


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


# --- Tree-sitter-backed extraction -------------------------------------------
#
# The documented minimum grammar set (ledger entry H-13): Python, JavaScript,
# TypeScript (including TSX), Go, and Rust. Every other extension, and any
# internal failure while parsing a supported one, degrades to ``fallback``
# rather than raising — the two "no structure available" paths behave
# identically by construction.


@dataclass(frozen=True, slots=True)
class _Grammar:
    """One entry in the documented minimum grammar set.

    ``definition_kinds`` maps a tree-sitter node type this grammar names a
    definition to the :class:`Symbol` kind it becomes. Every mapped node
    type is expected to expose a tree-sitter ``name`` field.
    """

    name: str
    factory: Callable[[], object]
    definition_kinds: Mapping[str, str]


_JS_LIKE_KINDS: Mapping[str, str] = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "method_definition": "method",
}

_TS_KINDS: Mapping[str, str] = {
    **_JS_LIKE_KINDS,
    "abstract_class_declaration": "class",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
}

#: File extension (lowercase, with leading dot) -> grammar. Registering the
#: same grammar under more than one extension (``.js``/``.mjs``/``.cjs``) is
#: cheap: it shares one ``_Grammar`` entry, and every call builds its own
#: ``Parser`` regardless (see :class:`TreeSitterOutliner`).
_EXTENSION_GRAMMARS: dict[str, _Grammar] = {
    ".py": _Grammar(
        "python",
        _ts_python.language,
        {
            "function_definition": "function",
            "class_definition": "class",
        },
    ),
    ".js": _Grammar("javascript", _ts_javascript.language, _JS_LIKE_KINDS),
    ".mjs": _Grammar("javascript", _ts_javascript.language, _JS_LIKE_KINDS),
    ".cjs": _Grammar("javascript", _ts_javascript.language, _JS_LIKE_KINDS),
    ".jsx": _Grammar("javascript", _ts_javascript.language, _JS_LIKE_KINDS),
    ".ts": _Grammar("typescript", _ts_typescript.language_typescript, _TS_KINDS),
    ".tsx": _Grammar("tsx", _ts_typescript.language_tsx, _TS_KINDS),
    ".go": _Grammar(
        "go",
        _ts_go.language,
        {
            "function_declaration": "function",
            "method_declaration": "method",
            "type_spec": "type",
        },
    ),
    ".rs": _Grammar(
        "rust",
        _ts_rust.language,
        {
            "function_item": "function",
            "function_signature_item": "function",
            "struct_item": "struct",
            "enum_item": "enum",
            "trait_item": "trait",
        },
    ),
}


def _grammar_for(subject: str) -> _Grammar | None:
    suffix = subject.rsplit("/", 1)[-1]
    _, dot, extension = suffix.rpartition(".")
    if not dot:
        return None
    return _EXTENSION_GRAMMARS.get(f".{extension.lower()}")


def _walk_symbols(tree: Tree, definition_kinds: Mapping[str, str]) -> list[Symbol]:
    """Depth-first symbol extraction via a single :class:`TreeCursor`.

    Deliberately not recursive ``node.children`` access: repeatedly
    re-evaluating that property while deep in a large tree interacts badly
    with CPython's cyclic garbage collector and this binding's ``Node``
    lifetime — observed directly as ``start_point``/``end_point`` silently
    corrupting into garbage mid-walk on an ~4,700-node tree, with no
    exception raised. A single cursor navigated in place allocates no
    per-node cursor and has no such hazard; it is also the traversal
    tree-sitter's own documentation recommends for exactly this reason.
    """
    symbols: list[Symbol] = []
    cursor = tree.walk()
    reached_root = False
    while not reached_root:
        node = cursor.node
        if node is not None:
            kind = definition_kinds.get(node.type)
            if kind is not None:
                name_node = node.child_by_field_name("name")
                text = None if name_node is None else name_node.text
                if text is not None:
                    symbols.append(
                        Symbol(
                            name=text.decode("utf-8", "replace"),
                            kind=kind,
                            start_line=node.start_point.row + 1,
                            end_line=node.end_point.row + 1,
                        )
                    )
        if cursor.goto_first_child():
            continue
        while not cursor.goto_next_sibling():
            if not cursor.goto_parent():
                reached_root = True
                break
    return symbols


class TreeSitterOutliner:
    """Symbol extraction over the documented minimum grammar set.

    An unrecognized extension, or any exception while parsing a recognized
    one, degrades to ``fallback`` — an empty, well-formed :class:`Outline` —
    rather than propagating.

    Deliberately builds a fresh :class:`Parser`/:class:`Language` pair on
    every call rather than caching one per grammar: reusing a single
    ``Parser`` across many ``.parse()`` calls in this binding was observed
    to silently corrupt later parses' ``Node`` position data (garbage
    ``start_point``/``end_point`` values, no exception) once enough prior
    trees had been dropped for CPython's cyclic GC to run — reproduced
    reliably against the fixture corpus. A fresh ``Parser`` per call has no
    prior-parse state to corrupt; construction cost is negligible next to
    the parse itself.
    """

    def __init__(self, fallback: Outliner | None = None) -> None:
        self._fallback = fallback if fallback is not None else FallbackOutliner()

    def outline(self, subject: str, raw: str) -> Outline:
        grammar = _grammar_for(subject)
        if grammar is None:
            return self._fallback.outline(subject, raw)
        try:
            parser = Parser(Language(grammar.factory()))
            tree = parser.parse(raw.encode("utf-8", "surrogatepass"))
            symbols = tuple(
                sorted(
                    _walk_symbols(tree, grammar.definition_kinds),
                    key=lambda symbol: (symbol.start_line, symbol.end_line, symbol.name),
                )
            )
            return Outline(subject=subject, symbols=symbols, grammar=grammar.name)
        except Exception:
            # A registered grammar that still chokes on this particular file
            # degrades exactly like an unregistered extension: never fail
            # closed. docs/system-design.md §2.2.
            return self._fallback.outline(subject, raw)
