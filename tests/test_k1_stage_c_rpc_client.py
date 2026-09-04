"""Tests for `laconic_stage_c.rpc_client.OmpRpcReplayClient` (K1 Stage C M1).

Every test drives a *fake* subprocess: a tiny, committed Python script
(`_FAKE_SERVER_SCRIPT`) launched via `sys.executable`, scripted per test
through a JSON scenario file. `omp` (the real binary) is never invoked,
matching this module's own constraint (design doc M1 "CONSTRAINTS: zero
real `omp` invocation in any test").

The JSONL event shapes each scenario emits (`ready`, the `prompt`
response, `message_update`/`toolcall_end`, `tool_execution_end`,
`message_end`, `agent_end`) reproduce the exact wire shapes
`rpc_client.py`'s own module docstring cites from `docs/rpc.md` and
`omp_rpc.protocol`, matching H-64/H-66's real findings -- not an
approximation.
"""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import laconic_stage_c
from laconic_stage_c import factory, preflight
from laconic_stage_c.rpc_client import (
    NoActionTakenError,
    OmpRpcClientError,
    OmpRpcReplayClient,
    RpcProcessExitedError,
    RpcProtocolError,
    RpcTimeoutError,
    UnexpectedToolExecutionError,
    default_command,
)

#: A fake `omp --mode rpc` stand-in: emits a `ready` frame, then for each
#: `prompt` command read from stdin, acknowledges it and emits the next
#: scripted turn's events in order. Never touches a real `omp` binary.
_FAKE_SERVER_SCRIPT = """
import json
import sys
import time

def emit(obj):
    if isinstance(obj, str):
        sys.stdout.write(obj + "\\n")
    else:
        sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

def main():
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        scenario = json.load(handle)
    emit(scenario.get("ready", {"type": "ready", "protocolVersion": 1}))
    command_log = scenario.get("command_log")
    commands = []
    turns = scenario.get("turns", [])
    turn_index = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        command = json.loads(line)
        commands.append(command)
        if command.get("type") != "prompt":
            continue
        if turn_index >= len(turns):
            if scenario.get("on_exhausted") == "hang":
                time.sleep(3600)
            break
        turn = turns[turn_index]
        turn_index += 1
        ack = {"command": "prompt", "success": True, "data": {"agentInvoked": True}}
        ack.update(turn.get("ack", {}))
        ack["type"] = "response"
        ack["id"] = command.get("id")
        emit(ack)
        for event in turn.get("events", []):
            emit(event)
        if turn.get("hang_after_events", False):
            time.sleep(3600)
        if turn.get("exit_after", False):
            sys.exit(turn.get("exit_code", 0))
    if command_log is not None:
        with open(command_log, "w", encoding="utf-8") as handle:
            json.dump(commands, handle)
    sys.exit(0)

if __name__ == "__main__":
    main()
"""


def _toolcall_end_event(
    *, tool_use_id: str, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    return {
        "type": "message_update",
        "message": {"role": "assistant"},
        "assistantMessageEvent": {
            "type": "toolcall_end",
            "contentIndex": 0,
            "toolCall": {
                "type": "toolCall",
                "id": tool_use_id,
                "name": tool_name,
                "arguments": arguments,
            },
            "partial": {"role": "assistant"},
        },
    }


def _tool_execution_end_event(
    *,
    tool_use_id: str,
    tool_name: str,
    is_error: bool = True,
    result: str = "blocked by user policy",
) -> dict[str, Any]:
    return {
        "type": "tool_execution_end",
        "toolCallId": tool_use_id,
        "toolName": tool_name,
        "result": result,
        "isError": is_error,
    }


def _message_end_event(
    *,
    tool_use_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> dict[str, Any]:
    return {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "model": model,
            "content": [
                {"type": "toolCall", "id": tool_use_id, "name": tool_name, "arguments": arguments}
            ],
            "usage": {
                "input": input_tokens,
                "output": output_tokens,
                "cacheRead": cache_read,
                "cacheWrite": cache_write,
                "totalTokens": input_tokens + output_tokens + cache_read + cache_write,
                "cost": {
                    "input": 0.0,
                    "output": 0.0,
                    "cacheRead": 0.0,
                    "cacheWrite": 0.0,
                    "total": 0.0,
                },
            },
        },
    }


def _text_message_end_event(*, model: str, text: str = "here is my answer") -> dict[str, Any]:
    return {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {"input": 10, "output": 5, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 15},
        },
    }


