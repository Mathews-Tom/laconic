"""Content-free local Observe receipts (M2): the only persisted view of an
event.

Every field is drawn from the privacy allowlist in
`docs/observe-design.md` § Receipt and privacy boundary: opaque session
identifier, adapter identity and schema version, tool category,
result/argument size bands, success/error class, and timestamp. A
:class:`Receipt` is built directly from a client's raw synthetic
payload -- never from :class:`~laconic.observe.claude_code.ClaudeCodeObservation`
or :class:`~laconic.observe.omp.OmpObservation` alone, since M1's
observations omit size data entirely -- but every size measurement is
reduced to a coarse :class:`SizeBand` immediately after being computed;
no exact character count, argument value, or result body survives
construction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from laconic.observe import claude_code, omp
from laconic.observe.contracts import ClientId, ObserveEventKind

#: Bumped whenever a field is added, removed, or reinterpreted.
RECEIPT_SCHEMA_VERSION = 1


class ToolCategory(StrEnum):
    """A client-agnostic bucket for a tool call. Never the tool's own
    name: a real MCP tool name such as ``mcp__internal-crm__search`` can
    itself leak which internal system a project integrates with, so only
    a coarse category survives into a receipt."""

    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    COMMAND = "command"
    SEARCH = "search"
    NETWORK = "network"
    MCP = "mcp"
    SESSION = "session"
    OTHER = "other"


class SizeBand(StrEnum):
    """A coarse magnitude bucket for an argument or result payload's
    encoded size. Never the exact character count: many receipts landing
    on one exact size could approximate a content fingerprint over time,
    which a band coarsens away."""

    NONE = "none"
    XS = "xs"
    S = "s"
    M = "m"
    L = "l"
    XL = "xl"


class ResultClass(StrEnum):
    """The outcome class an event's receipt records."""

    SUCCESS = "success"
    FAILURE = "failure"
    SESSION_CLOSE = "session_close"


#: Inclusive lower bounds, in ascending order, for each band above
#: :attr:`SizeBand.NONE`.
_SIZE_BAND_THRESHOLDS: tuple[tuple[int, SizeBand], ...] = (
    (1, SizeBand.XS),
    (64, SizeBand.S),
    (512, SizeBand.M),
    (4096, SizeBand.L),
    (32768, SizeBand.XL),
)


def _band(size_chars: int) -> SizeBand:
    band = SizeBand.NONE
    for threshold, candidate in _SIZE_BAND_THRESHOLDS:
        if size_chars >= threshold:
            band = candidate
    return band


def _json_size(value: Any) -> int:
    """Char length of ``value``'s canonical JSON encoding, or 0 for
    ``None``. The encoded string is never retained -- only its length is
    read, and the string itself falls out of scope immediately after."""
    if value is None:
        return 0
    return len(json.dumps(value, sort_keys=True, default=str))


_CLAUDE_CODE_CATEGORY: dict[str, ToolCategory] = {
    "Read": ToolCategory.FILE_READ,
    "Write": ToolCategory.FILE_WRITE,
    "Edit": ToolCategory.FILE_WRITE,
    "NotebookEdit": ToolCategory.FILE_WRITE,
    "Bash": ToolCategory.COMMAND,
    "Grep": ToolCategory.SEARCH,
    "Glob": ToolCategory.SEARCH,
    "WebFetch": ToolCategory.NETWORK,
    "WebSearch": ToolCategory.NETWORK,
}

_OMP_CATEGORY: dict[str, ToolCategory] = {
    "read": ToolCategory.FILE_READ,
    "write": ToolCategory.FILE_WRITE,
    "edit": ToolCategory.FILE_WRITE,
    "bash": ToolCategory.COMMAND,
    "grep": ToolCategory.SEARCH,
    "glob": ToolCategory.SEARCH,
    "browser": ToolCategory.NETWORK,
    "web_search": ToolCategory.NETWORK,
}


def _categorize(tool_name: str | None, table: dict[str, ToolCategory]) -> ToolCategory:
    if tool_name is None:
        return ToolCategory.SESSION
    if tool_name.startswith("mcp__") or tool_name.startswith("mcp_"):
        return ToolCategory.MCP
    return table.get(tool_name, ToolCategory.OTHER)


def _result_class_for(event: ObserveEventKind) -> ResultClass:
    if event is ObserveEventKind.SESSION_CLOSE:
        return ResultClass.SESSION_CLOSE
    if event is ObserveEventKind.TOOL_RESULT_SUCCESS:
        return ResultClass.SUCCESS
    return ResultClass.FAILURE


@dataclass(frozen=True, slots=True)
class Receipt:
    """One content-free local Observe receipt.

    Every field is on the privacy allowlist. Construction (via
    :func:`build_claude_code_receipt` / :func:`build_omp_receipt`) is the
    only place a raw client payload is ever inspected, and only to derive
    these fields.
    """

    schema_version: int
    adapter: ClientId
    adapter_schema_version: int
    session_id: str
    tool_category: ToolCategory
    argument_size: SizeBand
    result_size: SizeBand
    result_class: ResultClass
    timestamp: float

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "adapter": self.adapter.value,
            "adapter_schema_version": self.adapter_schema_version,
            "session_id": self.session_id,
            "tool_category": self.tool_category.value,
            "argument_size": self.argument_size.value,
            "result_size": self.result_size.value,
            "result_class": self.result_class.value,
            "timestamp": self.timestamp,
        }


def build_claude_code_receipt(payload: dict[str, Any], *, now: float) -> Receipt:
    """Build a receipt from one synthetic Claude Code hook payload.

    Reuses :func:`laconic.observe.claude_code.parse_event` for structural
    validation, so an unsupported or malformed payload raises the same
    error that validation raises rather than a second, possibly
    divergent check.
    """
    observation = claude_code.parse_event(payload)
    result_class = _result_class_for(observation.event)
    result_field = "tool_response" if result_class is ResultClass.SUCCESS else "error"
    return Receipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        adapter=ClientId.CLAUDE_CODE,
        adapter_schema_version=1,
        session_id=observation.session_id,
        tool_category=_categorize(observation.tool_name, _CLAUDE_CODE_CATEGORY),
        argument_size=_band(_json_size(payload.get("tool_input"))),
        result_size=_band(_json_size(payload.get(result_field))),
        result_class=result_class,
        timestamp=now,
    )


def build_omp_receipt(payload: dict[str, Any], *, now: float) -> Receipt:
    """Build a receipt from one synthetic, JSON-projected OMP hook event.

    Reuses :func:`laconic.observe.omp.parse_event` for structural
    validation.
    """
    observation = omp.parse_event(payload)
    result_class = _result_class_for(observation.event)
    result_size_chars = _json_size(payload.get("content")) + _json_size(payload.get("details"))
    return Receipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        adapter=ClientId.OMP,
        adapter_schema_version=1,
        session_id=observation.session_id,
        tool_category=_categorize(observation.tool_name, _OMP_CATEGORY),
        argument_size=_band(_json_size(payload.get("input"))),
        result_size=_band(result_size_chars),
        result_class=result_class,
        timestamp=now,
    )
