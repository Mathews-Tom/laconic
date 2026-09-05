"""Typed version-one JSONL contracts for the transport-neutral runtime engine."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

PROTOCOL_VERSION = 1


type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


class Operation(StrEnum):
    INITIALIZE = "initialize"
    ENCODE_OBSERVATION = "encode_observation"
    EXPAND = "expand"
    SHUTDOWN = "shutdown"


class ProtocolErrorCode(StrEnum):
    INVALID_JSON = "invalid_json"
    INVALID_FRAME = "invalid_frame"
    FRAME_TOO_LARGE = "frame_too_large"
    UNSUPPORTED_VERSION = "unsupported_version"
    INVALID_STATE = "invalid_state"
    OPERATION_FAILED = "operation_failed"
    INVALID_REFERENCE = "invalid_reference"
    UNKNOWN_SESSION = "unknown_session"
    UNKNOWN_HANDLE = "unknown_handle"
    INVALID_SPAN = "invalid_span"
    STORAGE_FAILURE = "storage_failure"
    ENCODING_FAILURE = "encoding_failure"
    METRIC_FAILURE = "metric_failure"


class ProtocolError(ValueError):
    """A content-free protocol failure safe to return to a host adapter."""

    def __init__(
        self,
        code: ProtocolErrorCode,
        message: str,
        *,
        request_id: str | None = None,
        operation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.operation = operation


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """The existing observation-codec controls bound at initialization."""

    span_budget: int
    keep_head: int
    keep_tail: int
    max_errors: int

    def __post_init__(self) -> None:
        if self.span_budget < 1:
            raise ProtocolError(ProtocolErrorCode.INVALID_FRAME, "span_budget must be positive")
        for name, value in (
            ("keep_head", self.keep_head),
            ("keep_tail", self.keep_tail),
            ("max_errors", self.max_errors),
        ):
            if value < 0:
                raise ProtocolError(
                    ProtocolErrorCode.INVALID_FRAME,
                    f"{name} must not be negative",
                )


@dataclass(frozen=True, slots=True)
class InitializeRequest:
    request_id: str
    session_id: str
    working_directory: str
    data_directory: str
    policy: RuntimePolicy
    operation: Operation = Operation.INITIALIZE
    protocol_version: int = PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class EncodeObservationRequest:
    request_id: str
    tool_name: str
    tool_input: dict[str, JsonValue]
    raw_text: str
    success: bool
    sequence: int
    operation: Operation = Operation.ENCODE_OBSERVATION
    protocol_version: int = PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class ExpandRequest:
    request_id: str
    reference: str
    operation: Operation = Operation.EXPAND
    protocol_version: int = PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class ShutdownRequest:
    request_id: str
    operation: Operation = Operation.SHUTDOWN
    protocol_version: int = PROTOCOL_VERSION


type RuntimeRequest = InitializeRequest | EncodeObservationRequest | ExpandRequest | ShutdownRequest


@dataclass(frozen=True, slots=True)
class InitializeResponse:
    request_id: str
    session_id: str
    next_sequence: int
    operation: Operation = Operation.INITIALIZE
    protocol_version: int = PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class EncodeObservationResponse:
    request_id: str
    decision: str
    reason: str
    content: str | None
    reference: str | None
    raw_chars: int
    visible_chars: int
    latency_ms: float
    operation: Operation = Operation.ENCODE_OBSERVATION
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.decision not in ("emitted", "pass_through"):
            raise ProtocolError(ProtocolErrorCode.INVALID_FRAME, "invalid encode decision")
        if not self.reason:
            raise ProtocolError(ProtocolErrorCode.INVALID_FRAME, "encode reason must not be empty")
        if self.raw_chars < 0 or self.visible_chars < 0:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_FRAME,
                "encode character counts must not be negative",
            )
        if not math.isfinite(self.latency_ms) or self.latency_ms < 0:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_FRAME,
                "encode latency must be finite and non-negative",
            )
        if self.decision == "emitted":
            if self.content is None or self.reference is None:
                raise ProtocolError(
                    ProtocolErrorCode.INVALID_FRAME,
                    "emitted response requires content and reference",
                )
            if self.visible_chars >= self.raw_chars or len(self.content) != self.visible_chars:
                raise ProtocolError(
                    ProtocolErrorCode.INVALID_FRAME,
                    "emitted response must contain a strictly smaller envelope",
                )
        elif (
            self.content is not None
            or self.reference is not None
            or self.visible_chars != self.raw_chars
        ):
            raise ProtocolError(
                ProtocolErrorCode.INVALID_FRAME,
                "pass-through response must omit content and reference",
            )


@dataclass(frozen=True, slots=True)
class ExpandResponse:
    request_id: str
    reference: str
    content: str
    metric_recorded: bool
    operation: Operation = Operation.EXPAND
    protocol_version: int = PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class ShutdownResponse:
    request_id: str
    session_id: str
    operation: Operation = Operation.SHUTDOWN
    protocol_version: int = PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class ErrorResponse:
    request_id: str | None
    operation: str | None
    code: ProtocolErrorCode
    message: str
    protocol_version: int = PROTOCOL_VERSION

    @classmethod
    def from_error(cls, error: ProtocolError) -> ErrorResponse:
        return cls(
            request_id=error.request_id,
            operation=error.operation,
            code=error.code,
            message=str(error),
        )


type RuntimeResponse = (
    InitializeResponse
    | EncodeObservationResponse
    | ExpandResponse
    | ShutdownResponse
    | ErrorResponse
)


class _Reader:
    def __init__(self, payload: dict[str, JsonValue]) -> None:
        self.payload = payload

    def exact_keys(self, *keys: str) -> None:
        expected = set(keys)
        actual = set(self.payload)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            detail = []
            if missing:
                detail.append(f"missing fields: {', '.join(missing)}")
            if extra:
                detail.append(f"unexpected fields: {', '.join(extra)}")
            raise ProtocolError(ProtocolErrorCode.INVALID_FRAME, "; ".join(detail))

    def string(self, key: str, *, allow_empty: bool = False) -> str:
        value = self.payload.get(key)
        if not isinstance(value, str) or (not allow_empty and not value):
            raise ProtocolError(ProtocolErrorCode.INVALID_FRAME, f"field {key!r} must be a string")
        return value

    def integer(self, key: str, *, minimum: int = 0) -> int:
        value = self.payload.get(key)
        if type(value) is not int or value < minimum:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_FRAME,
                f"field {key!r} must be an integer at least {minimum}",
            )
        return value

    def number(self, key: str, *, minimum: float = 0.0) -> float:
        value = self.payload.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < minimum
        ):
            raise ProtocolError(
                ProtocolErrorCode.INVALID_FRAME,
                f"field {key!r} must be a finite number at least {minimum}",
            )
        return float(value)

    def boolean(self, key: str) -> bool:
        value = self.payload.get(key)
        if type(value) is not bool:
            raise ProtocolError(ProtocolErrorCode.INVALID_FRAME, f"field {key!r} must be boolean")
        return value

    def object(self, key: str) -> dict[str, JsonValue]:
        value = self.payload.get(key)
        if not isinstance(value, dict) or any(not isinstance(item, str) for item in value):
            raise ProtocolError(ProtocolErrorCode.INVALID_FRAME, f"field {key!r} must be an object")
        return value

    def optional_string(self, key: str) -> str | None:
        value = self.payload.get(key)
        if value is not None and not isinstance(value, str):
            raise ProtocolError(
                ProtocolErrorCode.INVALID_FRAME,
                f"field {key!r} must be a string or null",
            )
        return value


def _reject_json_constant(_value: str) -> JsonValue:
    raise ValueError("non-finite JSON number")


def _strict_json_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _decode_line(line: str) -> dict[str, JsonValue]:
    try:
        payload: object = json.loads(
            line,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_strict_json_object,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        raise ProtocolError(ProtocolErrorCode.INVALID_JSON, "frame is not valid JSON") from error
    if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
        raise ProtocolError(ProtocolErrorCode.INVALID_FRAME, "frame must be a JSON object")
    return cast("dict[str, JsonValue]", payload)


def _request_context(reader: _Reader) -> tuple[str, Operation]:
    request_id = reader.string("request_id")
    version = reader.integer("protocol_version", minimum=1)
    operation_text = reader.string("operation")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            ProtocolErrorCode.UNSUPPORTED_VERSION,
            f"unsupported protocol version: {version}",
            request_id=request_id,
            operation=operation_text,
        )
    try:
        operation = Operation(operation_text)
    except ValueError as error:
        raise ProtocolError(
            ProtocolErrorCode.INVALID_FRAME,
            "unsupported operation",
            request_id=request_id,
            operation=operation_text,
        ) from error
    return request_id, operation


def _parse_policy(payload: dict[str, JsonValue]) -> RuntimePolicy:
    reader = _Reader(payload)
    reader.exact_keys("span_budget", "keep_head", "keep_tail", "max_errors")
    return RuntimePolicy(
        span_budget=reader.integer("span_budget", minimum=1),
        keep_head=reader.integer("keep_head"),
        keep_tail=reader.integer("keep_tail"),
        max_errors=reader.integer("max_errors"),
    )


def parse_request_line(line: str) -> RuntimeRequest:
    """Parse and strictly validate one request frame without echoing its content."""
    payload = _decode_line(line)
    reader = _Reader(payload)
    request_id, operation = _request_context(reader)
    try:
        if operation is Operation.INITIALIZE:
            reader.exact_keys(
                "protocol_version",
                "request_id",
                "operation",
                "session_id",
                "working_directory",
                "data_directory",
                "policy",
            )
            return InitializeRequest(
                request_id=request_id,
                session_id=reader.string("session_id"),
                working_directory=reader.string("working_directory"),
                data_directory=reader.string("data_directory"),
                policy=_parse_policy(reader.object("policy")),
            )
        if operation is Operation.ENCODE_OBSERVATION:
            reader.exact_keys(
                "protocol_version",
                "request_id",
                "operation",
                "tool_name",
                "tool_input",
                "raw_text",
                "success",
                "sequence",
            )
            return EncodeObservationRequest(
                request_id=request_id,
                tool_name=reader.string("tool_name"),
                tool_input=reader.object("tool_input"),
                raw_text=reader.string("raw_text", allow_empty=True),
                success=reader.boolean("success"),
                sequence=reader.integer("sequence"),
            )
        if operation is Operation.EXPAND:
            reader.exact_keys("protocol_version", "request_id", "operation", "reference")
            return ExpandRequest(request_id=request_id, reference=reader.string("reference"))
        reader.exact_keys("protocol_version", "request_id", "operation")
        return ShutdownRequest(request_id=request_id)
    except ProtocolError as error:
        raise ProtocolError(
            error.code,
            str(error),
            request_id=request_id,
            operation=operation.value,
        ) from error


def _response_payload(response: RuntimeResponse) -> dict[str, JsonValue]:
    common: dict[str, JsonValue] = {
        "protocol_version": response.protocol_version,
        "request_id": response.request_id,
        "operation": response.operation,
    }
    if isinstance(response, ErrorResponse):
        return {
            **common,
            "status": "error",
            "error": {"code": response.code.value, "message": response.message},
        }
    if isinstance(response, InitializeResponse):
        result: dict[str, JsonValue] = {
            "session_id": response.session_id,
            "next_sequence": response.next_sequence,
        }
    elif isinstance(response, EncodeObservationResponse):
        result = {
            "decision": response.decision,
            "reason": response.reason,
            "content": response.content,
            "reference": response.reference,
            "raw_chars": response.raw_chars,
            "visible_chars": response.visible_chars,
            "latency_ms": response.latency_ms,
        }
    elif isinstance(response, ExpandResponse):
        result = {
            "reference": response.reference,
            "content": response.content,
            "metric_recorded": response.metric_recorded,
        }
    else:
        result = {"session_id": response.session_id}
    return {**common, "status": "ok", "result": result}


def serialize_response(response: RuntimeResponse) -> str:
    """Serialize one response as a compact newline-terminated JSONL frame."""
    return json.dumps(_response_payload(response), ensure_ascii=True, separators=(",", ":")) + "\n"


def parse_response_line(line: str) -> RuntimeResponse:
    """Validate one response frame for non-Python host implementations and tests."""
    payload = _decode_line(line)
    reader = _Reader(payload)
    status = reader.string("status")
    if status == "error":
        reader.exact_keys("protocol_version", "request_id", "operation", "status", "error")
        version = reader.integer("protocol_version", minimum=1)
        if version != PROTOCOL_VERSION:
            raise ProtocolError(
                ProtocolErrorCode.UNSUPPORTED_VERSION, "unsupported protocol version"
            )
        request_id = reader.optional_string("request_id")
        operation = reader.optional_string("operation")
        error_reader = _Reader(reader.object("error"))
        error_reader.exact_keys("code", "message")
        try:
            code = ProtocolErrorCode(error_reader.string("code"))
        except ValueError as error:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_FRAME, "unsupported error code"
            ) from error
        return ErrorResponse(
            request_id=request_id,
            operation=operation,
            code=code,
            message=error_reader.string("message"),
        )

    if status != "ok":
        raise ProtocolError(ProtocolErrorCode.INVALID_FRAME, "unsupported response status")
    reader.exact_keys("protocol_version", "request_id", "operation", "status", "result")
    request_id, operation = _request_context(reader)
    result = _Reader(reader.object("result"))
    if operation is Operation.INITIALIZE:
        result.exact_keys("session_id", "next_sequence")
        return InitializeResponse(
            request_id=request_id,
            session_id=result.string("session_id"),
            next_sequence=result.integer("next_sequence", minimum=0),
        )
    if operation is Operation.ENCODE_OBSERVATION:
        result.exact_keys(
            "decision",
            "reason",
            "content",
            "reference",
            "raw_chars",
            "visible_chars",
            "latency_ms",
        )
        return EncodeObservationResponse(
            request_id=request_id,
            decision=result.string("decision"),
            reason=result.string("reason"),
            content=result.optional_string("content"),
            reference=result.optional_string("reference"),
            raw_chars=result.integer("raw_chars"),
            visible_chars=result.integer("visible_chars"),
            latency_ms=result.number("latency_ms"),
        )
    if operation is Operation.EXPAND:
        result.exact_keys("reference", "content", "metric_recorded")
        return ExpandResponse(
            request_id=request_id,
            reference=result.string("reference"),
            content=result.string("content", allow_empty=True),
            metric_recorded=result.boolean("metric_recorded"),
        )
    result.exact_keys("session_id")
    return ShutdownResponse(request_id=request_id, session_id=result.string("session_id"))
