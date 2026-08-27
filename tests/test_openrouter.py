"""Non-network contract tests for the corrected OpenRouter paired replay adapter (R-18/H-72)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tools.paired_replay.config import ProviderRouting
from tools.paired_replay.evidence import JsonValue, NativeEvent, NativeSession, ToolCall, ToolResult
from tools.paired_replay.interaction import InteractionRenderer, derive_interaction_receipt
from tools.paired_replay.openrouter import OpenRouterChatCompletionsClient, _OpenRouterHTTPError
from tools.paired_replay.runner import PairedReplayError, PairedReplayRequest


class RecordingOpenRouterClient(OpenRouterChatCompletionsClient):
    """Capture local request payloads and return fixed non-network Chat Completions documents."""

    def __init__(self, responses: list[dict[str, JsonValue]]) -> None:
        self.payloads: list[dict[str, object]] = []
        self._responses = iter(responses)

    def _post(
        self,
        request: PairedReplayRequest,
        credential: str,
        payload: dict[str, object],
    ) -> dict[str, JsonValue]:
        del request, credential
        self.payloads.append(payload)
        return next(self._responses)


def _session(tool_input: dict[str, JsonValue] | None = None) -> NativeSession:
    return NativeSession(
        candidate_id="redesign-1",
        provider="omp",
        parser="omp-jsonl-v1",
        source_path=Path("/private/redesign-1.jsonl"),
        source_sha256="a" * 64,
        model="anthropic/claude-haiku-4.5",
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


def _multi_tool_session() -> NativeSession:
    """A session with two distinct tool actions, used to prove per-turn narrowing:
    a Read call followed later by a Write call with a different name and schema."""
    return NativeSession(
        candidate_id="redesign-1",
        provider="omp",
        parser="omp-jsonl-v1",
        source_path=Path("/private/redesign-1.jsonl"),
        source_sha256="a" * 64,
        model="anthropic/claude-haiku-4.5",
        events=(
            NativeEvent(0, "2026-08-01T00:00:00Z", "user_prompt", text="inspect source"),
            NativeEvent(
                1,
                "2026-08-01T00:01:00Z",
                "assistant",
                text="",
                tool_calls=(ToolCall("call-1", "Read", {"path": "src/main.py"}),),
            ),
            NativeEvent(
                2,
                "2026-08-01T00:02:00Z",
                "tool_result",
                tool_result=ToolResult("call-1", {"text": "source"}),
            ),
            NativeEvent(3, "2026-08-01T00:03:00Z", "user_prompt", text="now write a summary"),
            NativeEvent(
                4,
                "2026-08-01T00:04:00Z",
                "assistant",
                text="",
                tool_calls=(
                    ToolCall(
                        "call-2", "Write", {"path": "summary.txt", "content": "source summary"}
                    ),
                ),
            ),
            NativeEvent(
                5,
                "2026-08-01T00:05:00Z",
                "tool_result",
                tool_result=ToolResult("call-2", {"bytes_written": 14}),
            ),
            NativeEvent(6, "2026-08-01T00:06:00Z", "user_prompt", text="done"),
        ),
    )


def _request(
    tmp_path: Path,
    tool_input: dict[str, JsonValue] | None = None,
    session: NativeSession | None = None,
) -> PairedReplayRequest:
    session = _session(tool_input) if session is None else session
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
        credential_environment="OPENROUTER_API_KEY",
        decoding_parameters={"max_tokens": 4096, "temperature": 0.0},
        model="anthropic/claude-haiku-4.5",
        provider_routing=ProviderRouting(
            only=("anthropic",), allow_fallbacks=False, require_parameters=True
        ),
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


def _multi_tool_request(tmp_path: Path) -> PairedReplayRequest:
    return _request(tmp_path, session=_multi_tool_session())


def _response(choices: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue],
        {
            "model": "anthropic/claude-haiku-4.5",
            "choices": choices,
            "usage": {
                "prompt_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "completion_tokens": 10,
            },
        },
    )


def _choice(message: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {"message": message}


def test_openrouter_payload_uses_chat_completions_text_content_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chat Completions blocks must use `text`, not the Responses `input_text` type."""
    client = RecordingOpenRouterClient(
        [_response([_choice({"role": "assistant", "content": None, "tool_calls": None})])]
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-credential")

    client.respond(_request(tmp_path))

    assert client.payloads[0]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "inspect source"}]}
    ]


