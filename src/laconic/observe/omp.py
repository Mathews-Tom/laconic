"""OMP compatibility spike: verified contract and synthetic event
normalization.

OMP hooks are in-process JS/TS extension modules
(`omp://hooks.md`, `omp://skills/authoring-hooks.md`), not subprocesses.
`parse_event` here models the JSON-serializable envelope a discovered
extension shim would forward to a shared bounded subprocess entrypoint on
``tool_result``/``session_shutdown`` -- it never runs against a real
event, and no such shim is installed or invoked by this module. Every
fact in :data:`OMP_CONTRACT` is cited to the M1 design gate
(`.docs/DEVELOPMENT_PLAN_HISTORY.md` H-46).
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

#: Event name for a completed tool call (success or failure; see ``isError``).
TOOL_RESULT = "tool_result"

#: Event name for session termination.
SESSION_SHUTDOWN = "session_shutdown"

_SUPPORTED_EVENT_NAMES = frozenset({TOOL_RESULT, SESSION_SHUTDOWN})

OMP_CONTRACT = AdapterContract(
    client=ClientId.OMP,
    supported_events=(
        ObserveEventKind.TOOL_RESULT_SUCCESS,
        ObserveEventKind.TOOL_RESULT_FAILURE,
        ObserveEventKind.SESSION_CLOSE,
    ),
    install_mechanism=InstallMechanism.FILE_DROP,
    config_locations=(
        ConfigLocation(
            scope="project",
            path="<cwd>/.omp/extensions/laconic-observe.ts",
            shareable=True,
            notes="committable; cwd-only, does not walk ancestor directories",
        ),
        ConfigLocation(
            scope="user",
            path="<agent-dir>/extensions/laconic-observe.ts",
            shareable=False,
            notes="default ~/.omp/agent/extensions; profile- and "
            "PI_CODING_AGENT_DIR-aware, so the resolved path is not a fixed constant",
        ),
    ),
    default_timeout_seconds=0.0,
    max_timeout_seconds=0.0,
    source="omp://hooks.md, omp://skills/authoring-hooks.md, "
    "omp://extension-loading.md (fetched 2026-08-27)",
    notes=(
        "OMP hooks are in-process JS/TS extension modules (pi.on(event, handler)), "
        "not subprocesses; tool_result (post-execution) and session_shutdown jointly "
        "cover the completed-tool-result and session-close signals.",
        "ExtensionRunner catches handler exceptions after load (fail-open at the "
        "runner level), but does not bound a handler's own child process -- a shim "
        "that shells out via pi.exec must enforce its own hard timeout.",
        "Install is a single owned file drop, not a JSON-array merge; ownership must "
        "be verified by an embedded content marker, not by filename alone.",
    ),
)


@dataclass(frozen=True, slots=True)
class OmpObservation:
    """A normalized, content-free view of one synthetic OMP hook event.

    Carries only fields the design's privacy boundary permits into a
    receipt: tool name, session id, and event class. Never carries
    ``input``, ``content``, ``details``, or any other event field.
    """

    event: ObserveEventKind
    tool_name: str | None
    session_id: str


class UnsupportedEventError(ValueError):
    """The payload's ``event`` is not one Observe subscribes to."""


class MalformedPayloadError(ValueError):
    """The payload is missing a field required for its event, or a field
    has the wrong type."""


def parse_event(payload: dict[str, Any]) -> OmpObservation:
    """Normalize one synthetic, JSON-projected OMP hook event.

    Raises :class:`UnsupportedEventError` for any ``event`` outside
    :data:`_SUPPORTED_EVENT_NAMES`. Raises :class:`MalformedPayloadError`
    when a required field is missing or mistyped.
    """
    event_name = payload.get("event")
    if event_name not in _SUPPORTED_EVENT_NAMES:
        raise UnsupportedEventError(f"unsupported event: {event_name!r}")

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise MalformedPayloadError("missing or empty session_id")

    if event_name == SESSION_SHUTDOWN:
        return OmpObservation(
            event=ObserveEventKind.SESSION_CLOSE, tool_name=None, session_id=session_id
        )

    tool_name = payload.get("toolName")
    if not isinstance(tool_name, str) or not tool_name:
        raise MalformedPayloadError("missing or empty toolName")

    is_error = payload.get("isError")
    if not isinstance(is_error, bool):
        raise MalformedPayloadError("isError must be a bool")

    kind = (
        ObserveEventKind.TOOL_RESULT_FAILURE if is_error else ObserveEventKind.TOOL_RESULT_SUCCESS
    )
    return OmpObservation(event=kind, tool_name=tool_name, session_id=session_id)
