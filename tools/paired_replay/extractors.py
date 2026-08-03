"""Fail-closed native transcript extractors for paired replay evidence."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from tools.paired_replay.evidence import (
    BillableUsage,
    JsonValue,
    NativeEvent,
    NativeEvidenceError,
    NativeSession,
    ToolCall,
    ToolResult,
)
from tools.paired_replay.manifest import Candidate, source_sha256


@dataclass(slots=True)
class _ClaudeAssistant:
    """Fragments of one streamed Claude assistant message."""

    first_line: int
    timestamp: str
    model: str | None
    usage: BillableUsage | None
    text_parts: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    seen_blocks: set[str] = field(default_factory=set)


def extract_native(candidate: Candidate) -> NativeSession:
    """Extract a supported native transcript selected by a paired replay manifest."""
    if candidate.provider == "claude-code":
        return extract_claude_code(candidate)
    if candidate.provider == "omp":
        return extract_omp(candidate)
    if candidate.provider == "codex":
        return extract_codex(candidate)
    raise NativeEvidenceError(f"unsupported native provider {candidate.provider!r}")


def extract_claude_code(candidate: Candidate) -> NativeSession:
    """Extract Claude Code JSONL without inferring streamed event relationships."""
    entries: list[tuple[str, object]] = []
    assistants: dict[str, _ClaudeAssistant] = {}
    for line_number, record in _records(candidate.source_path):
        message = _mapping(record.get("message"))
        role = message.get("role")
        if role == "user":
            _append_claude_user_entries(entries, message, record, line_number)
            continue
        if role != "assistant":
            continue
        message_id = _text(message.get("id"))
        if message_id is None:
            raise _error(line_number, "assistant message has no id")
        fragment = assistants.get(message_id)
        if fragment is None:
            fragment = _ClaudeAssistant(
                first_line=line_number,
                timestamp=_timestamp(record, line_number),
                model=_optional_text(message.get("model"), line_number, "assistant model"),
                usage=_claude_usage(message.get("usage"), line_number),
            )
            assistants[message_id] = fragment
            entries.append(("assistant", message_id))
        else:
            _merge_claude_fragment(fragment, message, record, line_number)
        _append_claude_blocks(fragment, message.get("content"), line_number)

    events = _build_claude_events(entries, assistants)
    return _session(candidate, "claude-code-jsonl-v1", events)


def extract_codex(candidate: Candidate) -> NativeSession:
    """Reject Codex JSONL when its usage cannot identify a response event."""
    pending_calls: set[str] = set()
    saw_usage = False
    for line_number, record in _records(candidate.source_path):
        record_type = record.get("type")
        payload = _mapping(record.get("payload"))
        payload_type = payload.get("type")
        if record_type == "response_item" and payload_type in {
            "custom_tool_call",
            "function_call",
        }:
            call_id = _required_text(payload.get("call_id"), line_number, "tool call id")
            if call_id in pending_calls:
                raise _error(line_number, f"duplicate tool call id {call_id!r}")
            pending_calls.add(call_id)
        elif record_type == "response_item" and payload_type in {
            "custom_tool_call_output",
            "function_call_output",
        }:
            call_id = _required_text(payload.get("call_id"), line_number, "tool result id")
            if call_id not in pending_calls:
                raise _error(line_number, f"unmatched tool result {call_id!r}")
            pending_calls.remove(call_id)
        elif record_type == "event_msg" and payload_type == "token_count":
            info = _mapping(payload.get("info"))
            usage = _mapping(info.get("last_token_usage"))
            _counter(usage, "input_tokens", line_number)
            _counter(usage, "output_tokens", line_number)
            _counter(usage, "cached_input_tokens", line_number)
            saw_usage = True
    if pending_calls:
        raise NativeEvidenceError(
            f"native source has unmatched tool calls: {sorted(pending_calls)!r}",
            cause="missing_evidence",
        )
    if saw_usage:
        raise NativeEvidenceError(
            "Codex token_count usage has no response identifier; cannot associate billable usage",
            cause="missing_evidence",
        )
    raise NativeEvidenceError(
        "Codex native source has no billable usage evidence", cause="missing_evidence"
    )


def extract_omp(candidate: Candidate) -> NativeSession:
    """Extract OMP JSONL messages with their recorded usage and tool ids."""
    events: list[NativeEvent] = []
    declared_models: set[str] = set()
    for line_number, record in _records(candidate.source_path):
        if record.get("type") == "model_change":
            declared_models.add(_required_text(record.get("model"), line_number, "model change"))
            continue
        if record.get("type") != "message":
            continue
        message = _mapping(record.get("message"))
        role = message.get("role")
        timestamp = _timestamp(record, line_number)
        if role == "user":
            text = _text_content(message.get("content"))
            if text is not None:
                events.append(NativeEvent(len(events), timestamp, "user_prompt", text=text))
            continue
        if role == "assistant":
            tool_calls = _omp_tool_calls(message.get("content"), line_number)
            text = _text_content(message.get("content"))
            if text is None and not tool_calls:
                continue
            events.append(
                NativeEvent(
                    len(events),
                    timestamp,
                    "assistant",
                    text=text,
                    model=_optional_text(message.get("model"), line_number, "assistant model"),
                    usage=_omp_usage(message.get("usage"), line_number),
                    tool_calls=tuple(tool_calls),
                )
            )
            continue
        if role == "toolResult":
            call_id = _required_text(message.get("toolCallId"), line_number, "tool result id")
            events.append(
                NativeEvent(
                    len(events),
                    timestamp,
                    "tool_result",
                    tool_result=ToolResult(
                        call_id, _json_value(message.get("content"), line_number)
                    ),
                )
            )
    return _session(candidate, "omp-jsonl-v1", events, declared_models)


def _append_claude_user_entries(
    entries: list[tuple[str, object]],
    message: dict[str, JsonValue],
    record: dict[str, JsonValue],
    line_number: int,
) -> None:
    timestamp = _timestamp(record, line_number)
    content = message.get("content")
    if isinstance(content, str):
        entries.append(("user_prompt", (timestamp, content)))
        return
    blocks = _list(content, line_number, "user content")
    text_parts: list[str] = []
    for block in blocks:
        item = _mapping(block)
        block_type = item.get("type")
        if block_type == "text":
            text = _text(item.get("text"))
            if text is None:
                raise _error(line_number, "user text block has no text")
            text_parts.append(text)
        elif block_type == "tool_result":
            if text_parts:
                entries.append(("user_prompt", (timestamp, "\n".join(text_parts))))
                text_parts.clear()
            call_id = _required_text(item.get("tool_use_id"), line_number, "tool result id")
            entries.append(
                (
                    "tool_result",
                    (timestamp, ToolResult(call_id, _json_value(item.get("content"), line_number))),
                )
            )
    if text_parts:
        entries.append(("user_prompt", (timestamp, "\n".join(text_parts))))


def _merge_claude_fragment(
    fragment: _ClaudeAssistant,
    message: dict[str, JsonValue],
    record: dict[str, JsonValue],
    line_number: int,
) -> None:
    _timestamp(record, line_number)
    model = _optional_text(message.get("model"), line_number, "assistant model")
    usage = _claude_usage(message.get("usage"), line_number)
    if model != fragment.model:
        raise _error(line_number, "repeated assistant message changed model")
    if usage != fragment.usage:
        raise _error(line_number, "repeated assistant message changed usage")


def _append_claude_blocks(fragment: _ClaudeAssistant, content: JsonValue, line_number: int) -> None:
    for block in _list(content, line_number, "assistant content"):
        item = _mapping(block)
        encoded = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if encoded in fragment.seen_blocks:
            continue
        fragment.seen_blocks.add(encoded)
        block_type = item.get("type")
        if block_type == "text":
            text = _text(item.get("text"))
            if text is None:
                raise _error(line_number, "assistant text block has no text")
            fragment.text_parts.append(text)
        elif block_type == "tool_use":
            fragment.tool_calls.append(
                ToolCall(
                    _required_text(item.get("id"), line_number, "tool call id"),
                    _required_text(item.get("name"), line_number, "tool call name"),
                    _mapping(item.get("input")),
                )
            )


def _build_claude_events(
    entries: list[tuple[str, object]], assistants: dict[str, _ClaudeAssistant]
) -> tuple[NativeEvent, ...]:
    events: list[NativeEvent] = []
    for kind, data in entries:
        if kind == "user_prompt":
            if not isinstance(data, tuple) or len(data) != 2:
                raise NativeEvidenceError("invalid Claude user entry")
            timestamp, text = data
            if not isinstance(timestamp, str) or not isinstance(text, str):
                raise NativeEvidenceError("invalid Claude user entry")
            events.append(NativeEvent(len(events), timestamp, "user_prompt", text=text))
        elif kind == "tool_result":
            if not isinstance(data, tuple) or len(data) != 2:
                raise NativeEvidenceError("invalid Claude tool result entry")
            timestamp, result = data
            if not isinstance(timestamp, str) or not isinstance(result, ToolResult):
                raise NativeEvidenceError("invalid Claude tool result entry")
            events.append(NativeEvent(len(events), timestamp, "tool_result", tool_result=result))
        else:
            if not isinstance(data, str):
                raise NativeEvidenceError("invalid Claude assistant entry")
            fragment = assistants[data]
            if not fragment.text_parts and not fragment.tool_calls:
                continue
            text = "\n".join(fragment.text_parts) if fragment.text_parts else None
            events.append(
                NativeEvent(
                    len(events),
                    fragment.timestamp,
                    "assistant",
                    text=text,
                    model=fragment.model,
                    usage=fragment.usage,
                    tool_calls=tuple(fragment.tool_calls),
                )
            )
    return tuple(events)


def _session(
    candidate: Candidate,
    parser: str,
    events: tuple[NativeEvent, ...] | list[NativeEvent],
    declared_models: set[str] | None = None,
) -> NativeSession:
    ordered_events = tuple(events)
    models = {event.model for event in ordered_events if event.kind == "assistant" and event.model}
    if len(models) > 1:
        raise NativeEvidenceError(f"native source records multiple models: {sorted(models)!r}")
    if declared_models is not None and len(declared_models) > 1:
        raise NativeEvidenceError(
            f"native source records multiple declared models: {sorted(declared_models)!r}"
        )
    if declared_models and models and not _matching_declared_model(declared_models, models):
        raise NativeEvidenceError("assistant model does not match model_change provenance")
    model = next(iter(declared_models), None) if declared_models else next(iter(models), None)
    return NativeSession(
        candidate_id=candidate.candidate_id,
        provider=candidate.provider,
        parser=parser,
        source_path=candidate.source_path,
        source_sha256=source_sha256(candidate.source_path),
        model=model,
        events=ordered_events,
    )


def _matching_declared_model(declared_models: set[str], assistant_models: set[str]) -> bool:
    """Return whether a sole short assistant model matches declared provenance."""
    if declared_models == assistant_models:
        return True
    if len(declared_models) != 1 or len(assistant_models) != 1:
        return False
    declared_model = next(iter(declared_models))
    assistant_model = next(iter(assistant_models))
    return "/" in declared_model and declared_model.rsplit("/", maxsplit=1)[1] == assistant_model


def _records(path: Path) -> Iterator[tuple[int, dict[str, JsonValue]]]:
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError as error:
                    raise _error(line_number, f"invalid JSON: {error.msg}") from error
                if not isinstance(parsed, dict):
                    raise _error(line_number, "record must be an object")
                yield line_number, parsed
    except OSError as error:
        raise NativeEvidenceError("cannot read native source") from error


def _claude_usage(value: JsonValue, line_number: int) -> BillableUsage | None:
    if value is None:
        return None
    usage = _mapping(value)
    return BillableUsage(
        _counter(usage, "input_tokens", line_number),
        _counter(usage, "output_tokens", line_number),
        _counter(usage, "cache_read_input_tokens", line_number),
        _counter(usage, "cache_creation_input_tokens", line_number),
    )


def _omp_usage(value: JsonValue, line_number: int) -> BillableUsage | None:
    if value is None:
        return None
    usage = _mapping(value)
    return BillableUsage(
        _counter(usage, "input", line_number),
        _counter(usage, "output", line_number),
        _counter(usage, "cacheRead", line_number),
        _counter(usage, "cacheWrite", line_number),
    )


def _counter(usage: dict[str, JsonValue], field_name: str, line_number: int) -> int | None:
    value = usage.get(field_name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _error(line_number, f"usage.{field_name} must be a non-negative integer")
    return value


def _omp_tool_calls(content: JsonValue, line_number: int) -> list[ToolCall]:
    if isinstance(content, str):
        return []
    calls: list[ToolCall] = []
    for block in _list(content, line_number, "assistant content"):
        item = _mapping(block)
        if item.get("type") != "toolCall":
            continue
        calls.append(
            ToolCall(
                _required_text(item.get("id"), line_number, "tool call id"),
                _required_text(item.get("name"), line_number, "tool call name"),
                _mapping(item.get("arguments")),
            )
        )
    return calls


def _text_content(content: JsonValue) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    text_parts: list[str] = []
    for block in content:
        item = _mapping(block)
        text = item.get("text")
        if item.get("type") == "text" and isinstance(text, str):
            text_parts.append(text)
    return "\n".join(text_parts) if text_parts else None


def _timestamp(record: dict[str, JsonValue], line_number: int) -> str:
    return _required_text(record.get("timestamp"), line_number, "timestamp")


def _required_text(value: JsonValue, line_number: int, field_name: str) -> str:
    text = _text(value)
    if text is None:
        raise _error(line_number, f"{field_name} must be a non-empty string")
    return text


def _optional_text(value: JsonValue, line_number: int, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, line_number, field_name)


def _text(value: JsonValue) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _mapping(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _list(value: JsonValue, line_number: int, field_name: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise _error(line_number, f"{field_name} must be an array")
    return value


def _json_value(value: JsonValue, line_number: int) -> JsonValue:
    if value is None:
        raise _error(line_number, "tool result content is missing")
    return value


def _error(line_number: int, message: str) -> NativeEvidenceError:
    return NativeEvidenceError(f"native record line {line_number}: {message}")
