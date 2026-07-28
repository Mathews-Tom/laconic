"""Observation codec dispatch: one encoder per tool shape, by tool name.

``docs/system-design.md`` §2.2: "One encoder per tool shape, dispatched by
tool name, with a safe fallback." :class:`ObservationCodec` is that
dispatch layer. It holds one instance of every encoder this build knows
about and routes each observation to the encoder matching the tool name
that produced it, falling back to
:class:`~laconic.codec.encoders.fallback.FallbackEncoder` for any tool
name it does not recognize — so an unfamiliar tool always encodes, never
raises. Every tool shape this build knows about is now dispatched: files,
commands, and search results, with fallback as the safe default for
anything else.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from laconic.codec.encoders._elision import DEFAULT_KEEP_HEAD, DEFAULT_KEEP_TAIL, DEFAULT_MAX_ERRORS
from laconic.codec.encoders.command import CommandEncoder
from laconic.codec.encoders.fallback import FallbackEncoder
from laconic.codec.encoders.file import FileEncoder
from laconic.codec.encoders.search import SearchEncoder
from laconic.codec.outline import Outliner
from laconic.codec.span import DEFAULT_SPAN_BUDGET
from laconic.ledger import Ledger, Record

#: Tool names whose result is a file read, routed to
#: :class:`~laconic.codec.encoders.file.FileEncoder`.
FILE_TOOLS = frozenset({"Read"})

#: Tool names whose result is command output, routed to
#: :class:`~laconic.codec.encoders.command.CommandEncoder`.
COMMAND_TOOLS = frozenset({"Bash"})

#: Tool names whose result is a search-shaped list of matches, routed to
#: :class:`~laconic.codec.encoders.search.SearchEncoder`.
SEARCH_TOOLS = frozenset({"Grep", "Glob"})


#: Tool-input keys checked, in order, for an observation's human-readable
#: subject (a file path, a command line, a search pattern). ``file_path``
#: precedes ``path``: a real Claude Code ``Read`` result carries the
#: former; only this repo's own synthetic corpus fixtures use the latter.
_SUBJECT_KEYS = ("file_path", "path", "command", "pattern", "query")


def subject_for(tool_input: Mapping[str, object]) -> str:
    """Return a tool call's human-readable subject: whichever of
    :data:`_SUBJECT_KEYS` its input carries, or the input's own JSON as a
    last resort so a subject is never empty.

    The single source for this lookup -- ``laconic.cli``, K4, and the
    fixture generator all encode real tool observations and must derive
    the same subject for the same input, so none of them keeps its own
    copy.
    """
    for key in _SUBJECT_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(tool_input, sort_keys=True)


class ObservationCodec:
    """Dispatches a raw tool observation to the encoder for its shape."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        outliner: Outliner | None = None,
        span_budget: int = DEFAULT_SPAN_BUDGET,
        keep_head: int = DEFAULT_KEEP_HEAD,
        keep_tail: int = DEFAULT_KEEP_TAIL,
        max_errors: int = DEFAULT_MAX_ERRORS,
    ) -> None:
        self._file = FileEncoder(ledger, outliner, span_budget=span_budget)
        self._command = CommandEncoder(
            ledger, keep_head=keep_head, keep_tail=keep_tail, max_errors=max_errors
        )
        self._search = SearchEncoder(ledger)
        self._fallback = FallbackEncoder(
            ledger, keep_head=keep_head, keep_tail=keep_tail, max_errors=max_errors
        )

    def encode(
        self,
        tool_name: str,
        subject: str,
        raw: str,
        request: Mapping[str, object],
        *,
        turn: int,
    ) -> Record:
        """Encode one observation, dispatched by the tool name that produced it.

        A name outside every known set — including one this build has
        simply never seen — falls to :class:`FallbackEncoder` rather than
        raising, per ``docs/system-design.md`` §2.2's mandated safe default.
        """
        if tool_name in FILE_TOOLS:
            return self._file.encode(subject, raw, request, turn=turn)
        if tool_name in COMMAND_TOOLS:
            return self._command.encode(subject, raw, request, turn=turn)
        if tool_name in SEARCH_TOOLS:
            return self._search.encode(subject, raw, request, turn=turn)
        return self._fallback.encode(subject, raw, request, turn=turn)
