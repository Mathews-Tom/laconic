"""Shared elision primitives for :class:`~laconic.codec.encoders.fallback.FallbackEncoder`
and :class:`~laconic.codec.encoders.command.CommandEncoder`.

``docs/system-design.md`` §2.2 states three rules every encoder obeys; the
two here are the ones an eliding encoder must actively enforce (the third,
"never elide code about to be edited", is ``FileEncoder``'s ``edit_target``
concern and does not apply to command-shaped or unrecognized-tool output):

1. **Never elide an error.** Stderr, non-zero exits, tracebacks, and failing
   assertions are always shown directly in ``encoded`` — never only
   recoverable through an ``expand`` call.
2. **Never elide silently.** Every removed region leaves a visible marker;
   the record's own handle is what recovers everything regardless
   (``ledger.expand(handle)`` always returns the untouched ``raw`` payload,
   whatever ``encoded`` elided — that guarantee lives in the ledger, not
   here).

There is no separate stdout/stderr/exit-code channel in the observation
this module receives: ``tests/corpus/README.md`` documents a single
``tool_result.content`` string, and ``raw`` here is exactly that merged
text. "Stderr", "traceback", and "failing assertion" salience is therefore
a content heuristic over that merged text — the only channel available —
not a structural split. Erring toward over-matching is the safe direction:
a false-positive "error line" costs a little compression, a false negative
would drop real diagnostic content the rules forbid dropping. The
extracted-error cap (:data:`DEFAULT_MAX_ERRORS`) bounds this on genuinely
pathological input; a marker always reports what the cap hid, so nothing
disappears without a trace, and the complete payload is always one
``expand`` away.

Duplicate-line collapse only merges *consecutive* identical lines (a
``uniq -c`` shape), not every non-adjacent repeat: two textually identical
lines separated by unrelated output are two distinct occurrences of
something happening twice, and collapsing them across that gap would
discard the fact that it recurred at all. The repeat count is folded
*into* the surviving line (``line  [xN]``) rather than emitted as a
separate marker line, so an error line that repeated stays classifiable as
an error line — a standalone marker would not itself look like an error
and could be elided out from under the very count it describes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

#: Lines of head and tail kept verbatim by default. Errors inside the
#: elided middle are still pulled out (see :func:`elide_middle`), so these
#: bounds trade compression for readability, not correctness.
DEFAULT_KEEP_HEAD = 40
DEFAULT_KEEP_TAIL = 40

#: Extracted error lines shown before the elision marker are themselves
#: capped, so a run of thousands of matching lines cannot reproduce the
#: whole elided region under a different name.
DEFAULT_MAX_ERRORS = 20

_BODY_INDENT = "  "

#: Each pattern names one category the acceptance text calls out by name,
#: or a real-world shape that category takes. Deliberately over-inclusive
#: per the module rationale above. A gutter prefix (``[\s|+>-]*``) is
#: allowed before the traceback/exception shapes so a nested
#: ``ExceptionGroup`` (Python 3.11+, rendered with a ``|`` gutter) or a
#: Docker BuildKit / CI timestamp-prefixed line is still recognized.
_GUTTER = r"[\s|+>-]*"
_ERROR_PATTERNS = (
    # Python tracebacks: the header line, and the "File "..."" frame lines
    # that make the trace itself legible rather than just its final cause.
    re.compile(rf"^{_GUTTER}Traceback \(most recent call last\):\s*$"),
    re.compile(rf'^{_GUTTER}File "[^"]+", line \d+'),
    # "SomeError: message" / "some.module.SomeException: message" — the
    # exception summary line any traceback ends with, and the same shape a
    # bare raised exception produces with no traceback at all. The suffix
    # alternation also covers exception types that do not end in
    # "Error"/"Exception" (KeyboardInterrupt, SystemExit, StopIteration,
    # socket.timeout).
    re.compile(
        rf"^{_GUTTER}[A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit|Iteration|Timeout|timeout)"
        r"\b(?::.*)?$"
    ),
    # pytest: "FAILED path::test - Reason", collection/fixture "ERROR ...",
    # and the ``-v``/``-rA`` progress-line shape "path::test FAILED  [50%]"
    # where the outcome word trails the test id rather than leading it —
    # so this is unanchored, not ``^``-anchored. Also the "E   assert ..."
    # / "E   AssertionError: ..." detail lines pytest emits for every line
    # of a failure body (not just the first), a bare ``AssertionError``,
    # and Python 3.11+'s ``ExceptionGroup`` (its class name does not end in
    # "Exception"/"Error", so the suffix pattern above cannot catch it).
    re.compile(r"\b(?:FAILED|ERROR)\b"),
    re.compile(r"^E\s"),
    re.compile(r"\bAssertionError\b"),
    re.compile(r"\bExceptionGroup\b"),
    # Other ecosystems' conventional fatal-error markers: git's/rustc's
    # bare-line "fatal:"/"panic:" prefix; the "error:" marker mypy, tsc,
    # gcc, and (bracketed) rustc's "error[E0308]:" all place after a
    # "path:line:col: " prefix as often as at the start of the line; and
    # npm's "npm ERR!" prefix.
    re.compile(r"^(fatal|panic):", re.IGNORECASE),
    re.compile(r"\berror(\[[^\]]*\])?:", re.IGNORECASE),
    re.compile(r"^npm ERR!"),
    # POSIX process-termination and permission signals that carry no
    # colon-delimited marker at all.
    re.compile(r"^(Segmentation fault|Bus error|Aborted|Killed|Illegal instruction)\b"),
    re.compile(r"\b(permission denied|no such file or directory|core dumped)\b", re.IGNORECASE),
    # make's "*** [target] Error N" and common linker failures.
    re.compile(r"^make(\[\d+\])?: \*\*\*"),
    re.compile(r"\bundefined reference\b"),
    re.compile(r"\bsymbol\(s\) not found\b"),
    # A line explicitly tagged as originating on stderr, when a caller's
    # request carries that distinction into the merged text.
    re.compile(r"\bstderr\b", re.IGNORECASE),
)


def looks_like_error(line: str) -> bool:
    """True when ``line`` matches one of the protected error categories."""
    return any(pattern.search(line) for pattern in _ERROR_PATTERNS)


def collapse_duplicate_lines(lines: list[str]) -> list[str]:
    """Collapse a run of consecutive identical lines to one annotated line.

    A run of ``N`` identical lines becomes one surviving line with a
    ``  [xN]`` suffix folded in, rather than the line plus a separate
    marker line — this is what keeps a repeated error line classifiable
    as an error line by :func:`looks_like_error` (a bare marker line would
    not match any pattern, and could itself be elided out from under the
    count it was reporting). A run of one line is returned unchanged.
    Ordering of every surviving line is exactly the order it first appears
    in ``lines`` — this only ever removes elements, never reorders them.
    """
    if not lines:
        return []

    def _rendered(line: str, run: int) -> str:
        return line if run == 1 else f"{line}{_BODY_INDENT}[x{run}]"

    collapsed: list[str] = []
    previous = lines[0]
    run = 1
    for line in lines[1:]:
        if line == previous:
            run += 1
            continue
        collapsed.append(_rendered(previous, run))
        previous = line
        run = 1
    collapsed.append(_rendered(previous, run))
    return collapsed


@dataclass(frozen=True, slots=True)
class ElisionResult:
    """The rendered text plus whether anything was actually elided."""

    text: str
    elided: bool


def elide_middle(
    lines: list[str],
    *,
    keep_head: int = DEFAULT_KEEP_HEAD,
    keep_tail: int = DEFAULT_KEEP_TAIL,
    max_errors: int = DEFAULT_MAX_ERRORS,
) -> ElisionResult:
    """Keep the head and tail verbatim; summarize the middle, errors first.

    The head and tail are never truncated, so any error line inside either
    region survives automatically. Error lines inside the elided middle are
    additionally extracted and shown, so an error is never visible only to
    an ``expand`` call — it is always in ``encoded`` directly.
    """
    if len(lines) <= keep_head + keep_tail:
        return ElisionResult(text="\n".join(lines), elided=False)

    head = lines[:keep_head]
    tail = lines[len(lines) - keep_tail :] if keep_tail else []
    middle = lines[keep_head : len(lines) - keep_tail]
    errors = [line for line in middle if looks_like_error(line)]

    parts = [*head]
    if errors:
        shown = errors[:max_errors]
        omitted_error_count = len(errors) - len(shown)
        parts.append(f"{_BODY_INDENT}[{len(errors)} error lines from the elided region]")
        parts.extend(f"{_BODY_INDENT}{error}" for error in shown)
        if omitted_error_count > 0:
            parts.append(f"{_BODY_INDENT}[{omitted_error_count} further error lines omitted]")
    parts.append(f"{_BODY_INDENT}[... {len(middle)} lines elided — expand with the handle]")
    parts.extend(tail)
    return ElisionResult(text="\n".join(parts), elided=True)


#: The request key a caller uses to carry a command's exit status alongside
#: the merged ``raw`` text (there is no separate exit-code channel — see
#: the module docstring). Shared so any encoder — not just
#: :class:`~laconic.codec.encoders.command.CommandEncoder` — can surface a
#: non-zero exit unconditionally.
REQUEST_EXIT_CODE_KEY = "exit_code"


def read_exit_code(request: Mapping[str, object]) -> int | None:
    """The command's exit status, if ``request`` names one validly.

    ``bool`` is rejected even though it is an ``int`` subclass — a stray
    ``True``/``False`` is not an exit status — matching
    ``codec/span.py``'s ``_bounded_int`` convention.
    """
    value = request.get(REQUEST_EXIT_CODE_KEY)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def with_exit_header(body: str, exit_code: int | None) -> str:
    """Prefix a non-zero exit status, unconditionally kept — never elided.

    Applied *after* elision, so a non-zero exit cannot be elided by
    construction, not merely by heuristic.
    """
    if exit_code is None or exit_code == 0:
        return body
    header = f"exit {exit_code}"
    return f"{header}\n{body}" if body else header
