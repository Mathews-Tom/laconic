"""Bounded JSONL stdio service for the runtime session engine."""

from __future__ import annotations

import sys
from typing import BinaryIO, TextIO

from laconic.runtime.engine import RuntimeSession
from laconic.runtime.protocol import (
    ErrorResponse,
    ProtocolError,
    ProtocolErrorCode,
    RuntimeResponse,
    ShutdownResponse,
    parse_request_line,
    serialize_response,
)

MAX_FRAME_BYTES = 16 * 1024 * 1024


def _read_frame(stdin: BinaryIO, max_frame_bytes: int) -> tuple[bytes, bool] | None:
    chunk = stdin.readline(max_frame_bytes + 2)
    if chunk == b"":
        return None
    if chunk.endswith(b"\n"):
        payload = chunk[:-1]
        if payload.endswith(b"\r"):
            payload = payload[:-1]
        return payload, len(payload) > max_frame_bytes
    if len(chunk) <= max_frame_bytes:
        return chunk, False

    while chunk and not chunk.endswith(b"\n"):
        chunk = stdin.readline(max_frame_bytes + 2)
    return b"", True


def _write_response(stdout: TextIO, response: RuntimeResponse) -> None:
    stdout.write(serialize_response(response))
    stdout.flush()


def _safe_diagnostic(stderr: TextIO, message: str) -> None:
    stderr.write(f"laconic runtime: {message}\n")
    stderr.flush()


def serve_stdio(
    stdin: BinaryIO,
    stdout: TextIO,
    stderr: TextIO,
    *,
    max_frame_bytes: int = MAX_FRAME_BYTES,
) -> int:
    """Serve request frames until EOF or a successful shutdown."""
    if max_frame_bytes < 1:
        raise ValueError("max_frame_bytes must be positive")
    runtime = RuntimeSession()
    exit_code = 0
    stop = False
    try:
        while not stop:
            try:
                frame = _read_frame(stdin, max_frame_bytes)
            except OSError:
                _safe_diagnostic(stderr, "stdin read failed")
                exit_code = 1
                break
            if frame is None:
                break
            payload, oversized = frame
            if oversized:
                response: RuntimeResponse = ErrorResponse(
                    request_id=None,
                    operation=None,
                    code=ProtocolErrorCode.FRAME_TOO_LARGE,
                    message="protocol frame exceeds the byte limit",
                )
            else:
                try:
                    request = parse_request_line(payload.decode("utf-8"))
                    response = runtime.handle(request)
                except UnicodeDecodeError:
                    response = ErrorResponse(
                        request_id=None,
                        operation=None,
                        code=ProtocolErrorCode.INVALID_JSON,
                        message="frame is not valid UTF-8 JSON",
                    )
                except ProtocolError as error:
                    response = ErrorResponse.from_error(error)
                except Exception:  # noqa: BLE001 - final process boundary
                    response = ErrorResponse(
                        request_id=None,
                        operation=None,
                        code=ProtocolErrorCode.OPERATION_FAILED,
                        message="runtime request failed",
                    )
            try:
                _write_response(stdout, response)
            except (OSError, UnicodeError):
                _safe_diagnostic(stderr, "stdout write failed")
                exit_code = 1
                break
            stop = isinstance(response, ShutdownResponse)
    finally:
        try:
            runtime.close()
        except Exception:  # noqa: BLE001 - shutdown must not leak a traceback
            _safe_diagnostic(stderr, "runtime shutdown failed")
            exit_code = 1
    return exit_code


def main() -> int:
    """Run the stdio service on the process standard streams."""
    return serve_stdio(sys.stdin.buffer, sys.stdout, sys.stderr)
