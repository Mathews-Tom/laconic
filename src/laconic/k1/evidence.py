"""Canonical native-transcript evidence contract for K1 eligibility."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from laconic.k1.manifest import Candidate, source_sha256

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


class NativeEvidenceError(ValueError):
    """Raised when native evidence cannot satisfy K1's replay contract."""


@dataclass(frozen=True, slots=True)
class BillableUsage:
    """Native model usage counters without inferred cache values."""

    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("cache_read_tokens", self.cache_read_tokens),
            ("cache_write_tokens", self.cache_write_tokens),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise NativeEvidenceError(f"{field_name} must be a non-negative integer or null")

    @property
    def is_billable(self) -> bool:
        """Return whether required cost counters were recorded natively."""
        return self.input_tokens is not None and self.output_tokens is not None


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One native assistant tool action."""

    call_id: str
    name: str
    input: dict[str, JsonValue]

    def __post_init__(self) -> None:
        _require_nonempty("tool call id", self.call_id)
        _require_nonempty("tool name", self.name)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """One native observation linked to its originating tool action."""

    call_id: str
    output: JsonValue

    def __post_init__(self) -> None:
        _require_nonempty("tool result call_id", self.call_id)


@dataclass(frozen=True, slots=True)
class NativeEvent:
    """One ordered replay-relevant event reconstructed from a native source."""

    index: int
    timestamp: str
    kind: Literal["user_prompt", "assistant", "tool_result"]
    text: str | None = None
    model: str | None = None
    usage: BillableUsage | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_result: ToolResult | None = None

    def __post_init__(self) -> None:
        if self.index < 0:
            raise NativeEvidenceError("event index must be non-negative")
        _parse_timestamp(self.timestamp)
        if self.kind == "user_prompt":
            if self.text is None:
                raise NativeEvidenceError(f"event {self.index}: user_prompt requires text")
            if (
                self.model is not None
                or self.usage is not None
                or self.tool_calls
                or self.tool_result is not None
            ):
                raise NativeEvidenceError(
                    f"event {self.index}: user_prompt carries assistant-only fields"
                )
        elif self.kind == "assistant":
            if self.tool_result is not None:
                raise NativeEvidenceError(
                    f"event {self.index}: assistant cannot carry a tool result"
                )
            if self.text is None and not self.tool_calls:
                raise NativeEvidenceError(
                    f"event {self.index}: assistant requires text or tool calls"
                )
        elif self.kind == "tool_result":
            if self.tool_result is None:
                raise NativeEvidenceError(f"event {self.index}: tool_result requires a result")
            if (
                self.text is not None
                or self.model is not None
                or self.usage is not None
                or self.tool_calls
            ):
                raise NativeEvidenceError(
                    f"event {self.index}: tool_result carries unrelated fields"
                )
        else:
            raise NativeEvidenceError(f"event {self.index}: unknown kind {self.kind!r}")


@dataclass(frozen=True, slots=True)
class NativeSession:
    """Canonical evidence for one native transcript and its parser provenance."""

    candidate_id: str
    provider: str
    parser: str
    source_path: Path
    source_sha256: str
    model: str | None
    events: tuple[NativeEvent, ...]

    def __post_init__(self) -> None:
        _require_nonempty("candidate_id", self.candidate_id)
        _require_nonempty("provider", self.provider)
        _require_nonempty("parser", self.parser)
        if not self.source_path.is_absolute():
            raise NativeEvidenceError("source_path must be absolute")
        _require_nonempty("source_sha256", self.source_sha256)
        if self.model is not None:
            _require_nonempty("model", self.model)
        if not self.events:
            raise NativeEvidenceError("native session requires events")
        if [event.index for event in self.events] != list(range(len(self.events))):
            raise NativeEvidenceError("event indexes must be contiguous and ordered from zero")


def validate_confirmatory_evidence(candidate: Candidate, session: NativeSession) -> None:
    """Fail closed unless native evidence can drive a confirmatory K1 replay."""
    if session.candidate_id != candidate.candidate_id:
        raise NativeEvidenceError("candidate_id does not match manifest")
    if session.provider != candidate.provider:
        raise NativeEvidenceError("provider does not match manifest")
    if session.source_path != candidate.source_path:
        raise NativeEvidenceError("source_path does not match manifest")
    actual_hash = source_sha256(candidate.source_path)
    if actual_hash != candidate.source_sha256 or session.source_sha256 != candidate.source_sha256:
        raise NativeEvidenceError("native source hash does not match manifest")
    if candidate.model is not None and session.model != candidate.model:
        raise NativeEvidenceError("model does not match manifest")
    if session.model is None:
        raise NativeEvidenceError("native session has no model identifier")

    open_calls: set[str] = set()
    seen_calls: set[str] = set()
    saw_prompt = False
    saw_assistant = False
    for event in session.events:
        if event.kind == "user_prompt":
            saw_prompt = True
            continue
        if event.kind == "assistant":
            saw_assistant = True
            if event.usage is None or not event.usage.is_billable:
                raise NativeEvidenceError(
                    f"event {event.index}: assistant usage is missing or incomplete"
                )
            for call in event.tool_calls:
                if call.call_id in seen_calls:
                    raise NativeEvidenceError(
                        f"event {event.index}: duplicate tool call id {call.call_id!r}"
                    )
                seen_calls.add(call.call_id)
                open_calls.add(call.call_id)
            continue
        result = event.tool_result
        if result is None or result.call_id not in open_calls:
            call_id = result.call_id if result is not None else "<missing>"
            raise NativeEvidenceError(f"event {event.index}: unmatched tool result {call_id!r}")
        open_calls.remove(result.call_id)

    if not saw_prompt:
        raise NativeEvidenceError("native session has no user prompt")
    if not saw_assistant:
        raise NativeEvidenceError("native session has no assistant event")
    if open_calls:
        raise NativeEvidenceError(
            f"native session has unmatched tool calls: {sorted(open_calls)!r}"
        )


def _require_nonempty(field_name: str, value: str) -> None:
    if not value.strip():
        raise NativeEvidenceError(f"{field_name} must not be empty")


def _parse_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise NativeEvidenceError(f"event timestamp must be ISO-8601: {value!r}") from error
    if parsed.tzinfo is None:
        raise NativeEvidenceError(f"event timestamp must include a timezone: {value!r}")