def test_openrouter_client_declares_only_current_tool_never_full_session_universe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root-cause fix (H-66): declare exactly one tool per turn, tool_choice auto, no
    allowed_tools -- never the full receipt/session tool universe the retired adapter sent.

    Uses a two-distinct-tool session (Read then Write) so a regression that reverts to
    declaring every receipt tool on every turn is caught even though it still stops
    declaring tools once the receipt is exhausted (turn 2)."""
    request = _multi_tool_request(tmp_path)
    client = RecordingOpenRouterClient(
        [
            _response(
                [
                    _choice(
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "Read",
                                        "arguments": json.dumps({"path": "src/main.py"}),
                                    },
                                }
                            ],
                        }
                    )
                ]
            ),
            _response(
                [
                    _choice(
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-2",
                                    "type": "function",
                                    "function": {
                                        "name": "Write",
                                        "arguments": json.dumps(
                                            {
                                                "path": "summary.txt",
                                                "content": "source summary",
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    )
                ]
            ),
            _response([_choice({"role": "assistant", "content": "done", "tool_calls": None})]),
        ]
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-credential")

    replay = client.respond(request)

    assert [turn.classification for turn in replay.turns] == [
        "completed",
        "completed",
        "completed",
    ]
    # Turn 0: only the current action (Read) is declared -- not [Read, Write].
    assert client.payloads[0]["tools"] == [
        {
            "type": "function",
            "function": {
                "description": "Replay-authorized native tool",
                "name": "Read",
                "parameters": {
                    "additionalProperties": False,
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "type": "object",
                },
            },
        }
    ]
    assert client.payloads[0]["tool_choice"] == "auto"
    assert "allowed_tools" not in json.dumps(client.payloads[0])
    # Turn 1: only the NEW current action (Write) is declared -- Read must not reappear.
    # This is the assertion a full-session-universe regression (H-41's actual shape) fails:
    # a reverted adapter would declare [Read, Write] here instead of [Write] alone.
    assert client.payloads[1]["tools"] == [
        {
            "type": "function",
            "function": {
                "description": "Replay-authorized native tool",
                "name": "Write",
                "parameters": {
                    "additionalProperties": False,
                    "properties": {
                        "content": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["content", "path"],
                    "type": "object",
                },
            },
        }
    ]
    assert client.payloads[1]["tool_choice"] == "auto"
    tool_names_declared_at_turn_1 = {
        tool["function"]["name"] for tool in client.payloads[1]["tools"]
    }
    assert "Read" not in tool_names_declared_at_turn_1
    # Turn 2: the receipt is exhausted -- no tool is declared at all.
    assert "tools" not in client.payloads[2]
    assert "tool_choice" not in client.payloads[2]
    assert client.payloads[2]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "inspect source"}]},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": json.dumps({"path": "src/main.py"}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": '{"text":"source"}'},
        {"role": "user", "content": [{"type": "text", "text": "now write a summary"}]},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-2",
                    "type": "function",
                    "function": {
                        "name": "Write",
                        "arguments": json.dumps(
                            {"path": "summary.txt", "content": "source summary"}
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-2",
            "content": '{"bytes_written":14}',
        },
        {"role": "user", "content": [{"type": "text", "text": "done"}]},
    ]


@pytest.mark.parametrize(
    ("choices", "reason"),
    [
        (
            [_choice({"role": "assistant", "content": None, "tool_calls": None})],
            "provider emitted no tool action for current receipt position",
        ),
        (
            [
                _choice(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "mcp__tessera_stats", "arguments": "{}"},
                            }
                        ],
                    }
                )
            ],
            "tool name differs from interaction receipt",
        ),
        (
            [
                _choice(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "Read", "arguments": "{}"},
                            },
                            {
                                "id": "call-2",
                                "type": "function",
                                "function": {"name": "Read", "arguments": "{}"},
                            },
                        ],
                    }
                )
            ],
            "provider emitted multiple tool actions in one chronological turn",
        ),
        (
            [
                _choice(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "Read", "arguments": "{}"},
                            }
                        ],
                    }
                )
            ],
            "tool input does not match receipt schema",
        ),
        (
            [
                _choice(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "arguments": json.dumps({"path": "src/other.py"}),
                                },
                            }
                        ],
                    }
                )
            ],
            "tool call differs from exact recorded action",
        ),
    ],
)
def test_openrouter_client_terminates_unsupported_provider_turns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    choices: list[dict[str, JsonValue]],
    reason: str,
) -> None:
    request = _request(tmp_path)
    client = RecordingOpenRouterClient([_response(choices)])
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-credential")

    replay = client.respond(request)

    assert len(replay.turns) == 1
    assert replay.turns[0].classification == "unsupported"
    assert replay.turns[0].unsupported_reason == reason


def test_openrouter_client_retains_http_error_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    error_body = b'{"error":{"message":"unsupported tool_choice value"}}'

    class HTTPErrorClient(OpenRouterChatCompletionsClient):
        def _post(
            self,
            request: PairedReplayRequest,
            credential: str,
            payload: dict[str, object],
        ) -> dict[str, JsonValue]:
            del request, credential, payload
            raise _OpenRouterHTTPError(400, error_body)

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-credential")
    replay = HTTPErrorClient().respond(request)

    assert replay.turns[0].classification == "unsupported"
    assert replay.turns[0].response == {
        "body_base64": base64.b64encode(error_body).decode("ascii"),
        "http_status": 400,
    }
    assert replay.turns[0].unsupported_reason == "OpenRouter request failed: HTTP 400"


def test_openrouter_client_retains_billing_before_terminal_request_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    first_response = _response(
        [
            _choice(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "Read",
                                "arguments": json.dumps({"path": "src/main.py"}),
                            },
                        }
                    ],
                }
            )
        ]
    )

    class PostBillingErrorClient(OpenRouterChatCompletionsClient):
        def __init__(self) -> None:
            self.calls = 0

        def _post(
            self,
            request: PairedReplayRequest,
            credential: str,
            payload: dict[str, object],
        ) -> dict[str, JsonValue]:
            del request, credential, payload
            self.calls += 1
            if self.calls == 1:
                return first_response
            raise PairedReplayError("network failure")

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-credential")
    replay = PostBillingErrorClient().respond(request)

    assert [turn.classification for turn in replay.turns] == ["completed", "unsupported"]
    assert replay.turns[0].native_usage == {
        "usage.prompt_tokens": 100,
        "usage.prompt_tokens_details.cached_tokens": 0,
        "usage.prompt_tokens_details.cache_write_tokens": 0,
        "usage.completion_tokens": 10,
    }
    assert replay.turns[1].unsupported_reason == (
        "OpenRouter request failed after billed response: network failure"
    )


def test_openrouter_client_rejects_missing_process_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(
        PairedReplayError, match="required credential environment 'OPENROUTER_API_KEY' is unset"
    ):
        RecordingOpenRouterClient([]).respond(_request(tmp_path))


def test_openrouter_client_terminates_response_with_unpinned_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    response = _response([])
    response["model"] = "anthropic/claude-4.5-haiku"
    client = RecordingOpenRouterClient([response])
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-credential")

    replay = client.respond(request)

    assert len(replay.turns) == 1
    assert replay.turns[0].classification == "unsupported"
    assert replay.turns[0].unsupported_reason == (
        "OpenRouter response is invalid: OpenRouter response model does not match configuration"
    )


def test_openrouter_client_rejects_missing_provider_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    object.__setattr__(request.config, "provider_routing", None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-credential")

    with pytest.raises(
        PairedReplayError, match="OpenRouter replay requires configured provider routing"
    ):
        RecordingOpenRouterClient([]).respond(request)
