"""K1 Stage C's real `ReplayClient`: drives `omp --mode rpc` as one
persistent process per baseline session, sending one `prompt` command per
replayed turn and parsing the resulting event stream into
`laconic.replay.engine.ReplayTurnCapture` sequences.

Implements `laconic.replay.engine.ReplayClient` (`respond()`). Lives in the
separately importable `laconic_stage_c` sibling package, which `src/laconic`
never imports.

Architecture and wire-protocol citations
-----------------------------------------
`.docs/K1_STAGE_C_LIVE_REPLAY_DESIGN.md` SS14 and
`.docs/DEVELOPMENT_PLAN_HISTORY.md` H-66 validated the required
architecture with a real `omp --mode rpc` process: one long-lived process
per baseline session, kept alive across every replayed turn, gets real
`cacheRead` pricing on every turn after the first (~12x cost reduction
vs. one process per turn). H-64 confirmed the safety mechanism -- a
`tools.approval.<tool>: deny` overlay (`laconic.k1corpus.deny_overlay`)
blocks a proposed tool call's real execution while the event stream
still surfaces the model's full proposed `{id, name, arguments}` before
the denial.

The exact wire shapes this module parses are drawn from the canonical
protocol reference (`docs/rpc.md`) and its typed reference implementation
(`python/omp-rpc/src/omp_rpc/protocol.py`) in the oh-my-pi source tree
checked out locally at this session's design gate (H-68), cross-checked
against H-64/H-66's own real-call findings:

- Startup: the process writes one `{"type": "ready", ...}` frame before
  processing any command (`docs/rpc.md` SS Transport and Framing). This
  module stays on protocol v1 (no `negotiate_protocol` handshake) --
  Stage C's tool-call/usage messages stay far below the v1 1 MiB
  per-frame ceiling, and H-64/H-66's real spikes drove the raw v1 wire
  format directly with no v2 negotiation.
- One `{"id": <uuid>, "type": "prompt", "message": <observation>}` line
  is written to stdin per replayed turn (`docs/rpc.md` SS Command Schema
  SS Prompting).
- The immediate `prompt` response is
  `{"id": ..., "type": "response", "command": "prompt", "success": true,
  "data": {"agentInvoked": true|false}}` (`docs/rpc.md` SS `prompt`
  payload) -- accepted, not completed; a later `agent_end` event marks
  the turn's actual completion.
- A `message_update` event whose `assistantMessageEvent.type ==
  "toolcall_end"` carries the full proposed action before any denial:
  `assistantMessageEvent.toolCall == {"id", "name", "arguments"}`
  (`omp_rpc.protocol.AssistantToolCallEndEvent`/`ToolCall`) -- this is
  `RecordedAction`'s source, matching H-64/H-66's own description
  verbatim.
- A `message_end` event carries the finalized assistant message,
  including `message.usage == {"input", "output", "cacheRead",
  "cacheWrite", "totalTokens", "cost": {...}}`
  (`omp_rpc.protocol.Usage`/`MessageEndEvent`) -- this is `TurnUsage`'s
  source.
- A `tool_execution_end` event
  (`{"toolCallId", "toolName", "result", "isError"}`,
  `omp_rpc.protocol.ToolExecutionEndEvent`) reports whether the proposed
  call actually ran. This module treats `isError is not True` here as a
  hard failure (`UnexpectedToolExecutionError`): Stage C's entire safety
  guarantee is that the deny-overlay blocks execution, and a silent pass
  here would mean a replayed session mutated something real.
- `agent_end` (`docs/rpc.md` SS Outbound frame categories) marks the
  turn complete for this module's purposes.

Every other stdout frame category (`available_commands_update`,
`extension_ui_request`, `subagent_*`, `command_output`, ...) is not part
of Stage C's contract and is skipped.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, Any, Protocol

from laconic.replay.engine import RecordedAction, ReplayTurn, ReplayTurnCapture, TurnUsage

#: Bounded ring buffer of the most recent stderr chunks, kept only to
#: enrich error messages -- never parsed, never a data source.
_STDERR_HISTORY_LINES = 200


class OmpRpcClientError(RuntimeError):
    """Base class for every `OmpRpcReplayClient` failure."""


class RpcProtocolError(OmpRpcClientError):
    """The child `omp --mode rpc` process emitted a line this client
    cannot safely interpret: malformed JSON, or a
    `message_update`/`message_end`/`tool_execution_end` event missing a
    field this client's contract requires. Raised instead of silently
    skipping or guessing, because dropping or fabricating a
    denied-but-proposed action here would corrupt K2 equivalence data
    downstream (design doc M1 "Risks & rollback")."""


class UnexpectedToolExecutionError(OmpRpcClientError):
    """Raised when a proposed tool call the deny-overlay was supposed to
    block was not, in fact, denied (`tool_execution_end.isError` was not
    `True`). Stage C's entire safety mechanism (design doc SS10.2) depends
    on this never happening silently."""


class NoActionTakenError(OmpRpcClientError):
    """Raised when a replayed turn's assistant message ended with no
    proposed tool-use action (the model answered in prose instead). Not
    representable as a `ReplayTurnCapture` -- the caller (batch
    orchestration) decides how to treat a turn the live model chose not
    to act on; this client never fabricates a placeholder action."""


class RpcProcessExitedError(OmpRpcClientError):
    """The `omp --mode rpc` child process exited before a turn completed."""


class RpcTimeoutError(OmpRpcClientError):
    """No event arrived from the child process within the configured
    per-read timeout."""


class CommandFactory(Protocol):
    """Build the `omp --mode rpc` argv for one session. Injectable so
    tests never construct a command that could resolve to a real `omp`
    binary."""

    def __call__(
        self, *, model: str, deny_overlay_path: Path, cwd: Path, session_dir: Path
    ) -> list[str]: ...


def default_command(
    *, model: str, deny_overlay_path: Path, cwd: Path, session_dir: Path
) -> list[str]:
    """Build the private, default-profile `omp --mode rpc` invocation."""
    return [
        "omp",
        "--mode",
        "rpc",
        "--model",
        model,
        "--config",
        str(deny_overlay_path),
        "--cwd",
        str(cwd),
        "--session-dir",
        str(session_dir),
        "--no-session",
        "--no-title",
    ]


class OmpRpcReplayClient:
    """`laconic.replay.engine.ReplayClient`: spawns one `omp --mode rpc`
    process on first use, kept alive for every subsequent `respond()`
    call, and torn down by :meth:`close` (or the context-manager
    protocol) once the session's replay completes -- design doc SS14's
    "one process per baseline session" contract. One instance serves
    exactly one session; construct a fresh instance per session.
    """

    def __init__(
        self,
        *,
        deny_overlay_path: Path,
        cwd: Path,
        session_dir: Path,
        command_factory: CommandFactory = default_command,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        timeout_s: float = 120.0,
    ) -> None:
        self._deny_overlay_path = deny_overlay_path
        self._cwd = cwd
        self._session_dir = session_dir
        self._command_factory = command_factory
        self._popen = popen
        self._timeout_s = timeout_s
        self._process: subprocess.Popen[str] | None = None
        self._model: str | None = None
        self._closed = False
        self._stderr_history: deque[str] = deque(maxlen=_STDERR_HISTORY_LINES)
        self._stderr_thread: threading.Thread | None = None
        self._stdout_queue: queue.Queue[str | None] = queue.Queue()
        self._stdout_thread: threading.Thread | None = None

    def __enter__(self) -> OmpRpcReplayClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def respond(
        self, *, prefix: Sequence[ReplayTurn], observation: str, model: str
    ) -> Sequence[ReplayTurnCapture]:
        if self._closed:
            raise OmpRpcClientError(
                "respond() called after close(): one client instance serves exactly one session"
            )
        if self._process is None:
            self._spawn(model)
        elif model != self._model:
            raise OmpRpcClientError(
                f"model changed mid-session ({self._model!r} -> {model!r}); one RPC "
                "process serves one model for its whole session-replay (design doc SS14)"
            )
        return self._send_prompt(observation)

    def preflight(self, model: str) -> None:
        """Start the RPC process and wait for ``ready`` without a prompt."""
        if self._closed:
            raise OmpRpcClientError("preflight() called after close()")
        if self._process is None:
            self._spawn(model)
        elif model != self._model:
            raise OmpRpcClientError(
                f"model changed mid-session ({self._model!r} -> {model!r}); one RPC "
                "process serves one model for its whole session-replay (design doc SS14)"
            )

    def close(self) -> None:
        """Close stdin and wait for exit (design doc SS14) -- idempotent."""
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=self._timeout_s)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=self._timeout_s)

    @property
    def recent_stderr(self) -> str:
        """The most recent stderr output, for error-message context only."""
        return "".join(self._stderr_history)

    def _spawn(self, model: str) -> None:
        command = self._command_factory(
            model=model,
            deny_overlay_path=self._deny_overlay_path,
            cwd=self._cwd,
            session_dir=self._session_dir,
        )
        process = self._popen(
            command,
            cwd=str(self._cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._process = process
        self._model = model
        if process.stderr is not None:
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr, args=(process.stderr,), daemon=True
            )
            self._stderr_thread.start()
        if process.stdout is not None:
            self._stdout_thread = threading.Thread(
                target=self._read_stdout, args=(process.stdout,), daemon=True
            )
            self._stdout_thread.start()
        ready = self._parse_json(self._readline())
        if ready.get("type") != "ready":
            raise RpcProtocolError(f"expected a `ready` frame first, got: {ready!r}")

    def _drain_stderr(self, stderr: Any) -> None:
        for line in stderr:
            self._stderr_history.append(line)

    def _read_stdout(self, stdout: IO[str]) -> None:
        # Runs on a background thread for this process's whole lifetime:
        # `TextIOWrapper.readline()` may read an OS pipe chunk containing
        # several JSONL lines at once, and a `selectors`-based readiness
        # check against the wrapped stream would then see no further OS
        # bytes pending and time out even though a full line is already
        # buffered client-side. Queuing decouples "a line is available"
        # from "the OS fd is currently readable".
        for line in stdout:
            self._stdout_queue.put(line)
        self._stdout_queue.put(None)

    def _readline(self) -> str:
        process = self._process
        assert process is not None
        try:
            line = self._stdout_queue.get(timeout=self._timeout_s)
        except queue.Empty as error:
            raise RpcTimeoutError(
                f"no event from omp --mode rpc within {self._timeout_s}s "
                f"(recent stderr: {self.recent_stderr!r})"
            ) from error
        if line is None:
            returncode = process.poll()
            raise RpcProcessExitedError(
                f"omp --mode rpc exited (code={returncode}) before the turn completed "
                f"(recent stderr: {self.recent_stderr!r})"
            )
        return line

    def _parse_json(self, line: str) -> dict[str, Any]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise RpcProtocolError(f"malformed JSONL line from omp --mode rpc: {line!r}") from error
        if not isinstance(payload, dict):
            raise RpcProtocolError(f"expected a JSON object line, got: {payload!r}")
        return payload

    def _send_prompt(self, observation: str) -> Sequence[ReplayTurnCapture]:
        process = self._process
        assert process is not None and process.stdin is not None
        request_id = uuid.uuid4().hex
        process.stdin.write(
            json.dumps({"id": request_id, "type": "prompt", "message": observation}) + "\n"
        )
        process.stdin.flush()
        return self._collect_turn(request_id)

    def _collect_turn(self, request_id: str) -> Sequence[ReplayTurnCapture]:
        prompt_acknowledged = False
        pending_actions: dict[str, RecordedAction] = {}
        verified_denials: set[str] = set()
        captures: list[tuple[str, ReplayTurnCapture]] = []
        while True:
            payload = self._parse_json(self._readline())
            event_type = payload.get("type")
            if event_type == "response" and payload.get("id") == request_id:
                prompt_acknowledged = self._handle_prompt_response(payload, request_id)
                continue
            if event_type == "message_update":
                action = self._parse_toolcall_end(payload)
                if action is not None:
                    pending_actions[action.tool_use_id] = action
                continue
            if event_type == "tool_execution_end":
                verified_denials.add(self._verify_denied(payload))
                continue
            if event_type == "message_end":
                capture = self._parse_message_end(payload, pending_actions)
                if capture is not None:
                    captures.append((capture.action.tool_use_id, capture))
                continue
            if event_type == "agent_end":
                return self._finish_turn(prompt_acknowledged, captures, verified_denials)
            # Every other outbound frame category (docs/rpc.md SS Outbound
            # frame categories: available_commands_update,
            # extension_ui_request, subagent_*, command_output, ...) is
            # not part of Stage C's contract.
            continue

    def _handle_prompt_response(self, payload: dict[str, Any], request_id: str) -> bool:
        if payload.get("command") != "prompt":
            raise RpcProtocolError(
                f"expected a `prompt` response for id {request_id!r}, got: {payload!r}"
            )
        if not payload.get("success", False):
            raise RpcProtocolError(f"omp --mode rpc rejected the prompt: {payload.get('error')!r}")
        data = payload.get("data")
        if isinstance(data, dict) and data.get("agentInvoked") is False:
            raise RpcProtocolError(
                "prompt completed without invoking the agent (data.agentInvoked: false); "
                "Stage C always expects a real model turn"
            )
        return True

    def _parse_toolcall_end(self, payload: dict[str, Any]) -> RecordedAction | None:
        event = payload.get("assistantMessageEvent")
        if not isinstance(event, dict) or event.get("type") != "toolcall_end":
            return None
        tool_call = event.get("toolCall")
        if not isinstance(tool_call, dict):
            raise RpcProtocolError(f"toolcall_end missing a toolCall object: {payload!r}")
        tool_use_id = tool_call.get("id")
        tool_name = tool_call.get("name")
        arguments = tool_call.get("arguments")
        if (
            not isinstance(tool_use_id, str)
            or not isinstance(tool_name, str)
            or not isinstance(arguments, dict)
        ):
            raise RpcProtocolError(f"toolcall_end has malformed id/name/arguments: {tool_call!r}")
        return RecordedAction(tool_use_id=tool_use_id, tool_name=tool_name, tool_input=arguments)

    def _verify_denied(self, payload: dict[str, Any]) -> str:
        if payload.get("isError") is not True:
            raise UnexpectedToolExecutionError(
                f"tool {payload.get('toolName')!r} (call {payload.get('toolCallId')!r}) executed "
                f"for real (tool_execution_end.isError={payload.get('isError')!r}); the "
                "deny-overlay failed to block it"
            )
        tool_call_id = payload.get("toolCallId")
        if not isinstance(tool_call_id, str):
            raise RpcProtocolError(f"tool_execution_end missing toolCallId: {payload!r}")
        return tool_call_id

    def _parse_message_end(
        self, payload: dict[str, Any], pending_actions: dict[str, RecordedAction]
    ) -> ReplayTurnCapture | None:
        message = payload.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return None
        content = message.get("content")
        if not isinstance(content, list):
            raise RpcProtocolError(f"message_end.message.content missing or malformed: {payload!r}")
        tool_calls = [
            item for item in content if isinstance(item, dict) and item.get("type") == "toolCall"
        ]
        if not tool_calls:
            raise NoActionTakenError(
                "assistant message ended with no proposed tool-use action; the replayed "
                "model answered in prose instead of proposing a comparable action"
            )
        if len(tool_calls) > 1:
            raise RpcProtocolError(
                f"assistant message proposed {len(tool_calls)} parallel tool calls; this "
                "client only supports one action per replayed turn"
            )
        tool_use_id = tool_calls[0].get("id")
        if not isinstance(tool_use_id, str) or tool_use_id not in pending_actions:
            raise RpcProtocolError(
                f"message_end tool_use id {tool_use_id!r} was never seen in a toolcall_end event"
            )
        action = pending_actions.pop(tool_use_id)
        return ReplayTurnCapture(action=action, usage=self._parse_usage(message))

    def _parse_usage(self, message: dict[str, Any]) -> TurnUsage:
        model = message.get("model")
        usage_payload = message.get("usage")
        if not isinstance(model, str) or not isinstance(usage_payload, dict):
            raise RpcProtocolError(f"message_end.message missing model/usage: {message!r}")
        try:
            return TurnUsage(
                model=model,
                input_tokens=int(usage_payload["input"]),
                cache_read=int(usage_payload.get("cacheRead", 0)),
                cache_write=int(usage_payload.get("cacheWrite", 0)),
                output_tokens=int(usage_payload["output"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RpcProtocolError(
                f"message_end.message.usage malformed: {usage_payload!r}"
            ) from error

    def _finish_turn(
        self,
        prompt_acknowledged: bool,
        captures: list[tuple[str, ReplayTurnCapture]],
        verified_denials: set[str],
    ) -> Sequence[ReplayTurnCapture]:
        if not prompt_acknowledged:
            raise RpcProtocolError("agent_end observed before the prompt was acknowledged")
        if not captures:
            raise RpcProtocolError(
                "agent_end observed with no captured tool_use action for this turn"
            )
        unverified = [
            tool_use_id for tool_use_id, _ in captures if tool_use_id not in verified_denials
        ]
        if unverified:
            raise RpcProtocolError(
                f"turn ended with no tool_execution_end denial observed for: {unverified}; "
                "cannot confirm the deny-overlay actually blocked these proposed actions"
            )
        return tuple(capture for _, capture in captures)
