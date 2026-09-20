"""Transforming ``PostToolUse`` adapter for Claude Code.

Claude Code's ``PostToolUse`` hook may return ``updatedToolOutput`` to
replace what the model sees for a tool call that already ran. That is the
one hook field capable of carrying the codec into Claude Code; every other
field only *adds* text. This module is that transport.

It is deliberately **not** part of :mod:`laconic.observe`. Observe's
entrypoint holds a hard no-agent-visible-output invariant (H-46/H-48): it
must never write to stdout on any path. Transformation is the exact
opposite contract — the replacement is delivered *as* stdout. Folding the
two together would mean one program with two contradictory output rules, so
they stay separate programs: Observe records content-free receipts, this
records a codec decision and rewrites the payload.

The decision itself is not reimplemented here. Each invocation drives a
:class:`~laconic.runtime.engine.RuntimeSession`, the same engine the OMP
transport drives, so the strictly-smaller rule, the ledger, reference
minting, and exact recovery are shared rather than forked. This adapter
only translates between Claude Code's hook JSON and that engine.

Two properties of Claude Code's contract drive the design.

**A replacement that does not match the tool's output shape is silently
ignored and the original output is used.** There is no error and no signal
back to the hook. So this module never *constructs* a replacement object
from a schema it believes in: it deep-copies the observed ``tool_response``
and overwrites exactly one text field inside the copy. Unknown and future
keys — Bash's ``noOutputExpected`` and ``gitOperation``, both present in
real transcripts and absent from the published example — survive untouched
because they are never enumerated.

**The hook runs once per tool call in a fresh process.** Sequence numbers
are therefore read back from the ledger at initialize rather than held in
memory, which is what :attr:`InitializeResponse.next_sequence` already
exists to provide.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Final, TextIO, cast

from laconic.runtime.engine import RuntimeSession
from laconic.runtime.protocol import (
    EncodeObservationRequest,
    EncodeObservationResponse,
    InitializeRequest,
    InitializeResponse,
    JsonValue,
    RuntimePolicy,
    ShutdownRequest,
)
from laconic.runtime.references import InvalidSessionIdError, validate_session_id
from laconic.runtime.storage import RuntimeStorage, resolve_data_dir

#: Overrides the runtime storage root. Exists so this repository's tests can
#: point at a scratch directory; a real installed hook never sets it.
DATA_DIR_ENV_VAR: Final = "LACONIC_RUNTIME_DATA_DIR"

#: The hook event this adapter answers. ``PostToolUseFailure`` is excluded:
#: a failed call's output is usually short and always diagnostic, and
#: replacing an error message risks the model proceeding on a false
#: assumption, which Claude Code's own documentation warns about.
POST_TOOL_USE: Final = "PostToolUse"

#: Claude Code's default codec controls, matching the OMP transport's.
DEFAULT_POLICY: Final = RuntimePolicy(
    span_budget=4000,
    keep_head=40,
    keep_tail=40,
    max_errors=20,
)


class _Unsupported(Exception):
    """This payload is not one the adapter transforms. Not an error."""


def _bash_text(response: dict[str, Any]) -> str:
    stdout = response.get("stdout")
    if not isinstance(stdout, str) or not stdout:
        raise _Unsupported("bash result has no stdout text")
    return stdout


def _bash_replace(response: dict[str, Any], text: str) -> dict[str, Any]:
    updated = copy.deepcopy(response)
    updated["stdout"] = text
    return updated


def _read_text(response: dict[str, Any]) -> str:
    if response.get("type") != "text":
        # An image or a split-parts read carries no text to shrink, and its
        # payload is not a string this codec can round-trip.
        raise _Unsupported("read result is not textual")
    file = response.get("file")
    if not isinstance(file, dict):
        raise _Unsupported("read result has no file object")
    content = file.get("content")
    if not isinstance(content, str) or not content:
        raise _Unsupported("read result has no content text")
    return content


def _read_replace(response: dict[str, Any], text: str) -> dict[str, Any]:
    updated = copy.deepcopy(response)
    file = updated["file"]
    if not isinstance(file, dict):  # pragma: no cover -- guarded by _read_text
        raise TypeError("read result file must be an object")
    file["content"] = text
    if "numLines" in file:
        # `numLines` describes the lines actually returned, so it must follow
        # the replacement or the model is told it received more than it can
        # see. `totalLines` and `startLine` describe the file on disk and are
        # still true, so they stay untouched.
        file["numLines"] = text.count("\n") + 1
    return updated


#: Tool name -> (extract the shrinkable text, rebuild the response around it).
#: Only tools whose output shape has been observed in real Claude Code
#: transcripts appear here. `Grep` and `Glob` are absent because Claude Code
#: does not expose them as tools; their work arrives through `Bash`.
_TRANSFORMS: Final = {
    "Bash": (_bash_text, _bash_replace),
    "Read": (_read_text, _read_replace),
}


def _request_id(payload: dict[str, Any], sequence: int) -> str:
    """Return an id unique within the Claude Code session.

    ``tool_use_id`` is the natural key and keeps the ledger row traceable
    to the originating call. The sequence is already the ledger's primary
    key alongside the session, so it is a safe fallback when a client
    omits the field.
    """
    tool_use_id = payload.get("tool_use_id")
    if isinstance(tool_use_id, str) and tool_use_id:
        return tool_use_id
    return f"cc-{sequence}"


def _validated_session_id(session_id: str) -> str:
    """Validate a hook session id without exposing it through hook diagnostics."""
    try:
        return validate_session_id(session_id)
    except InvalidSessionIdError as error:
        raise ValueError("invalid Claude Code session identifier") from error


def transform(payload: dict[str, Any], *, data_dir: Path | None = None) -> dict[str, Any] | None:
    """Return one hook response, or ``None`` to leave the output untouched.

    Raises on genuine failure; :func:`main` is the only caller responsible
    for swallowing it and leaving the original output in place.
    """
    if payload.get("hook_event_name") != POST_TOOL_USE:
        return None
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or tool_name not in _TRANSFORMS:
        return None
    response = payload.get("tool_response")
    if not isinstance(response, dict):
        return None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    working_directory = payload.get("cwd")
    if not isinstance(working_directory, str) or not working_directory:
        return None
    # The engine derives the observation's subject from `tool_input`, and the
    # file encoder reads its span hints from the same object. Dropping it
    # leaves the outliner unable to identify the language and silently
    # degrades a file encoding into a far weaker one.
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    extract, rebuild = _TRANSFORMS[tool_name]
    try:
        raw_text = extract(response)
    except _Unsupported:
        return None

    checked_session_id = _validated_session_id(session_id)
    data_root = resolve_data_dir(data_dir)
    storage = RuntimeStorage(data_root)
    with storage.session_lock(checked_session_id):
        session = RuntimeSession()
        try:
            initialized = session.handle(
                InitializeRequest(
                    request_id="cc-init",
                    session_id=checked_session_id,
                    working_directory=working_directory,
                    data_directory=str(data_root),
                    policy=DEFAULT_POLICY,
                )
            )
            assert isinstance(initialized, InitializeResponse)
            encoded = session.handle(
                EncodeObservationRequest(
                    # Unique per session, or the ledger rejects the second call:
                    # every tool call in one Claude Code session shares a
                    # session_id, so a constant id would transform the first
                    # observation and fail open on every one after it.
                    request_id=_request_id(payload, initialized.next_sequence),
                    tool_name=tool_name,
                    tool_input=cast("dict[str, JsonValue]", tool_input),
                    raw_text=raw_text,
                    success=True,
                    sequence=initialized.next_sequence,
                )
            )
            assert isinstance(encoded, EncodeObservationResponse)
        finally:
            try:
                session.handle(ShutdownRequest(request_id="cc-shutdown"))
            except Exception:  # noqa: BLE001 -- shutdown must not mask a decision
                pass
            try:
                session.close()
            except Exception:  # noqa: BLE001 -- descriptor release must proceed
                pass

    if encoded.decision != "emitted" or encoded.content is None:
        # The engine declined: the encoding was not strictly smaller. Leaving
        # the output alone is the correct outcome, not a failure.
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": POST_TOOL_USE,
            "updatedToolOutput": rebuild(response, encoded.content),
        }
    }


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Entry point. Always returns 0 and never leaves partial output.

    Fail-open is the whole safety story: on any malformed payload, storage
    failure, or unexpected error, the adapter writes nothing and Claude Code
    keeps the original tool result. A crash here costs compression, never
    correctness. Diagnostics go to stderr, which an exit-0 hook sends to the
    client's debug log rather than to the model.
    """
    parser = argparse.ArgumentParser(prog="laconic-claude-code-hook", add_help=False)
    parser.add_argument("--data-dir", type=Path, default=None)
    out = stdout or sys.stdout
    try:
        args = parser.parse_args(argv)
        payload = json.loads((stdin or sys.stdin).read())
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        response = transform(payload, data_dir=args.data_dir)
        if response is not None:
            # Serialized only once the decision is complete, so a failure
            # mid-encode can never emit a truncated replacement.
            out.write(json.dumps(response))
    except SystemExit as error:
        print(f"laconic-claude-code: argument parsing failed: {error}", file=sys.stderr)
    except Exception as error:  # noqa: BLE001 -- fail-open by design
        print(f"laconic-claude-code: {type(error).__name__}: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
