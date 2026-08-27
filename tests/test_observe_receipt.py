"""Receipt construction: content-free by construction, size bands over
exact counts, tool-name-to-category collapsing.

Every payload below is a fixture; none is read from a real session.
"""

from __future__ import annotations

import pytest

from laconic.observe.claude_code import MalformedPayloadError, UnsupportedEventError
from laconic.observe.contracts import ClientId
from laconic.observe.receipt import (
    ResultClass,
    SizeBand,
    ToolCategory,
    build_claude_code_receipt,
    build_omp_receipt,
)

_CC_SUCCESS = {
    "session_id": "abc123",
    "hook_event_name": "PostToolUse",
    "tool_name": "Write",
    "tool_input": {"file_path": "/x/file.txt", "content": "hello"},
    "tool_response": {"filePath": "/x/file.txt", "success": True},
    "duration_ms": 12,
}

_CC_FAILURE = {
    "session_id": "abc123",
    "hook_event_name": "PostToolUseFailure",
    "tool_name": "Bash",
    "tool_input": {"command": "npm test"},
    "error": "Exit code 1\n" + ("x" * 40000),
}

_CC_SESSION_END = {"session_id": "abc123", "hook_event_name": "SessionEnd", "reason": "other"}

_OMP_SUCCESS = {
    "session_id": "abc123",
    "event": "tool_result",
    "toolName": "bash",
    "isError": False,
    "input": {"command": "ls"},
    "content": [{"type": "text", "text": "a.py\nb.py"}],
}

_OMP_MCP = {
    "session_id": "abc123",
    "event": "tool_result",
    "toolName": "mcp__internal-crm__search",
    "isError": False,
}


def test_claude_code_receipt_carries_only_allowlisted_fields() -> None:
    receipt = build_claude_code_receipt(_CC_SUCCESS, now=1000.0)
    assert receipt.adapter is ClientId.CLAUDE_CODE
    assert receipt.session_id == "abc123"
    assert receipt.tool_category is ToolCategory.FILE_WRITE
    assert receipt.result_class is ResultClass.SUCCESS
    assert receipt.timestamp == 1000.0


def test_claude_code_receipt_never_carries_raw_tool_name_in_json() -> None:
    receipt = build_claude_code_receipt(_CC_SUCCESS, now=1000.0)
    payload = receipt.to_json()
    assert "Write" not in str(payload.values())
    assert "tool_name" not in payload


def test_claude_code_failure_receipt_bands_a_large_error_string() -> None:
    receipt = build_claude_code_receipt(_CC_FAILURE, now=1000.0)
    assert receipt.result_class is ResultClass.FAILURE
    assert receipt.result_size is SizeBand.XL
    assert receipt.tool_category is ToolCategory.COMMAND


def test_claude_code_session_end_has_no_size_and_session_category() -> None:
    receipt = build_claude_code_receipt(_CC_SESSION_END, now=1000.0)
    assert receipt.result_class is ResultClass.SESSION_CLOSE
    assert receipt.tool_category is ToolCategory.SESSION
    assert receipt.argument_size is SizeBand.NONE
    assert receipt.result_size is SizeBand.NONE


def test_claude_code_receipt_rejects_unsupported_event() -> None:
    payload = {**_CC_SUCCESS, "hook_event_name": "PreToolUse"}
    with pytest.raises(UnsupportedEventError):
        build_claude_code_receipt(payload, now=1000.0)


def test_claude_code_receipt_rejects_malformed_payload() -> None:
    payload = {k: v for k, v in _CC_SUCCESS.items() if k != "tool_name"}
    with pytest.raises(MalformedPayloadError):
        build_claude_code_receipt(payload, now=1000.0)


def test_omp_receipt_carries_only_allowlisted_fields() -> None:
    receipt = build_omp_receipt(_OMP_SUCCESS, now=2000.0)
    assert receipt.adapter is ClientId.OMP
    assert receipt.tool_category is ToolCategory.COMMAND
    assert receipt.result_class is ResultClass.SUCCESS


def test_omp_receipt_never_carries_raw_tool_name_in_json() -> None:
    receipt = build_omp_receipt(_OMP_SUCCESS, now=2000.0)
    payload = receipt.to_json()
    assert "bash" not in str(payload.values())


def test_omp_mcp_tool_collapses_to_mcp_category_not_server_name() -> None:
    """A real MCP server name can itself leak which internal system a
    project integrates with -- it must never survive into a receipt."""
    receipt = build_omp_receipt(_OMP_MCP, now=2000.0)
    assert receipt.tool_category is ToolCategory.MCP
    payload = receipt.to_json()
    assert "internal-crm" not in str(payload.values())


def test_size_bands_are_monotonic_with_encoded_length() -> None:
    small = build_claude_code_receipt({**_CC_SUCCESS, "tool_response": {"success": True}}, now=0.0)
    large = build_claude_code_receipt(
        {**_CC_SUCCESS, "tool_response": {"blob": "x" * 100_000}}, now=0.0
    )
    order = list(SizeBand)
    assert order.index(small.result_size) < order.index(large.result_size)
