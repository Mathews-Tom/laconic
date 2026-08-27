"""Claude Code compatibility spike: verified contract and synthetic event
normalization.

Every fact in :data:`CLAUDE_CODE_CONTRACT` is cited to the hooks reference
inspected for the M1 design gate (`.docs/DEVELOPMENT_PLAN_HISTORY.md`
H-46). :func:`parse_event` only ever runs against synthetic fixtures in
this repository's test suite -- it never reads a real hook invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from laconic.observe.contracts import (
    AdapterContract,
    ClientId,
    ConfigLocation,
    InstallMechanism,
    ObserveEventKind,
)

#: Hook event name for a successfully completed tool call.
POST_TOOL_USE = "PostToolUse"

#: Hook event name for a tool call that failed.
POST_TOOL_USE_FAILURE = "PostToolUseFailure"

#: Hook event name for session termination.
SESSION_END = "SessionEnd"

_SUPPORTED_EVENT_NAMES = frozenset({POST_TOOL_USE, POST_TOOL_USE_FAILURE, SESSION_END})

CLAUDE_CODE_CONTRACT = AdapterContract(
    client=ClientId.CLAUDE_CODE,
    supported_events=(
        ObserveEventKind.TOOL_RESULT_SUCCESS,
        ObserveEventKind.TOOL_RESULT_FAILURE,
        ObserveEventKind.SESSION_CLOSE,
    ),
    install_mechanism=InstallMechanism.JSON_ENTRY_MERGE,
    config_locations=(
        ConfigLocation(
            scope="user",
            path="~/.claude/settings.json",
            shareable=False,
            notes="applies to all projects on this machine",
        ),
        ConfigLocation(
            scope="project",
            path=".claude/settings.json",
            shareable=True,
            notes="committable; distinct from the Claude-Code-managed, "
            "gitignored .claude/settings.local.json, which Observe must not target",
        ),
    ),
    default_timeout_seconds=600.0,
    max_timeout_seconds=60.0,
    source="https://code.claude.com/docs/en/hooks (fetched 2026-08-27)",
    notes=(
        "PostToolUse (success) and PostToolUseFailure (failure) jointly cover the "
        "completed-tool-result signal that PreToolUse cannot provide.",
        "SessionEnd hooks share a 1.5s default timeout budget across every configured "
        "SessionEnd hook; a hook entry may raise its own share up to 60s via its "
        "declared `timeout` field.",
        "Exit 0 with empty stdout is fully silent for all three events. A non-zero "
        "exit surfaces stderr to Claude for PostToolUse/PostToolUseFailure, or to the "
        "user for SessionEnd -- so the hook subprocess must exit 0 on every path.",
    ),
)


@dataclass(frozen=True, slots=True)
class ClaudeCodeObservation:
    """A normalized, content-free view of one synthetic Claude Code hook payload.

    Carries only fields the design's privacy boundary permits into a
    receipt (`docs/observe-design.md` § Receipt and privacy boundary):
    tool name, session id, event class, and duration. Never carries
    ``tool_input``, ``tool_response``, ``transcript_path`` contents, or
    any other payload field.
    """

    event: ObserveEventKind
    tool_name: str | None
    session_id: str
    duration_ms: int | None


class UnsupportedEventError(ValueError):
    """The payload's ``hook_event_name`` is not one Observe subscribes to."""


class MalformedPayloadError(ValueError):
    """The payload is missing a field required for its event, or a field
    has the wrong type."""


def parse_event(payload: dict[str, Any]) -> ClaudeCodeObservation:
    """Normalize one synthetic Claude Code hook payload.

    Raises :class:`UnsupportedEventError` for any ``hook_event_name``
    outside :data:`_SUPPORTED_EVENT_NAMES` -- Observe never guesses an
    adapter for an event it has not verified. Raises
    :class:`MalformedPayloadError` when a required field is missing or
    mistyped.
    """
    event_name = payload.get("hook_event_name")
    if event_name not in _SUPPORTED_EVENT_NAMES:
        raise UnsupportedEventError(f"unsupported hook_event_name: {event_name!r}")

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise MalformedPayloadError("missing or empty session_id")

    if event_name == SESSION_END:
        return ClaudeCodeObservation(
            event=ObserveEventKind.SESSION_CLOSE,
            tool_name=None,
            session_id=session_id,
            duration_ms=None,
        )

    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        raise MalformedPayloadError("missing or empty tool_name")

    duration_ms = payload.get("duration_ms")
    if duration_ms is not None and not isinstance(duration_ms, int):
        raise MalformedPayloadError("duration_ms must be an int when present")

    kind = (
        ObserveEventKind.TOOL_RESULT_SUCCESS
        if event_name == POST_TOOL_USE
        else ObserveEventKind.TOOL_RESULT_FAILURE
    )
    return ClaudeCodeObservation(
        event=kind, tool_name=tool_name, session_id=session_id, duration_ms=duration_ms
    )