def _agent_end_event() -> dict[str, Any]:
    return {"type": "agent_end", "messages": []}


def _denied_write_turn(
    *, tool_use_id: str = "call_1", cache_read: int = 0, cache_write: int = 57739
) -> dict[str, Any]:
    arguments = {"path": "test.txt", "content": "hello"}
    return {
        "events": [
            _toolcall_end_event(tool_use_id=tool_use_id, tool_name="write", arguments=arguments),
            _message_end_event(
                tool_use_id=tool_use_id,
                tool_name="write",
                arguments=arguments,
                model="anthropic/claude-haiku-4-5",
                input_tokens=120,
                output_tokens=40,
                cache_read=cache_read,
                cache_write=cache_write,
            ),
            _tool_execution_end_event(tool_use_id=tool_use_id, tool_name="write"),
            _agent_end_event(),
        ]
    }


@pytest.fixture
def fake_server_script(tmp_path: Path) -> Path:
    script_path = tmp_path / "fake_omp_rpc.py"
    script_path.write_text(_FAKE_SERVER_SCRIPT, encoding="utf-8")
    return script_path


def _write_scenario(tmp_path: Path, scenario: dict[str, Any], name: str = "scenario.json") -> Path:
    scenario_path = tmp_path / name
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    return scenario_path


def _fake_command_factory(script_path: Path, scenario_path: Path) -> Callable[..., list[str]]:
    def factory(
        *,
        model: str,
        deny_overlay_path: Path,
        cwd: Path,
        session_dir: Path,
    ) -> list[str]:
        return [sys.executable, str(script_path), str(scenario_path)]

    return factory


def _make_client(
    tmp_path: Path,
    fake_server_script: Path,
    scenario: dict[str, Any],
    *,
    timeout_s: float = 5.0,
) -> OmpRpcReplayClient:
    scenario_path = _write_scenario(tmp_path, scenario)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    session_dir = tmp_path / "omp-sessions"
    session_dir.mkdir()
    return OmpRpcReplayClient(
        deny_overlay_path=tmp_path / "deny-overlay.yml",
        cwd=sandbox,
        session_dir=session_dir,
        command_factory=_fake_command_factory(fake_server_script, scenario_path),
        timeout_s=timeout_s,
    )


def test_respond_parses_a_clean_denied_tool_call_turn(
    tmp_path: Path, fake_server_script: Path
) -> None:
    client = _make_client(tmp_path, fake_server_script, {"turns": [_denied_write_turn()]})
    with client:
        captures = client.respond(
            prefix=(), observation="please write test.txt", model="anthropic/claude-haiku-4-5"
        )
    assert len(captures) == 1
    capture = captures[0]
    assert capture.action.tool_use_id == "call_1"
    assert capture.action.tool_name == "write"
    assert capture.action.tool_input == {"path": "test.txt", "content": "hello"}
    assert capture.usage.model == "anthropic/claude-haiku-4-5"
    assert capture.usage.input_tokens == 120
    assert capture.usage.output_tokens == 40
    assert capture.usage.cache_write == 57739
    assert capture.usage.cache_read == 0


