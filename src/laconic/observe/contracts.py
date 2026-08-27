"""Shared client-adapter contract models for Laconic Observe.

Established by the M1 compatibility spike
(`docs/observe-design.md` §§ Client adapters, Compatibility spike). A
:class:`AdapterContract` describes what one client's hook subsystem
verifiably provides -- event coverage, install mechanism, config
locations, and timeout behavior -- with a citation to the source that was
inspected. These models carry no runtime behavior: nothing here installs
a hook, reads a real client configuration, or processes a real event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ClientId(StrEnum):
    """A client Laconic Observe has an independently versioned adapter for."""

    CLAUDE_CODE = "claude-code"
    OMP = "omp"


class ObserveEventKind(StrEnum):
    """Normalized event categories Observe distinguishes, independent of
    any one client's event names.

    A client is compatible only if it can deliver both
    :attr:`TOOL_RESULT_SUCCESS` (or :attr:`TOOL_RESULT_FAILURE`) and
    :attr:`SESSION_CLOSE` through a verified lifecycle event --
    pre-execution events alone cannot observe a completed result.
    """

    TOOL_RESULT_SUCCESS = "tool_result_success"
    TOOL_RESULT_FAILURE = "tool_result_failure"
    SESSION_CLOSE = "session_close"


class InstallMechanism(StrEnum):
    """How a client's hook configuration is owned and mutated.

    Claude Code hooks are entries merged into a JSON settings array under
    a shared ``hooks`` key; OMP hooks are single files dropped into a
    discovery directory. The two installers cannot share a write
    strategy, so each is modeled and implemented independently.
    """

    JSON_ENTRY_MERGE = "json_entry_merge"
    FILE_DROP = "file_drop"


@dataclass(frozen=True, slots=True)
class ConfigLocation:
    """One place a client reads Observe's (would-be) hook configuration from."""

    scope: str
    """``"project"`` or ``"user"``."""

    path: str
    """Display path. May contain a client-defined placeholder such as
    ``<cwd>`` or ``~``; never a path resolved against this machine."""

    shareable: bool
    """Whether this location is meant to be committed to a shared repo."""

    notes: str = ""


@dataclass(frozen=True, slots=True)
class AdapterContract:
    """One client's verified, source-cited hook contract.

    ``supported_events`` lists every :class:`ObserveEventKind` this client
    can deliver through at least one event confirmed against ``source``.
    :meth:`go` is the M1 GO/NO-GO test: a client is compatible only when
    it can deliver a completed-tool-result signal and a session-close
    signal without falling back to a pre-execution-only substitute.
    """

    client: ClientId
    supported_events: tuple[ObserveEventKind, ...]
    install_mechanism: InstallMechanism
    config_locations: tuple[ConfigLocation, ...]
    default_timeout_seconds: float
    max_timeout_seconds: float
    source: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def go(self) -> bool:
        """Return whether this client clears the M1 compatibility bar."""
        required = {ObserveEventKind.TOOL_RESULT_SUCCESS, ObserveEventKind.SESSION_CLOSE}
        return required <= set(self.supported_events)
