"""Concrete Anthropic Messages adapter for authorized K1 paired replay."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from laconic.k1.evidence import JsonValue
from laconic.k1.paired_config import PairedReplayConfig
from laconic.k1.paired_runner import (
    PairedReplayError,
    PairedReplayRequest,
    PairedReplayResponse,
    PairedResponseTurn,
)
from laconic.k1.pricing import normalize_usage

ANTHROPIC_API_KEY_ENVIRONMENT = "ANTHROPIC_API_KEY"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicReplayError(PairedReplayError):
    """Raised when the configured Anthropic execution contract cannot be honored."""


class AnthropicReplayClient:
    """Run one K1 arm through the first-party Anthropic Messages API.

    Request and response bodies remain inside the caller's private artifact root. This
    adapter never falls back to another provider, model, endpoint, or credential source.
    """

    def __init__(self, config: PairedReplayConfig) -> None:
        if config.provider != "anthropic":
            raise AnthropicReplayError("Anthropic replay client requires provider 'anthropic'")
        if config.seed_supported or config.seed is not None:
            raise AnthropicReplayError("Anthropic Messages replay does not support a seed")
        _validate_decoding_parameters(config.decoding_parameters)
        _require_credential()
        self._config = config

    def respond(self, request: PairedReplayRequest) -> PairedReplayResponse:
        if request.config != self._config:
            raise AnthropicReplayError("request configuration does not match Anthropic client")
        if len(request.condition.user_prompts) != 1:
            raise AnthropicReplayError("Anthropic replay requires exactly one native user prompt")
        credential = _require_credential()
        messages: list[dict[str, object]] = [
            {"role": "user", "content": request.condition.user_prompts[0]}
        ]
        tools = _tool_definitions(request)
        resolver = request.condition.resolver()
        turns: list[PairedResponseTurn] = []
        while True:
            response = self._post_messages(messages, tools, credential)
            usage = _usage(response, request.config)
            turns.append(PairedResponseTurn(response, usage, "completed"))
            content = _content(response)
            tool_results: list[dict[str, str]] = []
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                tool_id = _required_text(block, "id", "tool-use block")
                name = _required_text(block, "name", "tool-use block")
                tool_input = _json_object(block.get("input"), "tool-use input")
                resolution = resolver.resolve(name, tool_input)
                if resolution.status == "unsupported":
                    turns[-1] = PairedResponseTurn(
                        response,
                        usage,
                        "unsupported",
                        resolution.reason,
                    )
                    return PairedReplayResponse(
                        tuple(turns), request.condition.digest, resolver.position
                    )
                if not isinstance(resolution.output, str):
                    raise AnthropicReplayError("condition resolver returned non-text tool output")
                tool_results.append({"tool_use_id": tool_id, "content": resolution.output})
            stop_reason = _required_text(response, "stop_reason", "provider response")
            if not tool_results:
                if stop_reason != "end_turn":
                    raise AnthropicReplayError(
                        f"provider ended replay with unsupported stop_reason {stop_reason!r}"
                    )
                if resolver.position != len(request.condition.observations):
                    raise AnthropicReplayError(
                        "provider completed before resolving every condition tool call"
                    )
                return PairedReplayResponse(
                    tuple(turns), request.condition.digest, resolver.position
                )
            if stop_reason != "tool_use":
                raise AnthropicReplayError(
                    "provider returned tool uses without the tool_use stop_reason"
                )
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", **tool_result} for tool_result in tool_results
                    ],
                }
            )

    def _post_messages(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        credential: str,
    ) -> dict[str, JsonValue]:
        decoding = self._config.decoding_parameters
        body: dict[str, object] = {
            "max_tokens": decoding["max_tokens"],
            "messages": messages,
            "model": self._config.model,
            "temperature": decoding["temperature"],
            "tools": tools,
            "top_p": decoding["top_p"],
        }
        encoded = json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        provider_request = Request(
            self._config.endpoint,
            data=encoded,
            headers={
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
                "x-api-key": credential,
            },
            method="POST",
        )
        try:
            with urlopen(provider_request, timeout=60) as stream:
                status = stream.status
                payload = stream.read()
        except HTTPError as error:
            raise AnthropicReplayError(f"Anthropic API returned HTTP {error.code}") from error
        except URLError as error:
            raise AnthropicReplayError(f"Anthropic API request failed: {error.reason}") from error
        except OSError as error:
            raise AnthropicReplayError(f"Anthropic API request failed: {error}") from error
        if status != 200:
            raise AnthropicReplayError(f"Anthropic API returned HTTP {status}")
        try:
            response = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AnthropicReplayError("Anthropic API returned invalid JSON") from error
        document = _json_object(response, "Anthropic API response")
        if _required_text(document, "model", "provider response") != self._config.model:
            raise AnthropicReplayError("Anthropic API response model does not match configuration")
        return document


def _tool_definitions(request: PairedReplayRequest) -> list[dict[str, object]]:
    names = sorted({observation.name for observation in request.condition.observations})
    return [
        {
            "name": name,
            "description": "Replay the recorded tool interaction when the task requires it.",
            "input_schema": {"type": "object", "additionalProperties": True},
        }
        for name in names
    ]


def _usage(response: Mapping[str, JsonValue], config: PairedReplayConfig) -> dict[str, object]:
    usage = _json_object(response.get("usage"), "provider response usage")
    try:
        normalize_usage(usage, config.usage_mapping)
    except ValueError as error:
        raise AnthropicReplayError(
            f"Anthropic API response has invalid native usage: {error}"
        ) from error
    return cast(dict[str, object], usage)


def _content(response: Mapping[str, JsonValue]) -> list[dict[str, JsonValue]]:
    content = response.get("content")
    if not isinstance(content, list):
        raise AnthropicReplayError("provider response content must be an array")
    return [_json_object(block, "provider response content block") for block in content]


def _json_object(value: object, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise AnthropicReplayError(f"{label} must be an object")
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise AnthropicReplayError(f"{label} must be JSON") from error
    return cast(dict[str, JsonValue], value)


def _required_text(document: Mapping[str, JsonValue], key: str, label: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AnthropicReplayError(f"{label} {key} must be non-empty text")
    return value


def _validate_decoding_parameters(parameters: Mapping[str, JsonValue]) -> None:
    expected = {"extended_thinking", "max_tokens", "temperature", "top_p"}
    if set(parameters) != expected:
        raise AnthropicReplayError(
            "Anthropic replay decoding parameters must explicitly declare "
            "max_tokens, temperature, top_p, and extended_thinking"
        )
    max_tokens = parameters["max_tokens"]
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        raise AnthropicReplayError("Anthropic replay max_tokens must be a positive integer")
    temperature = parameters["temperature"]
    if not isinstance(temperature, float) or not 0.0 <= temperature <= 1.0:
        raise AnthropicReplayError("Anthropic replay temperature must be a float in [0, 1]")
    top_p = parameters["top_p"]
    if not isinstance(top_p, float) or not 0.0 < top_p <= 1.0:
        raise AnthropicReplayError("Anthropic replay top_p must be a float in (0, 1]")
    if parameters["extended_thinking"] is not False:
        raise AnthropicReplayError("Anthropic replay extended_thinking must be false")



def _require_credential() -> str:
    credential = os.environ.get(ANTHROPIC_API_KEY_ENVIRONMENT)
    if credential is None or not credential.strip():
        raise AnthropicReplayError(
            f"missing required process environment credential {ANTHROPIC_API_KEY_ENVIRONMENT}"
        )
    return credential