"""End-to-end JSONL serving, bounds, and raw-free failure behavior."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from laconic.runtime.engine import RuntimeSession
from laconic.runtime.protocol import (
    EncodeObservationResponse,
    ErrorResponse,
    ExpandResponse,
    InitializeResponse,
    ProtocolErrorCode,
    RuntimeResponse,
    ShutdownResponse,
    parse_response_line,
)
from laconic.runtime.server import serve_stdio


def _frame(operation: str, request_id: str, **values: object) -> bytes:
    payload = {
        "protocol_version": 1,
        "request_id": request_id,
        "operation": operation,
        **values,
    }
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def _initialize_frame(tmp_path: Path, *, request_id: str = "init-1") -> bytes:
    return _frame(
        "initialize",
        request_id,
        session_id="session-1",
        working_directory=str(tmp_path),
        data_directory=str(tmp_path / "data"),
        policy={"span_budget": 20, "keep_head": 2, "keep_tail": 2, "max_errors": 2},
    )


def _compressible_read() -> str:
    return "\n".join(f"def function_{index}():\n    return {index}" for index in range(300))


def _serve(
    payload: bytes, *, max_frame_bytes: int = 16 * 1024 * 1024
) -> tuple[int, list[RuntimeResponse], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = serve_stdio(
        io.BytesIO(payload),
        stdout,
        stderr,
        max_frame_bytes=max_frame_bytes,
    )
    responses = [parse_response_line(line) for line in stdout.getvalue().splitlines()]
    return exit_code, responses, stderr.getvalue()


class _UnreadableInput(io.BytesIO):
    def readline(self, size: int = -1) -> bytes:
        raise OSError("secret stdin detail")


class _UnwritableOutput(io.StringIO):
    def write(self, value: str) -> int:
        raise OSError("secret stdout detail")


def test_broken_input_and_output_exit_nonzero_with_content_free_diagnostics() -> None:
    input_error = io.StringIO()
    input_exit = serve_stdio(_UnreadableInput(), io.StringIO(), input_error)
    output_error = io.StringIO()
    output_exit = serve_stdio(io.BytesIO(b"{not-json}\n"), _UnwritableOutput(), output_error)

    assert input_exit == 1
    assert input_error.getvalue() == "laconic runtime: stdin read failed\n"
    assert output_exit == 1
    assert output_error.getvalue() == "laconic runtime: stdout write failed\n"
    assert "secret" not in input_error.getvalue() + output_error.getvalue()


def test_shutdown_failure_exits_nonzero_without_leaking_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "secret shutdown detail"

    def fail_close(self: RuntimeSession) -> None:
        raise OSError(secret)

    monkeypatch.setattr(RuntimeSession, "close", fail_close)
    stderr = io.StringIO()

    exit_code = serve_stdio(io.BytesIO(), io.StringIO(), stderr)

    assert exit_code == 1
    assert stderr.getvalue() == "laconic runtime: runtime shutdown failed\n"
    assert secret not in stderr.getvalue()


def test_stdio_drives_initialize_encode_expand_pass_through_and_shutdown(tmp_path: Path) -> None:
    raw = _compressible_read()
    payload = b"".join(
        [
            _initialize_frame(tmp_path),
            _frame(
                "encode_observation",
                "encode-1",
                tool_name="Read",
                tool_input={"file_path": "sample.py"},
                raw_text=raw,
                success=True,
                sequence=1,
            ),
            _frame("expand", "expand-1", reference="session-1/F1"),
            _frame("expand", "expand-2", reference="session-1/F1:2-4"),
            _frame(
                "encode_observation",
                "encode-2",
                tool_name="Read",
                tool_input={"file_path": "tiny.py"},
                raw_text="x",
                success=True,
                sequence=2,
            ),
            _frame("shutdown", "shutdown-1"),
            b"this frame must not run after shutdown\n",
        ]
    )

    exit_code, responses, stderr = _serve(payload)

    assert exit_code == 0
    assert stderr == ""
    assert len(responses) == 6
    assert isinstance(responses[0], InitializeResponse)
    assert isinstance(responses[1], EncodeObservationResponse)
    assert responses[1].decision == "emitted"
    assert isinstance(responses[2], ExpandResponse)
    assert responses[2].metric_recorded is True
    assert responses[2].content == raw
    assert isinstance(responses[3], ExpandResponse)
    assert responses[3].metric_recorded is True
    assert responses[3].content == "\n".join(raw.split("\n")[1:4])
    assert isinstance(responses[4], EncodeObservationResponse)
    assert (responses[4].decision, responses[4].reason) == ("pass_through", "not_smaller")
    assert isinstance(responses[5], ShutdownResponse)


def test_malformed_and_oversized_frames_are_typed_and_do_not_desynchronize(tmp_path: Path) -> None:
    oversized = b'"' + (b"x" * 700) + b'"\n'
    payload = b"".join(
        [
            oversized,
            b"{not-json}\n",
            _initialize_frame(tmp_path),
            _frame("shutdown", "shutdown-1"),
        ]
    )

    exit_code, responses, stderr = _serve(payload, max_frame_bytes=512)

    assert exit_code == 0
    assert stderr == ""
    assert len(responses) == 4
    assert isinstance(responses[0], ErrorResponse)
    assert responses[0].code is ProtocolErrorCode.FRAME_TOO_LARGE
    assert isinstance(responses[1], ErrorResponse)
    assert responses[1].code is ProtocolErrorCode.INVALID_JSON
    assert isinstance(responses[2], InitializeResponse)
    assert isinstance(responses[3], ShutdownResponse)


def test_invalid_utf8_is_rejected_without_stopping_the_service(tmp_path: Path) -> None:
    payload = b"\xff\xfe\n" + _initialize_frame(tmp_path) + _frame("shutdown", "shutdown-1")

    exit_code, responses, stderr = _serve(payload)

    assert exit_code == 0
    assert stderr == ""
    assert len(responses) == 3
    assert isinstance(responses[0], ErrorResponse)
    assert responses[0].code is ProtocolErrorCode.INVALID_JSON
    assert isinstance(responses[1], InitializeResponse)
    assert isinstance(responses[2], ShutdownResponse)


def test_unexpected_request_failure_never_leaks_raw_input_or_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "raw-secret-and-exception-detail"

    def fail_request(self: RuntimeSession, request: object) -> object:
        raise RuntimeError(secret)

    monkeypatch.setattr(RuntimeSession, "handle", fail_request)
    payload = _frame(
        "encode_observation",
        "encode-1",
        tool_name="Read",
        tool_input={"file_path": "secret.py"},
        raw_text=secret,
        success=True,
        sequence=1,
    )

    exit_code, responses, stderr = _serve(payload)

    assert exit_code == 0
    assert len(responses) == 1
    assert isinstance(responses[0], ErrorResponse)
    assert responses[0].code is ProtocolErrorCode.OPERATION_FAILED
    assert secret not in json.dumps(responses[0].message)
    assert secret not in stderr


def test_python_module_entrypoint_exits_cleanly_after_shutdown(tmp_path: Path) -> None:
    payload = (_initialize_frame(tmp_path) + _frame("shutdown", "shutdown-1")).decode()

    completed = subprocess.run(
        [sys.executable, "-m", "laconic.runtime"],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        cwd=tmp_path,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    responses = [parse_response_line(line) for line in completed.stdout.splitlines()]
    assert len(responses) == 2
    assert isinstance(responses[0], InitializeResponse)
    assert isinstance(responses[1], ShutdownResponse)
