"""Versioned JSONL runtime request and response contracts."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from laconic.runtime.protocol import (
    PROTOCOL_VERSION,
    EncodeObservationRequest,
    EncodeObservationResponse,
    ErrorResponse,
    ExpandRequest,
    ExpandResponse,
    InitializeRequest,
    InitializeResponse,
    Operation,
    ProtocolError,
    ProtocolErrorCode,
    RuntimePolicy,
    RuntimeResponse,
    ShutdownRequest,
    ShutdownResponse,
    parse_request_line,
    parse_response_line,
    serialize_response,
)

POLICY = {"span_budget": 120, "keep_head": 20, "keep_tail": 20, "max_errors": 20}


def _request(operation: str, **fields: object) -> str:
    return json.dumps(
        {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": f"req-{operation}",
            "operation": operation,
            **fields,
        }
    )


def test_initialize_request_binds_session_paths_and_policy() -> None:
    parsed = parse_request_line(
        _request(
            "initialize",
            session_id="session-1",
            working_directory="/repo",
            data_directory="/private/laconic",
            policy=POLICY,
        )
    )

    assert parsed == InitializeRequest(
        request_id="req-initialize",
        session_id="session-1",
        working_directory="/repo",
        data_directory="/private/laconic",
        policy=RuntimePolicy(span_budget=120, keep_head=20, keep_tail=20, max_errors=20),
    )


def test_encode_request_preserves_normalized_observation_exactly() -> None:
    parsed = parse_request_line(
        _request(
            "encode_observation",
            tool_name="Read",
            tool_input={"file_path": "src/a.py", "offset": 3},
            raw_text="line one\nline two",
            success=True,
            sequence=7,
        )
    )

    assert parsed == EncodeObservationRequest(
        request_id="req-encode_observation",
        tool_name="Read",
        tool_input={"file_path": "src/a.py", "offset": 3},
        raw_text="line one\nline two",
        success=True,
        sequence=7,
    )


def test_expand_and_shutdown_requests_are_typed() -> None:
    assert parse_request_line(_request("expand", reference="session-1/F3:2-4")) == ExpandRequest(
        request_id="req-expand", reference="session-1/F3:2-4"
    )
    assert parse_request_line(_request("shutdown")) == ShutdownRequest(request_id="req-shutdown")


def test_malformed_json_error_never_echoes_frame_content() -> None:
    secret = "secret raw observation"
    with pytest.raises(ProtocolError) as raised:
        parse_request_line('{"raw_text":"' + secret)

    assert raised.value.code is ProtocolErrorCode.INVALID_JSON
    assert secret not in str(raised.value)


@pytest.mark.parametrize(
    "line",
    [
        '{"protocol_version":1,"request_id":"a","request_id":"b","operation":"shutdown"}',
        '{"protocol_version":1,"request_id":"a","operation":"shutdown","value":NaN}',
    ],
)
def test_nonstandard_or_ambiguous_json_is_rejected(line: str) -> None:
    with pytest.raises(ProtocolError) as raised:
        parse_request_line(line)

    assert raised.value.code is ProtocolErrorCode.INVALID_JSON


def test_wrong_version_preserves_safe_request_correlation() -> None:
    payload = json.loads(_request("shutdown"))
    payload["protocol_version"] = 2

    with pytest.raises(ProtocolError) as raised:
        parse_request_line(json.dumps(payload))

    assert raised.value.code is ProtocolErrorCode.UNSUPPORTED_VERSION
    assert raised.value.request_id == "req-shutdown"
    assert raised.value.operation == "shutdown"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"unexpected": True}),
        lambda payload: payload.pop("sequence"),
        lambda payload: payload.update({"sequence": True}),
        lambda payload: payload.update({"success": 1}),
    ],
)
def test_encode_request_rejects_extra_missing_and_coerced_fields(
    mutate: Callable[[dict[str, object]], object],
) -> None:
    payload: dict[str, object] = json.loads(
        _request(
            "encode_observation",
            tool_name="Read",
            tool_input={},
            raw_text="body",
            success=True,
            sequence=1,
        )
    )
    mutate(payload)

    with pytest.raises(ProtocolError) as raised:
        parse_request_line(json.dumps(payload))

    assert raised.value.code is ProtocolErrorCode.INVALID_FRAME
    assert raised.value.request_id == "req-encode_observation"


@pytest.mark.parametrize(
    "response",
    [
        InitializeResponse(request_id="r1", session_id="session-1"),
        EncodeObservationResponse(
            request_id="r2",
            decision="emitted",
            reason="smaller_envelope",
            content="compact",
            reference="session-1/F1",
            raw_chars=100,
            visible_chars=7,
            latency_ms=2.5,
        ),
        EncodeObservationResponse(
            request_id="r3",
            decision="pass_through",
            reason="unsupported_tool",
            content=None,
            reference=None,
            raw_chars=20,
            visible_chars=20,
            latency_ms=0.25,
        ),
        ExpandResponse(
            request_id="r4",
            reference="session-1/F1",
            content="exact raw",
            metric_recorded=True,
        ),
        ShutdownResponse(request_id="r5", session_id="session-1"),
        ErrorResponse(
            request_id=None,
            operation=None,
            code=ProtocolErrorCode.INVALID_JSON,
            message="frame is not valid JSON",
        ),
    ],
)
def test_response_models_round_trip_through_jsonl(response: RuntimeResponse) -> None:
    line = serialize_response(response)

    assert line.endswith("\n")
    assert "\n" not in line[:-1]
    assert parse_response_line(line) == response


def test_emitted_response_requires_complete_strictly_smaller_envelope() -> None:
    with pytest.raises(ProtocolError, match="strictly smaller"):
        EncodeObservationResponse(
            request_id="r1",
            decision="emitted",
            reason="smaller_envelope",
            content="same",
            reference="session-1/F1",
            raw_chars=4,
            visible_chars=4,
            latency_ms=1.0,
        )


def test_pass_through_response_never_echoes_raw_content() -> None:
    response = EncodeObservationResponse(
        request_id="r1",
        decision="pass_through",
        reason="not_smaller",
        content=None,
        reference=None,
        raw_chars=20,
        visible_chars=20,
        latency_ms=1.0,
    )

    payload = json.loads(serialize_response(response))
    assert payload["result"]["content"] is None
    assert payload["result"]["reference"] is None
    assert payload["result"]["visible_chars"] == 20


def test_protocol_error_converts_to_a_typed_error_response() -> None:
    error = ProtocolError(
        ProtocolErrorCode.INVALID_STATE,
        "initialize first",
        request_id="r1",
        operation=Operation.ENCODE_OBSERVATION,
    )

    assert ErrorResponse.from_error(error) == ErrorResponse(
        request_id="r1",
        operation="encode_observation",
        code=ProtocolErrorCode.INVALID_STATE,
        message="initialize first",
    )
