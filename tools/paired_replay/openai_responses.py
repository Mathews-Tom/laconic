"""Direct OpenAI Responses client for the approved private paired replay contract."""

from __future__ import annotations

import base64
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from laconic.codec.observe import ObservationCodec, subject_for
from laconic.ledger import Ledger
from tools.paired_replay.evidence import JsonValue
from tools.paired_replay.interaction import ReceiptToolProjection
from tools.paired_replay.runner import (
    PairedReplayError,
    PairedReplayRequest,
    PairedReplayResponse,
    PairedResponseTurn,
    require_process_credential,
)


class _NoRedirect(HTTPRedirectHandler):
    """Reject redirects rather than following a credential-bearing request."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        """Return no redirect target so urllib surfaces the provider response."""
        return None


class _OpenAIHTTPError(PairedReplayError):
    """A provider error whose exact response body must remain private evidence."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        super().__init__(f"OpenAI Responses request failed: HTTP {status}")


class OpenAIResponsesClient:
    """Execute one frozen paired replay arm through OpenAI's direct Responses endpoint."""

    def respond(self, request: PairedReplayRequest) -> PairedReplayResponse:
        """Run an authenticated arm and retain provider bodies only in its artifact."""
        credential = require_process_credential(request.config.credential_environment)
        initial_prompts, follow_up_prompts = _prompt_schedule(request)
        if not initial_prompts:
            raise PairedReplayError("OpenAI Responses replay requires an initial user prompt")
        response_turns: list[PairedResponseTurn] = []
        conversation = _user_input_items(initial_prompts)
        with tempfile.TemporaryDirectory(
            prefix="laconic-paired-replay-", dir=request.config.artifact_root
        ) as temporary:
            ledger = Ledger(Path(temporary) / "codec.sqlite", request.run_id)
            try:
                codec = ObservationCodec(ledger)
                follow_up_index = 0
                while True:
                    projection = request.interaction.next_tool
                    payload = _request_payload(request, conversation, projection)
                    try:
                        response = self._post(request, credential, payload)
                    except _OpenAIHTTPError as error:
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
                                f"OpenAI request failed after billed response: {error}",
                            )
                        )
                        return PairedReplayResponse(tuple(response_turns))
                    try:
                        _validate_response_model(response, request.config.model)
                        usage = _usage(response)
                        output_items = _output_items(response)
                        function_calls = _function_calls(output_items)
                    except PairedReplayError as error:
                        response_turns.append(
                            PairedResponseTurn(
                                response,
                                _unvalidated_usage(response),
                                "unsupported",
                                f"OpenAI Responses response is invalid: {error}",
                            )
                        )
                        return PairedReplayResponse(tuple(response_turns))
                    if not function_calls:
                        response_turns.append(PairedResponseTurn(response, usage, "completed"))
                        if projection is not None:
                            return _unsupported_response(
                                response_turns,
                                response,
                                usage,
                                "provider emitted no tool action for current receipt position",
                            )
                        return PairedReplayResponse(tuple(response_turns))
                    if len(function_calls) != 1:
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
                        call_id, name, tool_input = _function_call(function_calls[0])
                    except PairedReplayError as error:
                        response_turns[-1] = PairedResponseTurn(
                            response,
                            usage,
                            "unsupported",
                            f"OpenAI Responses function call is invalid: {error}",
                        )
                        return PairedReplayResponse(tuple(response_turns))
                    resolution = request.interaction.resolve_tool(name, tool_input)
                    if resolution.disposition == "unsupported":
                        response_turns[-1] = PairedResponseTurn(
                            response, usage, "unsupported", resolution.reason
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
                    conversation.extend(
                        [
                            *output_items,
                            {
                                "call_id": call_id,
                                "output": output,
                                "type": "function_call_output",
                            },
                            *_user_input_items(follow_up_prompts[follow_up_index]),
                        ]
                    )
                    follow_up_index += 1
            finally:
                ledger.close()

    def _post(
        self,
        replay: PairedReplayRequest,
        credential: str,
        payload: dict[str, object],
    ) -> dict[str, JsonValue]:
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
            raise _OpenAIHTTPError(error.code, error.read()) from error
        except (OSError, URLError, json.JSONDecodeError) as error:
            raise PairedReplayError(f"OpenAI Responses request failed: {error}") from error
        if not isinstance(document, dict):
            raise PairedReplayError("OpenAI Responses response must be an object")
        return cast(dict[str, JsonValue], document)


def _request_payload(
    request: PairedReplayRequest,
    input_items: list[dict[str, JsonValue]],
    projection: ReceiptToolProjection | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "input": list(input_items),
        "max_output_tokens": request.config.decoding_parameters["max_output_tokens"],
        "model": request.config.model,
        "parallel_tool_calls": request.config.decoding_parameters["parallel_tool_calls"],
        "prompt_cache_retention": request.config.decoding_parameters["prompt_cache_retention"],
        "reasoning": {"effort": request.config.decoding_parameters["reasoning_effort"]},
        "store": request.config.decoding_parameters["store"],
        "stream": request.config.decoding_parameters["stream"],
        "temperature": request.config.decoding_parameters["temperature"],
    }
    if projection is None:
        return payload
    name = projection.name
    parameters = _draft2020_schema(projection.input_schema.to_document())
    payload["tool_choice"] = {
        "mode": "auto",
        "tools": [{"name": name, "type": "function"}],
        "type": "allowed_tools",
    }
    payload["tools"] = [
        {
            "description": "Replay-authorized native tool",
            "name": name,
            "parameters": parameters,
            "type": "function",
        }
    ]
    return payload


def _draft2020_schema(value: JsonValue) -> JsonValue:
    """Convert receipt tuple arrays to the equivalent Draft 2020-12 schema."""
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


def _user_input_items(prompts: tuple[str, ...]) -> list[dict[str, JsonValue]]:
    return [
        {"content": [{"text": prompt, "type": "input_text"}], "role": "user"} for prompt in prompts
    ]


def _unsupported_response(
    response_turns: list[PairedResponseTurn],
    response: dict[str, JsonValue],
    usage: dict[str, object],
    reason: str,
) -> PairedReplayResponse:
    response_turns[-1] = PairedResponseTurn(response, usage, "unsupported", reason)
    return PairedReplayResponse(tuple(response_turns))


def _validate_response_model(document: Mapping[str, object], expected_model: str) -> None:
    if document.get("model") != expected_model:
        raise PairedReplayError("OpenAI Responses response model does not match configuration")


def _usage(document: Mapping[str, object]) -> dict[str, object]:
    usage = _mapping(document, "usage")
    details = _mapping(usage, "input_tokens_details")
    return {
        "usage.input_tokens": _counter(usage, "input_tokens"),
        "usage.input_tokens_details.cached_tokens": _counter(details, "cached_tokens"),
        "usage.input_tokens_details.cache_write_tokens": _counter(details, "cache_write_tokens"),
        "usage.output_tokens": _counter(usage, "output_tokens"),
    }


def _unvalidated_usage(document: Mapping[str, object]) -> dict[str, object]:
    usage = document.get("usage")
    if not isinstance(usage, dict):
        return {}
    details = usage.get("input_tokens_details")
    if not isinstance(details, dict):
        details = {}
    return {
        "usage.input_tokens": usage.get("input_tokens"),
        "usage.input_tokens_details.cached_tokens": details.get("cached_tokens"),
        "usage.input_tokens_details.cache_write_tokens": details.get("cache_write_tokens"),
        "usage.output_tokens": usage.get("output_tokens"),
    }


def _output_items(document: Mapping[str, object]) -> list[dict[str, JsonValue]]:
    output = document.get("output")
    if not isinstance(output, list) or not all(isinstance(item, dict) for item in output):
        raise PairedReplayError("OpenAI Responses field 'output' must be an array of objects")
    return [cast(dict[str, JsonValue], item) for item in output]


def _function_calls(output_items: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    return [item for item in output_items if item.get("type") == "function_call"]


def _function_call(item: Mapping[str, JsonValue]) -> tuple[str, str, dict[str, JsonValue]]:
    arguments = _text(item, "arguments")
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as error:
        raise PairedReplayError("OpenAI Responses function arguments must be JSON") from error
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        raise PairedReplayError("OpenAI Responses function arguments must be an object")
    return _text(item, "call_id"), _text(item, "name"), cast(dict[str, JsonValue], parsed)


def _mapping(document: Mapping[str, object], field: str) -> dict[str, object]:
    value = document.get(field)
    if not isinstance(value, dict):
        raise PairedReplayError(f"OpenAI Responses response field {field!r} must be an object")
    return cast(dict[str, object], value)


def _counter(document: Mapping[str, object], field: str) -> int:
    value = document.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PairedReplayError(
            f"OpenAI Responses usage field {field!r} must be a non-negative integer"
        )
    return value


def _text(document: Mapping[str, JsonValue], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value:
        raise PairedReplayError(f"OpenAI Responses field {field!r} must be non-empty text")
    return value
