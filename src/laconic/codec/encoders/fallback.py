"""Fallback observation encoder: the safe default for any unrecognized tool.

``docs/system-design.md`` §2.2: "One encoder per tool shape, dispatched by
tool name, with a safe fallback." :mod:`laconic.codec.observe` dispatches
every tool name it does not otherwise recognize to :class:`FallbackEncoder`
so an unfamiliar tool encodes rather than raising — the same posture
``FallbackOutliner`` (M4) takes for an unrecognized file type.

With no known structure to exploit, this encoder does the two things every
tool shape shares: it is text that may be long, and it may carry the same
``request["exit_code"]`` hint a shell-like tool would — an unrecognized
tool name is not evidence the tool has no exit status, only that this
build does not have a dedicated encoder for it yet. Both concerns reuse
:mod:`laconic.codec.encoders._elision`, the same engine
:class:`~laconic.codec.encoders.command.CommandEncoder` builds on, so
"never elide an error" and "never elide a non-zero exit" each have one
implementation, not two.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from laconic.codec.encoders._elision import (
    DEFAULT_KEEP_HEAD,
    DEFAULT_KEEP_TAIL,
    DEFAULT_MAX_ERRORS,
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


class FallbackEncoder:
    """Head/tail, error-salient encoding for any tool this build cannot name.

    Every call registers the encoding with ``ledger`` under
    :attr:`~laconic.ledger.ObservationKind.OTHER` and returns the resulting
    :class:`~laconic.ledger.Record`; ``encode`` never raises regardless of
    ``raw``'s content.
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
        result = elide_middle(
            raw.split("\n"),
            keep_head=self._keep_head,
            keep_tail=self._keep_tail,
            max_errors=self._max_errors,
        )
        encoded = with_exit_header(result.text, exit_code)
        return self._ledger.register(
            ObservationKind.OTHER, _storable(subject), raw, _storable(encoded), turn
        )
