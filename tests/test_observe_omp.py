"""OMP Observe adapter: synthetic event normalization.

Every payload below is a fixture modeling the JSON-serializable envelope a
discovered extension shim would forward; none is produced by a real OMP
session or extension module.
"""

from __future__ import annotations

import pytest

from laconic.observe.contracts import ObserveEventKind
from laconic.observe.omp import (
    OMP_CONTRACT,
    MalformedPayloadError,
    UnsupportedEventError,
    parse_event,
)

_TOOL_RESULT_SUCCESS = {
    "session_id": "abc123",
    "event": "tool_result",
    "toolName": "read",
    "toolCallId": "call_01",
    "isError": False,
}

_TOOL_RESULT_FAILURE = {
    "session_id": "abc123",
    "event": "tool_result",
    "toolName": "bash",
    "toolCallId": "call_02",
    "isError": True,
}

_SESSION_SHUTDOWN = {
    "session_id": "abc123",
    "event": "session_shutdown",
}


def test_contract_covers_completed_result_and_session_close() -> None:
    assert OMP_CONTRACT.go() is True


def test_tool_result_success_normalizes_to_success() -> None:
    observation = parse_event(_TOOL_RESULT_SUCCESS)
    assert observation.event is ObserveEventKind.TOOL_RESULT_SUCCESS
    assert observation.tool_name == "read"
    assert observation.session_id == "abc123"


def test_tool_result_failure_normalizes_to_failure() -> None:
    observation = parse_event(_TOOL_RESULT_FAILURE)
    assert observation.event is ObserveEventKind.TOOL_RESULT_FAILURE
    assert observation.tool_name == "bash"


def test_session_shutdown_normalizes_to_session_close_with_no_tool_name() -> None:
    observation = parse_event(_SESSION_SHUTDOWN)
    assert observation.event is ObserveEventKind.SESSION_CLOSE
    assert observation.tool_name is None


def test_tool_call_is_rejected_as_unsupported() -> None:
    """`tool_call` fires before execution and cannot observe a completed
    result; Observe never substitutes it for `tool_result`."""
    payload = {**_TOOL_RESULT_SUCCESS, "event": "tool_call"}
    with pytest.raises(UnsupportedEventError):
        parse_event(payload)


def test_unknown_event_name_is_rejected() -> None:
    payload = {**_TOOL_RESULT_SUCCESS, "event": "some_future_event"}
    with pytest.raises(UnsupportedEventError):
        parse_event(payload)


def test_missing_session_id_is_malformed() -> None:
    payload = {k: v for k, v in _TOOL_RESULT_SUCCESS.items() if k != "session_id"}
    with pytest.raises(MalformedPayloadError):
        parse_event(payload)


def test_missing_tool_name_is_malformed() -> None:
    payload = {k: v for k, v in _TOOL_RESULT_SUCCESS.items() if k != "toolName"}
    with pytest.raises(MalformedPayloadError):
        parse_event(payload)


def test_missing_is_error_is_malformed() -> None:
    payload = {k: v for k, v in _TOOL_RESULT_SUCCESS.items() if k != "isError"}
    with pytest.raises(MalformedPayloadError):
        parse_event(payload)


def test_non_bool_is_error_is_malformed() -> None:
    payload = {**_TOOL_RESULT_SUCCESS, "isError": "false"}
    with pytest.raises(MalformedPayloadError):
        parse_event(payload)
