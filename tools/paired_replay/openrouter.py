"""Concrete OpenRouter Chat Completions client for authorized paired replay replay."""

from __future__ import annotations

import base64
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from laconic.codec.observe import ObservationCodec, subject_for
from laconic.ledger import Ledger
from tools.paired_replay.evidence import JsonValue
from tools.paired_replay.runner import (
    PairedReplayError,
    PairedReplayRequest,
    PairedReplayResponse,
    PairedResponseTurn,
)


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


class _OpenRouterHTTPError(PairedReplayError):
    """A provider error whose exact response body must remain private evidence."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        super().__init__(f"OpenRouter Chat Completions request failed: HTTP {status}")


class OpenRouterChatCompletionsClient:
    """Execute one frozen paired replay arm through the OpenRouter Chat Completions endpoint."""

    def respond(self, request: PairedReplayRequest) -> PairedReplayResponse:
        """Run an authenticated arm and retain provider bodies only in its artifact."""
        credential = require_process_credential(request.config.credential_environment)
        initial_prompts, follow_up_prompts = _prompt_schedule(request)
        if len(initial_prompts) != 1:
            raise PairedReplayError(
                "OpenRouter replay requires exactly one initial provider-deliverable user prompt"
            )
        messages: list[dict[str, object]] = [{"role": "user", "content": initial_prompts[0]}]
        response_turns: list[PairedResponseTurn] = []
        with tempfile.TemporaryDirectory(
            prefix="laconic-k1-replay-", dir=request.config.artifact_root
        ) as temporary:
            ledger = Ledger(Path(temporary) / "codec.sqlite", request.run_id)
            try:
                codec = ObservationCodec(ledger)
                follow_up_index = 0
                while True:
                    try:
                        response = self._post(request, credential, messages)
                    except _OpenRouterHTTPError as error:
                        response_turns.append(
                            PairedResponseTurn(
                                {
                                    "body_base64": base64.b64encode(error.body).decode("ascii"),
                                    "http_status": error.status,
                                },
                                {},
                                "unsupported",
                                str(error),
                            )
                        )
                        return PairedReplayResponse(tuple(response_turns))
                    except PairedReplayError as error:
                        if not response_turns:
                            raise
                        response_turns.append(
                            PairedResponseTurn(
                                None,
                                {},
                                "unsupported",
                                f"OpenRouter request failed after billed response: {error}",
                            )
                        )
                        return PairedReplayResponse(tuple(response_turns))
                    try:
                        _validate_response_model(response, request.config.model)
                        usage = _usage(response)
                        assistant = _assistant(response)
                        tool_calls = _tool_calls(assistant)
                    except PairedReplayError as error:
                        response_turns.append(
                            PairedResponseTurn(
                                response,
                                _unvalidated_usage(response),
                                "unsupported",
                                f"OpenRouter response is invalid: {error}",
                            )
                        )
                        return PairedReplayResponse(tuple(response_turns))
                    if not tool_calls:
                        if assistant.get("content") is None:
                            response_turns.append(PairedResponseTurn(response, usage, "completed"))
                            return _unsupported_response(
                                response_turns,
                                response,
                                usage,
                                "OpenRouter assistant response has neither text nor a tool call",
                            )
                        response_turns.append(PairedResponseTurn(response, usage, "completed"))
                        return PairedReplayResponse(tuple(response_turns))
                    if len(tool_calls) != 1:
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
                    try:
                        assistant_message = _assistant_message(assistant, tool_calls)
                        tool_call_id, name, tool_input = _tool_call(tool_calls[0])
                    except PairedReplayError as error:
                        response_turns[-1] = PairedResponseTurn(
                            response,
                            usage,
                            "unsupported",
                            f"OpenRouter tool call is invalid: {error}",
                        )
                        return PairedReplayResponse(tuple(response_turns))
                    messages.append(assistant_message)
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
                        return _unsupported_response(
                            response_turns,
                            response,
                            usage,
                            "authorized tool action produced no output",
                        )
                    try:
                        output = json.dumps(
                            resolution.output, sort_keys=True, separators=(",", ":")
                        )
                        if request.arm == "codec":
                            output = codec.encode(
                                name,
                                subject_for(tool_input),
                                output,
                                tool_input,
                                turn=len(response_turns),
                            ).encoded
                    except (OSError, RuntimeError, TypeError, ValueError) as error:
                        return _unsupported_response(
                            response_turns,
                            response,
                            usage,
                            f"codec execution failed: {error}",
                        )
                    if resolution.disposition == "induced":
                        response_turns[-1] = PairedResponseTurn(response, usage, "induced")
                    if follow_up_index >= len(follow_up_prompts):
                        return _unsupported_response(
                            response_turns,
                            response,
                            usage,
                            "interaction receipt has no follow-up schedule for authorized action",
                        )
                    messages.append(
                        {"role": "tool", "tool_call_id": tool_call_id, "content": output}
                    )
                    messages.extend(
                        {"role": "user", "content": prompt}
                        for prompt in follow_up_prompts[follow_up_index]
                    )
                    follow_up_index += 1
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
            "provider": replay.config.provider_routing.to_payload(),
            "temperature": replay.config.decoding_parameters["temperature"],
            "tools": _tools(replay),
            "top_p": replay.config.decoding_parameters["top_p"],
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        provider_request = Request(
            replay.config.endpoint,
            data=body,
            headers={
                "authorization": f"Bearer {credential}",
                "content-type": "application/json",
            },
            method="POST",
        )
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(provider_request, timeout=30) as response:
                document = json.loads(response.read().decode())
        except HTTPError as error:
            raise _OpenRouterHTTPError(error.code, error.read()) from error
        except (OSError, URLError, json.JSONDecodeError) as error:
            raise PairedReplayError(
                f"OpenRouter Chat Completions request failed: {error}"
            ) from error
        if not isinstance(document, dict):
            raise PairedReplayError("OpenRouter Chat Completions response must be an object")
        return cast(dict[str, JsonValue], document)


def _unsupported_response(
    response_turns: list[PairedResponseTurn],
    response: dict[str, JsonValue],
    usage: dict[str, object],
    reason: str,
) -> PairedReplayResponse:
    response_turns[-1] = PairedResponseTurn(response, usage, "unsupported", reason)
    return PairedReplayResponse(tuple(response_turns))


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
    definitions: dict[str, dict[str, dict[str, JsonValue]]] = {}
    for event in request.interaction.receipt.events:
        if event.kind != "tool_call":
            continue
        if event.tool_name is None or event.input_schema is None:
            raise PairedReplayError("interaction receipt has incomplete tool definition")
        converted = _draft2020_schema(event.input_schema.to_document())
        if not isinstance(converted, dict):
            raise PairedReplayError("receipt tool schema must be an object")
        canonical_schema = json.dumps(converted, separators=(",", ":"), sort_keys=True)
        definitions.setdefault(event.tool_name, {})[canonical_schema] = converted
    return [
        {
            "type": "function",
            "function": {
                "description": "Replay-authorized native tool",
                "name": name,
                "parameters": _tool_parameters(schemas),
            },
        }
        for name, schemas in sorted(definitions.items())
    ]


def _tool_parameters(
    schemas: dict[str, dict[str, JsonValue]],
) -> dict[str, JsonValue]:
    if len(schemas) == 1:
        return next(iter(schemas.values()))

    property_sets: list[set[str]] = []
    required_sets: list[set[str]] = []
    properties: dict[str, JsonValue] = {}
    all_closed = True
    for schema in schemas.values():
        raw_properties = schema.get("properties")
        if not isinstance(raw_properties, dict):
            raise PairedReplayError("receipt tool schema properties must be an object")
        names = set(raw_properties)
        property_sets.append(names)
        all_closed = all_closed and schema.get("additionalProperties") is False
        raw_required = schema.get("required", [])
        if not isinstance(raw_required, list):
            raise PairedReplayError("receipt tool schema required must be an array of strings")
        required_names = [name for name in raw_required if isinstance(name, str)]
        if len(required_names) != len(raw_required):
            raise PairedReplayError("receipt tool schema required must be an array of strings")
        required_sets.append(set(required_names))
        for name, definition in raw_properties.items():
            prior = properties.get(name)
            if prior is None:
                properties[name] = definition
            elif prior != definition:
                properties[name] = {}

    projected: dict[str, JsonValue] = {"properties": properties, "type": "object"}
    common_required = set.intersection(*required_sets)
    if common_required:
        projected["required"] = cast(JsonValue, sorted(common_required))
    if all_closed and all(names == property_sets[0] for names in property_sets):
        projected["additionalProperties"] = False
    return projected


def _draft2020_schema(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return [_draft2020_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    converted: dict[str, JsonValue] = {}
    for key, item in value.items():
        if key == "items" and isinstance(item, list):
            converted["prefixItems"] = _draft2020_schema(item)
            converted["items"] = False
        else:
            converted[key] = _draft2020_schema(item)
    return converted


def _validate_response_model(document: Mapping[str, object], expected_model: str) -> None:
    if document.get("model") != expected_model:
        raise PairedReplayError(
            "OpenRouter Chat Completions response model does not match configuration"
        )


def _usage(document: Mapping[str, object]) -> dict[str, object]:
    usage = _mapping(document, "usage")
    details = _mapping(usage, "prompt_tokens_details")
    return {
        "usage.prompt_tokens": _counter(usage, "prompt_tokens"),
        "usage.prompt_tokens_details.cached_tokens": _counter(details, "cached_tokens"),
        "usage.prompt_tokens_details.cache_write_tokens": _counter(details, "cache_write_tokens"),
        "usage.completion_tokens": _counter(usage, "completion_tokens"),
    }


def _unvalidated_usage(document: Mapping[str, object]) -> dict[str, object]:
    usage = document.get("usage")
    if not isinstance(usage, dict):
        return {}
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = {}
    return {
        "usage.prompt_tokens": usage.get("prompt_tokens"),
        "usage.prompt_tokens_details.cached_tokens": details.get("cached_tokens"),
        "usage.prompt_tokens_details.cache_write_tokens": details.get("cache_write_tokens"),
        "usage.completion_tokens": usage.get("completion_tokens"),
    }


def _assistant(document: Mapping[str, object]) -> dict[str, object]:
    choices = document.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise PairedReplayError(
            "OpenRouter Chat Completions response field 'choices' must contain one choice"
        )
    return _mapping(cast(dict[str, object], choices[0]), "message")


def _tool_calls(assistant: Mapping[str, object]) -> list[dict[str, object]]:
    tool_calls = assistant.get("tool_calls")
    if tool_calls is None:
        return []
    if not isinstance(tool_calls, list) or not all(isinstance(call, dict) for call in tool_calls):
        raise PairedReplayError(
            "OpenRouter Chat Completions assistant field 'tool_calls' must be an array"
        )
    return [cast(dict[str, object], call) for call in tool_calls]


def _assistant_message(
    assistant: Mapping[str, object], tool_calls: list[dict[str, object]]
) -> dict[str, object]:
    content = assistant.get("content")
    if content is not None and not isinstance(content, str):
        raise PairedReplayError(
            "OpenRouter Chat Completions assistant field 'content' must be text or null"
        )
    message: dict[str, object] = {"role": "assistant", "tool_calls": tool_calls}
    if content is not None:
        message["content"] = content
    return message


def _tool_call(tool_call: Mapping[str, object]) -> tuple[str, str, dict[str, JsonValue]]:
    if tool_call.get("type") != "function":
        raise PairedReplayError("OpenRouter tool call type must be 'function'")
    function = _mapping(tool_call, "function")
    arguments = _text(function, "arguments")
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as error:
        raise PairedReplayError("OpenRouter tool call arguments must be JSON") from error
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        raise PairedReplayError("OpenRouter tool call arguments must be an object")
    return _text(tool_call, "id"), _text(function, "name"), cast(dict[str, JsonValue], parsed)


def _mapping(document: Mapping[str, object], field: str) -> dict[str, object]:
    value = document.get(field)
    if not isinstance(value, dict):
        raise PairedReplayError(
            f"OpenRouter Chat Completions response field {field!r} must be an object"
        )
    return cast(dict[str, object], value)


def _counter(document: Mapping[str, object], field: str) -> int:
    value = document.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PairedReplayError(
            f"OpenRouter Chat Completions usage field {field!r} must be a non-negative integer"
        )
    return value


def _text(document: Mapping[str, object], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value:
        raise PairedReplayError(
            f"OpenRouter Chat Completions field {field!r} must be non-empty text"
        )
    return value
