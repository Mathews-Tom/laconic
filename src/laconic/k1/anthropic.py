"""Concrete first-party Anthropic Messages client for authorized K1 replay."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from laconic.codec.observe import ObservationCodec, subject_for
from laconic.k1.evidence import JsonValue
from laconic.k1.paired_runner import (
    PairedReplayError,
    PairedReplayRequest,
    PairedReplayResponse,
    PairedResponseTurn,
)
from laconic.ledger import Ledger

_ANTHROPIC_VERSION = "2023-06-01"


def require_process_credential(credential_environment: str) -> str:
    """Return the one approved process credential or fail before source access."""
    credential = os.environ.get(credential_environment)
    if not credential:
        raise PairedReplayError(
            f"required credential environment {credential_environment!r} is unset"
        )
    return credential


class _NoRedirect(HTTPRedirectHandler):
    """Reject redirects rather than following a credential-bearing request."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        """Return no redirect target so urllib surfaces the provider response."""
        return None


class AnthropicMessagesClient:
    """Execute one frozen K1 arm through the Anthropic Messages HTTPS endpoint."""

    def respond(self, request: PairedReplayRequest) -> PairedReplayResponse:
        """Run an authenticated arm and retain provider bodies only in its artifact."""
        credential = require_process_credential(request.config.credential_environment)
        initial_prompts, follow_up_prompts = _prompt_schedule(request)
        if len(initial_prompts) != 1:
            raise PairedReplayError(
                "Anthropic replay requires exactly one initial provider-deliverable user prompt"
            )
        messages: list[dict[str, object]] = [{"role": "user", "content": initial_prompts[0]}]
        response_turns: list[PairedResponseTurn] = []
        with tempfile.TemporaryDirectory(prefix="laconic-k1-replay-") as temporary:
            ledger = Ledger(Path(temporary) / "codec.sqlite", request.run_id)
            try:
                codec = ObservationCodec(ledger)
                follow_up_index = 0
                while True:
                    response = self._post(request, credential, messages)
                    usage = _mapping(response, "usage")
                    content = _content(response)
                    tool_uses = _tool_uses(content)
                    if not tool_uses:
                        response_turns.append(PairedResponseTurn(response, usage, "completed"))
                        return PairedReplayResponse(tuple(response_turns))
                    if len(tool_uses) != 1:
                        response_turns.append(
                            PairedResponseTurn(
                                response,
                                usage,
                                "unsupported",
                                "provider emitted multiple tool actions in one chronological turn",
                            )
                        )
                        return PairedReplayResponse(tuple(response_turns))
                    response_turns.append(PairedResponseTurn(response, usage, "completed"))
                    messages.append({"role": "assistant", "content": content})
                    tool_use = tool_uses[0]
                    name = _text(tool_use, "name")
                    tool_input = cast(dict[str, JsonValue], _mapping(tool_use, "input"))
                    resolution = request.interaction.resolve_tool(name, tool_input)
                    if resolution.disposition == "unsupported":
                        response_turns[-1] = PairedResponseTurn(
                            response,
                            usage,
                            "unsupported",
                            resolution.reason,
                        )
                        return PairedReplayResponse(tuple(response_turns))
                    if resolution.output is None:
                        raise PairedReplayError("authorized tool action produced no output")
                    output = json.dumps(resolution.output, sort_keys=True, separators=(",", ":"))
                    if request.arm == "codec":
                        output = codec.encode(
                            name,
                            subject_for(tool_input),
                            output,
                            tool_input,
                            turn=len(response_turns),
                        ).encoded
                    if resolution.disposition == "induced":
                        response_turns[-1] = PairedResponseTurn(response, usage, "induced")
                    if follow_up_index >= len(follow_up_prompts):
                        raise PairedReplayError(
                            "interaction receipt has no follow-up schedule for authorized action"
                        )
                    tool_results: list[dict[str, object]] = [
                        {
                            "type": "tool_result",
                            "tool_use_id": _text(tool_use, "id"),
                            "content": output,
                        }
                    ]
                    tool_results.extend(
                        {"type": "text", "text": prompt}
                        for prompt in follow_up_prompts[follow_up_index]
                    )
                    follow_up_index += 1
                    messages.append({"role": "user", "content": tool_results})
            finally:
                ledger.close()

    def _post(
        self,
        replay: PairedReplayRequest,
        credential: str,
        messages: list[dict[str, object]],
    ) -> dict[str, JsonValue]:
        payload: dict[str, object] = {
            "max_tokens": replay.config.decoding_parameters["max_tokens"],
            "messages": messages,
            "model": replay.config.model,
            "temperature": replay.config.decoding_parameters["temperature"],
            "tools": _tools(replay),
            "top_p": replay.config.decoding_parameters["top_p"],
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        request = Request(
            replay.config.endpoint,
            data=body,
            headers={
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
                "x-api-key": credential,
            },
            method="POST",
        )
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=30) as response:
                document = json.loads(response.read().decode())
        except HTTPError as error:
            raise PairedReplayError(
                f"Anthropic Messages request failed: HTTP {error.code}"
            ) from error
        except (OSError, URLError, json.JSONDecodeError) as error:
            raise PairedReplayError(f"Anthropic Messages request failed: {error}") from error
        if not isinstance(document, dict):
            raise PairedReplayError("Anthropic Messages response must be an object")
        if document.get("model") != replay.config.model:
            raise PairedReplayError(
                "Anthropic Messages response model does not match configuration"
            )
        return cast(dict[str, JsonValue], document)


def _prompt_schedule(
    request: PairedReplayRequest,
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    prompt_by_index = {prompt.native_index: prompt.text for prompt in request.interaction.prompts}
    tool_calls = [
        event.native_index
        for event in request.interaction.receipt.events
        if event.kind == "tool_call"
    ]
    tool_results = [
        event.native_index
        for event in request.interaction.receipt.events
        if event.kind == "tool_result"
    ]
    if len(tool_calls) != len(tool_results):
        raise PairedReplayError("interaction receipt tool-call/result chronology is incomplete")
    initial_boundary = tool_calls[0] if tool_calls else float("inf")
    initial = tuple(
        prompt_by_index[index] for index in sorted(prompt_by_index) if index < initial_boundary
    )
    follow_ups = tuple(
        tuple(
            prompt_by_index[index]
            for index in sorted(prompt_by_index)
            if result_index < index < next_call
        )
        for result_index, next_call in zip(
            tool_results, (*tool_calls[1:], float("inf")), strict=True
        )
    )
    return initial, follow_ups


def _tools(request: PairedReplayRequest) -> list[dict[str, object]]:
    definitions: dict[str, dict[str, JsonValue]] = {}
    for event in request.interaction.receipt.events:
        if event.kind != "tool_call":
            continue
        if event.tool_name is None or event.input_schema is None:
            raise PairedReplayError("interaction receipt has incomplete tool definition")
        schema = event.input_schema.to_document()
        prior = definitions.setdefault(event.tool_name, schema)
        if prior != schema:
            raise PairedReplayError(
                "interaction receipt reuses a tool name with conflicting schemas"
            )
    return [
        {"name": name, "description": "Replay-authorized native tool", "input_schema": schema}
        for name, schema in sorted(definitions.items())
    ]


def _mapping(document: Mapping[str, object], field: str) -> dict[str, object]:
    value = document.get(field)
    if not isinstance(value, dict):
        raise PairedReplayError(f"Anthropic Messages response field {field!r} must be an object")
    return cast(dict[str, object], value)


def _content(document: Mapping[str, object]) -> list[dict[str, JsonValue]]:
    value = document.get("content")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise PairedReplayError("Anthropic Messages response field 'content' must be an array")
    return [cast(dict[str, JsonValue], item) for item in value]


def _tool_uses(content: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    return [item for item in content if item.get("type") == "tool_use"]


def _text(document: Mapping[str, object], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value:
        raise PairedReplayError(
            f"Anthropic Messages response field {field!r} must be non-empty text"
        )
    return value
