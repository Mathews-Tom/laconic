"""Command observation encoder: error-salient, duplicate-collapsed elision.

``docs/system-design.md`` §2.2 names Bash the second-largest observation
channel — "5,708 calls, 6,309,732 characters, mean 1,105. Exactly-duplicated
lines account for only 4.2%, so this is elision of stable middles, not
deduplication." This module elides the stable middle of a long command
result while guaranteeing three things stay directly visible in ``encoded``,
with no ``expand`` call required to see them: a non-zero exit status, and
every stderr/traceback/exception/failing-assertion line, wherever in the
output it falls. Structured recognizers for test-runner and build-log
output are layered on top of this in a later revision of this module; this
one is the general case every command result passes through.

There is no separate stdout/stderr/exit-code channel in the observation
this encoder receives — ``raw`` is the single merged
``tool_result.content`` string ``tests/corpus/README.md`` documents. A
non-zero exit status therefore travels as an optional ``request["exit_code"]``
hint (mirroring how ``codec/span.py`` carries ``offset``/``limit``/
``edit_target`` hints alongside a plain-text ``raw``), and is rendered as an
unconditional header line the elision pass below never touches — so it can
never be elided by construction, not merely by heuristic.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from laconic.codec.encoders._elision import (
    DEFAULT_KEEP_HEAD,
    DEFAULT_KEEP_TAIL,
    DEFAULT_MAX_ERRORS,
    collapse_duplicate_lines,
    elide_middle,
    read_exit_code,
    with_exit_header,
)
from laconic.ledger import Ledger, ObservationKind, Record

#: See ``laconic.codec.encoders.file._LONE_SURROGATE``: a lone UTF-16
#: surrogate is a legal ``str`` code point but not valid UTF-8, and the
#: ledger's ``subject``/``encoded`` columns must be storable UTF-8 text.
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _storable(text: str) -> str:
    return _LONE_SURROGATE.sub("\ufffd", text)


class CommandEncoder:
    """Error-salient, duplicate-collapsed encoding of command output.

    Every call registers the encoding with ``ledger`` under
    :attr:`~laconic.ledger.ObservationKind.COMMAND` and returns the
    resulting :class:`~laconic.ledger.Record`; ``encode`` never raises
    regardless of ``raw``'s content.
    """

    def __init__(
        self,
        ledger: Ledger,
        *,
        keep_head: int = DEFAULT_KEEP_HEAD,
        keep_tail: int = DEFAULT_KEEP_TAIL,
        max_errors: int = DEFAULT_MAX_ERRORS,
    ) -> None:
        self._ledger = ledger
        self._keep_head = keep_head
        self._keep_tail = keep_tail
        self._max_errors = max_errors

    def encode(
        self,
        subject: str,
        raw: str,
        request: Mapping[str, object],
        *,
        turn: int,
    ) -> Record:
        exit_code = read_exit_code(request)
        lines = collapse_duplicate_lines(raw.split("\n"))
        result = elide_middle(
            lines,
            keep_head=self._keep_head,
            keep_tail=self._keep_tail,
            max_errors=self._max_errors,
        )
        encoded = with_exit_header(result.text, exit_code)
        return self._ledger.register(
            ObservationKind.COMMAND, _storable(subject), raw, _storable(encoded), turn
        )