def test_respond_parses_cache_read_shaped_usage_on_turn_two(
    tmp_path: Path, fake_server_script: Path
) -> None:
    """H-66's real spike: turn 1 pays a cold-cache `cacheWrite`; turn 2 on
    the *same* process hits `cacheRead` instead -- a ~12x cost reduction.
    This test proves the client's parsing (not the real cost) is correct
    across repeat `respond()` calls on one persistent process."""
    turn_one = _denied_write_turn(tool_use_id="call_1", cache_read=0, cache_write=57739)
    turn_two = {
        "events": [
            _toolcall_end_event(
                tool_use_id="call_2", tool_name="bash", arguments={"command": "ls -la"}
            ),
            _message_end_event(
                tool_use_id="call_2",
                tool_name="bash",
                arguments={"command": "ls -la"},
                model="anthropic/claude-haiku-4-5",
                input_tokens=15,
                output_tokens=20,
                cache_read=57975,
                cache_write=127,
            ),
            _tool_execution_end_event(tool_use_id="call_2", tool_name="bash"),
            _agent_end_event(),
        ]
    }
    client = _make_client(tmp_path, fake_server_script, {"turns": [turn_one, turn_two]})
    with client:
        first = client.respond(
            prefix=(), observation="write it", model="anthropic/claude-haiku-4-5"
        )
        second = client.respond(
            prefix=tuple(), observation="now list the dir", model="anthropic/claude-haiku-4-5"
        )
    assert first[0].usage.cache_write == 57739
    assert first[0].usage.cache_read == 0
    assert second[0].action.tool_name == "bash"
    assert second[0].usage.cache_read == 57975
    assert second[0].usage.cache_write == 127


def test_malformed_jsonl_line_raises(tmp_path: Path, fake_server_script: Path) -> None:
    turn = {"events": ["not valid json {"]}
    client = _make_client(tmp_path, fake_server_script, {"turns": [turn]})
    with client, pytest.raises(RpcProtocolError, match="malformed JSONL line"):
        client.respond(prefix=(), observation="obs", model="m")


def test_process_exit_before_turn_completes_raises(
    tmp_path: Path, fake_server_script: Path
) -> None:
    turn = {
        "events": [
            _toolcall_end_event(tool_use_id="call_1", tool_name="write", arguments={"path": "a"})
        ],
        "exit_after": True,
        "exit_code": 1,
    }
    client = _make_client(tmp_path, fake_server_script, {"turns": [turn]})
    with client, pytest.raises(RpcProcessExitedError):
        client.respond(prefix=(), observation="obs", model="m")


def test_timeout_when_no_event_arrives(tmp_path: Path, fake_server_script: Path) -> None:
    turn = {
        "events": [
            _toolcall_end_event(tool_use_id="call_1", tool_name="write", arguments={"path": "a"})
        ],
        "hang_after_events": True,
    }
    client = _make_client(tmp_path, fake_server_script, {"turns": [turn]}, timeout_s=0.3)
    with pytest.raises(RpcTimeoutError):
        client.respond(prefix=(), observation="obs", model="m")
    client.close()


def test_unexpected_tool_execution_not_denied_raises(
    tmp_path: Path, fake_server_script: Path
) -> None:
    turn = {
        "events": [
            _toolcall_end_event(tool_use_id="call_1", tool_name="write", arguments={"path": "a"}),
            _message_end_event(
                tool_use_id="call_1",
                tool_name="write",
                arguments={"path": "a"},
                model="m",
                input_tokens=1,
                output_tokens=1,
            ),
            _tool_execution_end_event(
                tool_use_id="call_1", tool_name="write", is_error=False, result="wrote file"
            ),
            _agent_end_event(),
        ]
    }
    client = _make_client(tmp_path, fake_server_script, {"turns": [turn]})
    with client, pytest.raises(UnexpectedToolExecutionError, match="executed for real"):
        client.respond(prefix=(), observation="obs", model="m")


def test_missing_denial_verification_raises(tmp_path: Path, fake_server_script: Path) -> None:
    """A capture with no matching `tool_execution_end` at all is exactly
    as dangerous as one that executed for real: this client cannot
    confirm the deny-overlay actually blocked it."""
    turn = {
        "events": [
            _toolcall_end_event(tool_use_id="call_1", tool_name="write", arguments={"path": "a"}),
            _message_end_event(
                tool_use_id="call_1",
                tool_name="write",
                arguments={"path": "a"},
                model="m",
                input_tokens=1,
                output_tokens=1,
            ),
            _agent_end_event(),
        ]
    }
    client = _make_client(tmp_path, fake_server_script, {"turns": [turn]})
    with client, pytest.raises(RpcProtocolError, match="no tool_execution_end denial observed"):
        client.respond(prefix=(), observation="obs", model="m")


