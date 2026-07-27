"""Command observation encoder: error-salient, duplicate-collapsed elision.

``docs/system-design.md`` §2.2 names Bash the second-largest observation
channel — "5,708 calls, 6,309,732 characters, mean 1,105. Exactly-duplicated
lines account for only 4.2%, so this is elision of stable middles, not
deduplication." This module elides the stable middle of a long command
result while guaranteeing three things stay directly visible in ``encoded``,
with no ``expand`` call required to see them: a non-zero exit status, and
every stderr/traceback/exception/failing-assertion line, wherever in the
output it falls.

Two structured recognizers summarize recognized pytest and build-tool
output instead of running it through generic head/tail elision. A
recognizer is consulted **only when the input is long enough that generic
elision would have applied anyway** (more than ``keep_head + keep_tail``
lines) — the same gate :func:`~laconic.codec.encoders._elision.elide_middle`
uses internally. Below that threshold, ``raw`` is short enough to show in
full regardless of shape, and a recognizer summarizing it anyway would make
recognizing the tool strictly *worse* for error visibility than not
recognizing it, which defeats the point. Both recognizers build their
``diagnostics`` set from
:func:`~laconic.codec.encoders._elision.looks_like_error` — the identical
function the generic path uses — so "never elide an error" has one
implementation whichever path a given input takes.

There is no separate stdout/stderr/exit-code channel in the observation
this encoder receives — ``raw`` is the single merged
``tool_result.content`` string ``tests/corpus/README.md`` documents. A
non-zero exit status therefore travels as an optional ``request["exit_code"]``
hint (mirroring how ``codec/span.py`` carries ``offset``/``limit``/
``edit_target`` hints alongside a plain-text ``raw``), and is rendered as an
unconditional header line neither the recognizer nor the elision pass below
ever touches — so it can never be elided by construction, not merely by
heuristic.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from laconic.codec.encoders._elision import (
    DEFAULT_KEEP_HEAD,
    DEFAULT_KEEP_TAIL,
    DEFAULT_MAX_ERRORS,
    collapse_duplicate_lines,
    elide_middle,
    looks_like_error,
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
        threshold = self._keep_head + self._keep_tail
        if len(lines) <= threshold:
            # Short enough that generic elision would return it unchanged —
            # a recognizer must not summarize what elision would not even
            # touch (that is the B1 defect this gate exists to prevent).
            body = "\n".join(lines)
        else:
            structured = _recognize(lines, max_shown=self._max_errors)
            body = (
                structured
                if structured is not None
                else elide_middle(
                    lines,
                    keep_head=self._keep_head,
                    keep_tail=self._keep_tail,
                    max_errors=self._max_errors,
                ).text
            )
        encoded = with_exit_header(body, exit_code)
        return self._ledger.register(
            ObservationKind.COMMAND, _storable(subject), raw, _storable(encoded), turn
        )


#: pytest's final summary line, bare (``5 passed in 0.12s``) or bordered by
#: ``=`` (``===== 3 passed, 2 failed in 1.23s =====``), including the
#: no-tests-collected case. The outcome-word vocabulary is pytest's own
#: closed set — not a bare ``\w+`` — so an unrelated benchmark line like
#: "1000 iterations in 2.5s" cannot masquerade as a pytest summary. The
#: optional ``(H:MM:SS)`` suffix is pytest's own duration format once a
#: session runs 60 seconds or longer (``_pytest.terminal.format_session_duration``).
_PYTEST_OUTCOME = r"(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)"
_PYTEST_SUMMARY = re.compile(
    rf"^=*\s*(?:\d+ {_PYTEST_OUTCOME}(?:, \d+ {_PYTEST_OUTCOME})*"
    rf" in [\d.]+s(?: \(\d+:\d{{2}}:\d{{2}}\))?|no tests ran in [\d.]+s)\s*=*$"
)

#: Build-tool final tallies. mypy's "Found N errors in M files", ruff's and
#: tsc's (``--pretty``) "Found N errors.", eslint's "N problems (N errors,
#: N warnings)", cargo's "error: aborting due to N previous errors...", and
#: clang's "N errors generated." gcc emits no machine-parseable tally at
#: all and falls through to generic elision, which is the safe default for
#: any build tool not named here.
_BUILD_SUMMARY = re.compile(r"\bfound \d+ errors?\b", re.IGNORECASE)
_BUILD_COUNTS = re.compile(r"\b\d+ errors?,\s*\d+ warnings?\b", re.IGNORECASE)
_BUILD_CARGO = re.compile(r"\bdue to \d+ previous errors?\b", re.IGNORECASE)
_BUILD_CLANG = re.compile(r"\b\d+ errors? generated\.?", re.IGNORECASE)
_WARNING_LINE = re.compile(r"\bwarning[:\[]", re.IGNORECASE)


def _last_matching_index(lines: list[str], matches: Callable[[str], bool]) -> int | None:
    """The index of the LAST line satisfying ``matches``, or ``None``.

    A summary/tally line is by construction the last one of its shape in
    real tool output (the verdict comes after every diagnostic it counts),
    not the first — searching from the end also avoids an early diagnostic
    line that happens to *contain* trigger-shaped text (e.g. a mypy error
    message quoting another tally) being mistaken for the real one.
    """
    for i in range(len(lines) - 1, -1, -1):
        if matches(lines[i]):
            return i
    return None


def _render_summary(
    header: str, lines: list[str], diagnostic_indices: list[int], *, max_shown: int
) -> str:
    """Render a recognizer's condensed block: header, capped diagnostics, tally.

    ``diagnostic_indices`` never contains the trigger line's own index
    (each recognizer excludes it before calling this), so the "how many
    other lines are unaccounted for" arithmetic below cannot double-count
    or under-count a trigger line that also happens to look like a
    diagnostic.
    """
    shown_indices = diagnostic_indices[:max_shown]
    parts = [header]
    parts.extend(f"  {lines[i]}" for i in shown_indices)
    omitted_diagnostics = len(diagnostic_indices) - len(shown_indices)
    if omitted_diagnostics > 0:
        parts.append(f"  [{omitted_diagnostics} further diagnostic lines omitted]")
    omitted_ordinary = len(lines) - 1 - len(diagnostic_indices)
    if omitted_ordinary > 0:
        parts.append(f"  [{omitted_ordinary} other lines summarized]")
    return "\n".join(parts)


def _recognize_pytest(lines: list[str], *, max_shown: int) -> str | None:
    """A pytest summary line, plus every error-salient line, capped and tallied.

    ``diagnostics`` is every line
    :func:`~laconic.codec.encoders._elision.looks_like_error` flags — not
    just lines matching ``FAILED``/``E ``/``AssertionError`` — so a
    collection-time traceback or an exception outside the final summary
    still surfaces here, the same guarantee the generic elision path
    gives.
    """
    trigger_index = _last_matching_index(
        lines, lambda line: bool(_PYTEST_SUMMARY.match(line.strip()))
    )
    if trigger_index is None:
        return None
    summary = lines[trigger_index].strip(" =")
    diagnostic_indices = [
        i for i, line in enumerate(lines) if i != trigger_index and looks_like_error(line)
    ]
    return _render_summary(f"pytest: {summary}", lines, diagnostic_indices, max_shown=max_shown)


def _is_build_trigger(line: str) -> bool:
    return bool(
        _BUILD_SUMMARY.search(line)
        or _BUILD_COUNTS.search(line)
        or _BUILD_CARGO.search(line)
        or _BUILD_CLANG.search(line)
    )


def _recognize_build_log(lines: list[str], *, max_shown: int) -> str | None:
    """A build-tool error/warning tally, plus every diagnostic line, capped."""
    trigger_index = _last_matching_index(lines, _is_build_trigger)
    if trigger_index is None:
        return None
    summary = lines[trigger_index].strip()
    diagnostic_indices = [
        i
        for i, line in enumerate(lines)
        if i != trigger_index and (looks_like_error(line) or _WARNING_LINE.search(line))
    ]
    return _render_summary(f"build: {summary}", lines, diagnostic_indices, max_shown=max_shown)


def _recognize(lines: list[str], *, max_shown: int = DEFAULT_MAX_ERRORS) -> str | None:
    """The structured summary for a recognized command shape, if any.

    Tried in a fixed order; the first recognizer to find its trigger line
    wins. When a single command genuinely produces both shapes (e.g.
    ``uv run mypy --strict src && uv run pytest -q``, this repository's own
    release-verification command), only the pytest summary is shown — the
    build tally is not lost, since it survives as an ordinary diagnostic or
    summarized line and the complete ``raw`` payload is always one
    ``expand`` away.
    """
    return _recognize_pytest(lines, max_shown=max_shown) or _recognize_build_log(
        lines, max_shown=max_shown
    )
