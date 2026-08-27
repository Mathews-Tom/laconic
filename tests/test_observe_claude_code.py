"""Claude Code Observe adapter: synthetic event normalization.

Every payload below is a fixture; none is read from a real Claude Code
session or hook invocation.
"""

from __future__ import annotations

import pytest

from laconic.observe.claude_code import (
    CLAUDE_CODE_CONTRACT,
    MalformedPayloadError,
    UnsupportedEventError,
    parse_event,
)
from laconic.observe.contracts import ObserveEventKind

_POST_TOOL_USE_SUCCESS = {
    "session_id": "abc123",
    "transcript_path": "/home/user/.claude/projects/x/transcript.jsonl",
    "cwd": "/home/user/project",
    "permission_mode": "default",
    "hook_event_name": "PostToolUse",
    "tool_name": "Write",
    "tool_input": {"file_path": "/path/to/file.txt", "content": "file content"},
    "tool_response": {"filePath": "/path/to/file.txt", "success": True},
    "tool_use_id": "toolu_01ABC123",
    "duration_ms": 12,
}

_POST_TOOL_USE_FAILURE = {
    "session_id": "abc123",
    "transcript_path": "/home/user/.claude/projects/x/transcript.jsonl",
    "cwd": "/home/user/project",
    "permission_mode": "default",
    "hook_event_name": "PostToolUseFailure",
    "tool_name": "Bash",
    "tool_input": {"command": "npm test"},
    "tool_use_id": "toolu_01DEF456",
    "error": "Exit code 1\nError: Cannot find module 'express'",
    "is_interrupt": False,
    "duration_ms": 4187,
}

_SESSION_END = {
    "session_id": "abc123",
    "transcript_path": "/home/user/.claude/projects/x/transcript.jsonl",
    "cwd": "/home/user/project",
    "hook_event_name": "SessionEnd",
    "reason": "other",
}


def test_contract_covers_completed_result_and_session_close() -> None:
    assert CLAUDE_CODE_CONTRACT.go() is True


def test_post_tool_use_normalizes_to_success() -> None:
    observation = parse_event(_POST_TOOL_USE_SUCCESS)
    assert observation.event is ObserveEventKind.TOOL_RESULT_SUCCESS
    assert observation.tool_name == "Write"
    assert observation.session_id == "abc123"
    assert observation.duration_ms == 12


def test_post_tool_use_failure_normalizes_to_failure() -> None:
    observation = parse_event(_POST_TOOL_USE_FAILURE)
    assert observation.event is ObserveEventKind.TOOL_RESULT_FAILURE
    assert observation.tool_name == "Bash"
    assert observation.duration_ms == 4187


def test_session_end_normalizes_to_session_close_with_no_tool_name() -> None:
    observation = parse_event(_SESSION_END)
    assert observation.event is ObserveEventKind.SESSION_CLOSE
    assert observation.tool_name is None
    assert observation.duration_ms is None


def test_pre_tool_use_is_rejected_as_unsupported() -> None:
    """`PreToolUse` cannot observe a completed result; Observe never
    substitutes it for a post-result event."""
    payload = {**_POST_TOOL_USE_SUCCESS, "hook_event_name": "PreToolUse"}
    with pytest.raises(UnsupportedEventError):
        parse_event(payload)


def test_unknown_event_name_is_rejected() -> None:
    payload = {**_POST_TOOL_USE_SUCCESS, "hook_event_name": "SomeFutureEvent"}
    with pytest.raises(UnsupportedEventError):
        parse_event(payload)


def test_missing_session_id_is_malformed() -> None:
    payload = {k: v for k, v in _POST_TOOL_USE_SUCCESS.items() if k != "session_id"}
    with pytest.raises(MalformedPayloadError):
        parse_event(payload)


def test_missing_tool_name_is_malformed() -> None:
    payload = {k: v for k, v in _POST_TOOL_USE_SUCCESS.items() if k != "tool_name"}
    with pytest.raises(MalformedPayloadError):
        parse_event(payload)


def test_non_integer_duration_is_malformed() -> None:
    payload = {**_POST_TOOL_USE_SUCCESS, "duration_ms": "12"}
    with pytest.raises(MalformedPayloadError):
        parse_event(payload)


def test_session_end_never_reads_tool_fields() -> None:
    """A `SessionEnd` payload carries no `tool_name`; parsing it must not
    fail even if a future payload shape adds unrelated fields."""
    payload = {**_SESSION_END, "unexpected_future_field": "ignored"}
    observation = parse_event(payload)
    assert observation.tool_name is None
