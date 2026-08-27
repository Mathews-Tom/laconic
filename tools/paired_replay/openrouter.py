"""Corrected OpenRouter Chat Completions client (R-18/H-72).

Reopens M4E under a corrected single-tool-declaration pattern after H-66 traced
H-41's failure to the retired adapter declaring the full session tool universe on
every turn. This adapter declares exactly the current M3E receipt-projected action
(never the full receipt/session universe) with ``tool_choice:"auto"``; OpenRouter has
no ``allowed_tools``-equivalent primitive (H-65), so this containment is empirical,
not structural. ``InteractionActionResolver`` remains final authority regardless of
transport, and every configuration built under this adapter carries
``containment_mechanism:"empirical_single_tool_declaration"`` (config.py) so M5-M7
never pool this evidence class with M4S's provider-enforced ``allowed_tools`` pairs.
"""

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
from tools.paired_replay.config import ProviderRouting
from tools.paired_replay.evidence import JsonValue
from tools.paired_replay.interaction import ReceiptToolProjection
from tools.paired_replay.openai_responses import _prompt_schedule
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


class _OpenRouterHTTPError(PairedReplayError):
    """A provider error whose exact response body must remain private evidence."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        super().__init__(f"OpenRouter request failed: HTTP {status}")


class OpenRouterChatCompletionsClient:
    """Execute one frozen paired replay arm through the corrected OpenRouter contract."""

    def respond(self, request: PairedReplayRequest) -> PairedReplayResponse:
        """Run an authenticated arm and retain provider bodies only in its artifact."""
        credential = require_process_credential(request.config.credential_environment)
        routing = request.config.provider_routing
        if routing is None:
            raise PairedReplayError("OpenRouter replay requires configured provider routing")
        initial_prompts, follow_up_prompts = _prompt_schedule(request)
        if not initial_prompts:
            raise PairedReplayError("OpenRouter replay requires an initial user prompt")
        response_turns: list[PairedResponseTurn] = []
        messages: list[dict[str, JsonValue]] = _user_message_items(initial_prompts)
        with tempfile.TemporaryDirectory(
            prefix="laconic-paired-replay-", dir=request.config.artifact_root
        ) as temporary:
            ledger = Ledger(Path(temporary) / "codec.sqlite", request.run_id)
            try:
                codec = ObservationCodec(ledger)
                follow_up_index = 0
                while True:
                    projection = request.interaction.next_tool
                    payload = _request_payload(request, messages, routing, projection)
                    try:
                        response = self._post(request, credential, payload)
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
                        response_turns.append(PairedResponseTurn(response, usage, "completed"))
                        if projection is not None:
                            return _unsupported_response(
                                response_turns,
                                response,
                                usage,
                                "provider emitted no tool action for current receipt position",
                            )
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
                    messages.append(
                        {"role": "tool", "tool_call_id": tool_call_id, "content": output}
                    )
                    messages.extend(_user_message_items(follow_up_prompts[follow_up_index]))
                    follow_up_index += 1
            finally:
                ledger.close()

    def _post(
        self,
        request: PairedReplayRequest,
        credential: str,
        payload: dict[str, object],
    ) -> dict[str, JsonValue]:
        body = json.dumps(payload, separators=(",", ":")).encode()
        provider_request = Request(
            request.config.endpoint,
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
            raise PairedReplayError(f"OpenRouter request failed: {error}") from error
        if not isinstance(document, dict):
            raise PairedReplayError("OpenRouter response must be an object")
        return cast(dict[str, JsonValue], document)


def _user_message_items(prompts: tuple[str, ...]) -> list[dict[str, JsonValue]]:
    """Build Chat Completions user messages for the OpenRouter endpoint."""
    return [{"role": "user", "content": [{"type": "text", "text": prompt}]} for prompt in prompts]


def _request_payload(
    request: PairedReplayRequest,
    messages: list[dict[str, JsonValue]],
    routing: ProviderRouting,
    projection: ReceiptToolProjection | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "messages": list(messages),
        "model": request.config.model,
        "provider": routing.to_payload(),
        **request.config.decoding_parameters,
    }
    if projection is None:
        return payload
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "description": "Replay-authorized native tool",
                "name": projection.name,
                "parameters": projection.input_schema.to_document(),
            },
        }
    ]
    payload["tool_choice"] = "auto"
    return payload


def _validate_response_model(document: Mapping[str, object], expected_model: str) -> None:
    if document.get("model") != expected_model:
        raise PairedReplayError("OpenRouter response model does not match configuration")


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
    if not isinstance(choices, list) or not choices:
        raise PairedReplayError("OpenRouter response 'choices' must be a non-empty array")
    first = choices[0]
    if not isinstance(first, dict):
        raise PairedReplayError("OpenRouter response choice must be an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise PairedReplayError("OpenRouter response message must be an object")
    return cast(dict[str, object], message)


def _tool_calls(assistant: Mapping[str, object]) -> list[dict[str, JsonValue]]:
    tool_calls = assistant.get("tool_calls")
    if tool_calls is None:
        return []
    if not isinstance(tool_calls, list) or not all(isinstance(item, dict) for item in tool_calls):
        raise PairedReplayError("OpenRouter tool_calls must be an array of objects")
    return [cast(dict[str, JsonValue], item) for item in tool_calls]


def _assistant_message(
    assistant: Mapping[str, object], tool_calls: list[dict[str, JsonValue]]
) -> dict[str, JsonValue]:
    return {
        "role": "assistant",
        "content": cast(JsonValue, assistant.get("content")),
        "tool_calls": cast(JsonValue, tool_calls),
    }


def _tool_call(call: Mapping[str, JsonValue]) -> tuple[str, str, dict[str, JsonValue]]:
    call_id = call.get("id")
    function = call.get("function")
    if not isinstance(call_id, str) or not call_id:
        raise PairedReplayError("OpenRouter tool call must carry a non-empty id")
    if not isinstance(function, dict):
        raise PairedReplayError("OpenRouter tool call function must be an object")
    name = function.get("name")
    arguments = function.get("arguments")
    if not isinstance(name, str) or not name:
        raise PairedReplayError("OpenRouter tool call function name must be non-empty text")
    if not isinstance(arguments, str):
        raise PairedReplayError("OpenRouter tool call arguments must be a JSON string")
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as error:
        raise PairedReplayError("OpenRouter tool call arguments must be JSON") from error
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        raise PairedReplayError("OpenRouter tool call arguments must be an object")
    return call_id, name, cast(dict[str, JsonValue], parsed)


def _unsupported_response(
    response_turns: list[PairedResponseTurn],
    response: dict[str, JsonValue],
    usage: dict[str, object],
    reason: str,
) -> PairedReplayResponse:
    response_turns[-1] = PairedResponseTurn(response, usage, "unsupported", reason)
    return PairedReplayResponse(tuple(response_turns))


def _mapping(document: Mapping[str, object], field: str) -> dict[str, object]:
    value = document.get(field)
    if not isinstance(value, dict):
        raise PairedReplayError(f"OpenRouter response field {field!r} must be an object")
    return cast(dict[str, object], value)


def _counter(document: Mapping[str, object], field: str) -> int:
    value = document.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PairedReplayError(f"OpenRouter usage field {field!r} must be a non-negative integer")
    return value
