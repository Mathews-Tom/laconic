"""Non-network contract tests for the direct OpenAI Responses paired replay adapter."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tools.paired_replay.evidence import JsonValue, NativeEvent, NativeSession, ToolCall, ToolResult
from tools.paired_replay.interaction import InteractionRenderer, derive_interaction_receipt
from tools.paired_replay.openai_responses import OpenAIResponsesClient, _OpenAIHTTPError
from tools.paired_replay.runner import PairedReplayError, PairedReplayRequest


class RecordingResponsesClient(OpenAIResponsesClient):
    """Capture local request payloads and return fixed non-network Responses documents."""

    def __init__(self, responses: list[dict[str, JsonValue]]) -> None:
        self.payloads: list[dict[str, object]] = []
        self._responses = iter(responses)

    def _post(
        self,
        replay: PairedReplayRequest,
        credential: str,
        payload: dict[str, object],
    ) -> dict[str, JsonValue]:
        del replay, credential
        self.payloads.append(payload)
        return next(self._responses)


def _session(tool_input: dict[str, JsonValue] | None = None) -> NativeSession:
    return NativeSession(
        candidate_id="redesign-1",
        provider="omp",
        parser="omp-jsonl-v1",
        source_path=Path("/private/redesign-1.jsonl"),
        source_sha256="a" * 64,
        model="openai-codex/gpt-5.6-terra",
        events=(
            NativeEvent(0, "2026-08-01T00:00:00Z", "user_prompt", text="inspect source"),
            NativeEvent(
                1,
                "2026-08-01T00:01:00Z",
                "assistant",
                text="",
                tool_calls=(
                    ToolCall(
                        "call-1",
                        "Read",
                        tool_input if tool_input is not None else {"path": "src/main.py"},
                    ),
                ),
            ),
            NativeEvent(
                2,
                "2026-08-01T00:02:00Z",
                "tool_result",
                tool_result=ToolResult("call-1", {"text": "source"}),
            ),
            NativeEvent(3, "2026-08-01T00:03:00Z", "user_prompt", text="summarize"),
        ),
    )


def _request(tmp_path: Path, tool_input: dict[str, JsonValue] | None = None) -> PairedReplayRequest:
    session = _session(tool_input)
    receipt = derive_interaction_receipt(
        session,
        epoch_digest="b" * 64,
        manifest_digest="c" * 64,
        eligibility_ledger_digest="d" * 64,
        environment_ledger_digest="e" * 64,
        audit_head_digest="f" * 64,
        environment_digest="1" * 64,
        environment_mode="recorded_tool",
    )
    config = SimpleNamespace(
        artifact_root=tmp_path,
        credential_environment="OPENAI_API_KEY",
        decoding_parameters={
            "max_output_tokens": 4096,
            "parallel_tool_calls": True,
            "prompt_cache_retention": "in_memory",
            "reasoning_effort": "none",
            "store": False,
            "stream": False,
            "temperature": 0.0,
        },
        model="gpt-5.4-mini-2026-03-17",
    )
    return cast(
        PairedReplayRequest,
        SimpleNamespace(
            arm="raw",
            config=config,
            interaction=InteractionRenderer(receipt, session),
            run_id="replay-1",
        ),
    )


def _response(output: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue],
        {
            "model": "gpt-5.4-mini-2026-03-17",
            "output": output,
            "usage": {
                "input_tokens": 100,
                "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
                "output_tokens": 10,
            },
        },
    )


def test_responses_client_projects_current_tool_and_preserves_private_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    client = RecordingResponsesClient(
        [
            _response(
                [
                    {
                        "arguments": json.dumps({"path": "src/main.py"}),
                        "call_id": "call-1",
                        "id": "fc-1",
                        "name": "Read",
                        "type": "function_call",
                    }
                ]
            ),
            _response([{"content": [], "id": "msg-1", "type": "message"}]),
        ]
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-credential")

    replay = client.respond(request)

    assert [turn.classification for turn in replay.turns] == ["completed", "completed"]
    assert client.payloads[0] == {
        "input": [{"content": [{"text": "inspect source", "type": "input_text"}], "role": "user"}],
        "max_output_tokens": 4096,
        "model": "gpt-5.4-mini-2026-03-17",
        "parallel_tool_calls": True,
        "prompt_cache_retention": "in_memory",
        "reasoning": {"effort": "none"},
        "store": False,
        "stream": False,
        "temperature": 0.0,
        "tool_choice": {
            "mode": "auto",
            "tools": [{"name": "Read", "type": "function"}],
            "type": "allowed_tools",
        },
        "tools": [
            {
                "description": "Replay-authorized native tool",
                "name": "Read",
                "parameters": {
                    "additionalProperties": False,
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "type": "object",
                },
                "type": "function",
            }
        ],
    }
    assert client.payloads[1] == {
        "input": [
            {"content": [{"text": "inspect source", "type": "input_text"}], "role": "user"},
            {
                "arguments": json.dumps({"path": "src/main.py"}),
                "call_id": "call-1",
                "id": "fc-1",
                "name": "Read",
                "type": "function_call",
            },
            {"call_id": "call-1", "output": '{"text":"source"}', "type": "function_call_output"},
            {"content": [{"text": "summarize", "type": "input_text"}], "role": "user"},
        ],
        "max_output_tokens": 4096,
        "model": "gpt-5.4-mini-2026-03-17",
        "parallel_tool_calls": True,
        "prompt_cache_retention": "in_memory",
        "reasoning": {"effort": "none"},
        "store": False,
        "stream": False,
        "temperature": 0.0,
    }


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        ([], "provider emitted no tool action for current receipt position"),
        (
            [
                {
                    "arguments": "{}",
                    "call_id": "call-1",
                    "id": "fc-1",
                    "name": "mcp__tessera_stats",
                    "type": "function_call",
                }
            ],
            "tool name differs from interaction receipt",
        ),
        (
            [
                {
                    "arguments": "{}",
                    "call_id": "call-1",
                    "id": "fc-1",
                    "name": "Read",
                    "type": "function_call",
                },
                {
                    "arguments": "{}",
                    "call_id": "call-2",
                    "id": "fc-2",
                    "name": "Read",
                    "type": "function_call",
                },
            ],
            "provider emitted multiple tool actions in one chronological turn",
        ),
        (
            [
                {
                    "arguments": "{}",
                    "call_id": "call-1",
                    "id": "fc-1",
                    "name": "Read",
                    "type": "function_call",
                }
            ],
            "tool input does not match receipt schema",
        ),
        (
            [
                {
                    "arguments": json.dumps({"path": "src/other.py"}),
                    "call_id": "call-1",
                    "id": "fc-1",
                    "name": "Read",
                    "type": "function_call",
                }
            ],
            "tool call differs from exact recorded action",
        ),
    ],
)
def test_responses_client_terminates_unsupported_provider_turns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: list[dict[str, JsonValue]],
    reason: str,
) -> None:
    request = _request(tmp_path)
    client = RecordingResponsesClient([_response(output)])
    monkeypatch.setenv("OPENAI_API_KEY", "test-credential")

    replay = client.respond(request)

    assert len(replay.turns) == 1
    assert replay.turns[0].classification == "unsupported"
    assert replay.turns[0].unsupported_reason == reason


def test_responses_client_projects_tuple_schema_as_draft_2020_12(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path, {"paths": ["a.py", "b.py"]})
    client = RecordingResponsesClient([_response([])])
    monkeypatch.setenv("OPENAI_API_KEY", "test-credential")

    client.respond(request)

    assert client.payloads[0]["tools"] == [
        {
            "description": "Replay-authorized native tool",
            "name": "Read",
            "parameters": {
                "additionalProperties": False,
                "properties": {
                    "paths": {
                        "items": False,
                        "maxItems": 2,
                        "minItems": 2,
                        "prefixItems": [{"type": "string"}, {"type": "string"}],
                        "type": "array",
                    }
                },
                "required": ["paths"],
                "type": "object",
            },
            "type": "function",
        }
    ]


def test_responses_client_retains_http_error_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    error_body = b'{"error":{"message":"unsupported schema"}}'

    class HTTPErrorClient(OpenAIResponsesClient):
        def _post(
            self,
            replay: PairedReplayRequest,
            credential: str,
            payload: dict[str, object],
        ) -> dict[str, JsonValue]:
            del replay, credential, payload
            raise _OpenAIHTTPError(400, error_body)

    monkeypatch.setenv("OPENAI_API_KEY", "test-credential")
    replay = HTTPErrorClient().respond(request)

    assert replay.turns[0].classification == "unsupported"
    assert replay.turns[0].response == {
        "body_base64": base64.b64encode(error_body).decode("ascii"),
        "http_status": 400,
    }
    assert replay.turns[0].unsupported_reason == "OpenAI Responses request failed: HTTP 400"


def test_responses_client_retains_billing_before_terminal_request_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    first_response = _response(
        [
            {
                "arguments": json.dumps({"path": "src/main.py"}),
                "call_id": "call-1",
                "id": "fc-1",
                "name": "Read",
                "type": "function_call",
            }
        ]
    )

    class PostBillingErrorClient(OpenAIResponsesClient):
        def __init__(self) -> None:
            self.calls = 0

        def _post(
            self,
            replay: PairedReplayRequest,
            credential: str,
            payload: dict[str, object],
        ) -> dict[str, JsonValue]:
            del replay, credential, payload
            self.calls += 1
            if self.calls == 1:
                return first_response
            raise PairedReplayError("network failure")

    monkeypatch.setenv("OPENAI_API_KEY", "test-credential")
    replay = PostBillingErrorClient().respond(request)

    assert [turn.classification for turn in replay.turns] == ["completed", "unsupported"]
    assert replay.turns[0].native_usage == {
        "usage.input_tokens": 100,
        "usage.input_tokens_details.cache_write_tokens": 0,
        "usage.input_tokens_details.cached_tokens": 0,
        "usage.output_tokens": 10,
    }
    assert replay.turns[1].unsupported_reason == (
        "OpenAI request failed after billed response: network failure"
    )


def test_responses_client_rejects_missing_process_credential(tmp_path: Path) -> None:
    with pytest.raises(
        PairedReplayError, match="required credential environment 'OPENAI_API_KEY' is unset"
    ):
        RecordingResponsesClient([]).respond(_request(tmp_path))