def test_no_action_taken_raises(tmp_path: Path, fake_server_script: Path) -> None:
    turn = {"events": [_text_message_end_event(model="m"), _agent_end_event()]}
    client = _make_client(tmp_path, fake_server_script, {"turns": [turn]})
    with client, pytest.raises(NoActionTakenError):
        client.respond(prefix=(), observation="obs", model="m")


def test_prompt_not_invoking_the_agent_raises(tmp_path: Path, fake_server_script: Path) -> None:
    turn = {
        "ack": {"success": True, "data": {"agentInvoked": False}},
        "events": [_agent_end_event()],
    }
    client = _make_client(tmp_path, fake_server_script, {"turns": [turn]})
    with client, pytest.raises(RpcProtocolError, match="agentInvoked: false"):
        client.respond(prefix=(), observation="obs", model="m")


def test_rejected_prompt_raises(tmp_path: Path, fake_server_script: Path) -> None:
    turn = {"ack": {"success": False, "error": "no active session"}, "events": []}
    client = _make_client(tmp_path, fake_server_script, {"turns": [turn]})
    with client, pytest.raises(RpcProtocolError, match="rejected the prompt"):
        client.respond(prefix=(), observation="obs", model="m")


def test_close_closes_stdin_and_waits_for_exit(tmp_path: Path, fake_server_script: Path) -> None:
    client = _make_client(tmp_path, fake_server_script, {"turns": [_denied_write_turn()]})
    client.respond(prefix=(), observation="obs", model="m")
    client.close()
    assert client._process is not None
    assert client._process.returncode == 0


def test_respond_after_close_raises(tmp_path: Path, fake_server_script: Path) -> None:
    client = _make_client(
        tmp_path, fake_server_script, {"turns": [_denied_write_turn(), _denied_write_turn()]}
    )
    client.respond(prefix=(), observation="obs", model="m")
    client.close()
    with pytest.raises(OmpRpcClientError, match="after close"):
        client.respond(prefix=(), observation="obs again", model="m")


def test_model_change_mid_session_raises(tmp_path: Path, fake_server_script: Path) -> None:
    client = _make_client(
        tmp_path, fake_server_script, {"turns": [_denied_write_turn(), _denied_write_turn()]}
    )
    with client:
        client.respond(prefix=(), observation="obs", model="model-a")
        with pytest.raises(OmpRpcClientError, match="model changed mid-session"):
            client.respond(prefix=(), observation="obs 2", model="model-b")


def test_preflight_waits_for_ready_without_sending_a_provider_prompt(
    tmp_path: Path, fake_server_script: Path
) -> None:
    command_log = tmp_path / "commands.json"
    client = _make_client(
        tmp_path,
        fake_server_script,
        {"command_log": str(command_log)},
    )

    client.preflight(model="openai-codex/gpt-5.6")
    client.close()

    assert json.loads(command_log.read_text(encoding="utf-8")) == []


def test_factory_creates_private_stage_c_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    client = factory()

    root = tmp_path / ".laconic/k1/stage_c"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "sandbox").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "omp-sessions").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "deny-overlay.yml").stat().st_mode) == 0o600
    assert list((root / "sandbox").iterdir()) == []
    assert client._deny_overlay_path == root / "deny-overlay.yml"


def test_public_preflight_uses_factory_and_closes_client(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeClient:
        def preflight(self, model: str) -> None:
            calls.append(f"preflight:{model}")

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(laconic_stage_c, "factory", FakeClient)

    preflight(model="openai-codex/gpt-5.6")

    assert calls == ["preflight:openai-codex/gpt-5.6", "close"]


def test_default_command_uses_private_paths_without_profile() -> None:
    """Documents (without invoking) the real client's argv -- every fake
    subprocess test overrides `command_factory`, so no test can run `omp`."""
    command = default_command(
        model="anthropic/claude-haiku-4-5",
        deny_overlay_path=Path("overlay.yml"),
        cwd=Path("sandbox"),
        session_dir=Path("omp-sessions"),
    )

    assert command == [
        "omp",
        "--mode",
        "rpc",
        "--model",
        "anthropic/claude-haiku-4-5",
        "--config",
        "overlay.yml",
        "--cwd",
        "sandbox",
        "--session-dir",
        "omp-sessions",
        "--no-session",
        "--no-title",
    ]
