"""Bounded Observe subprocess entrypoint: silent, always-exit-0 behavior.

Every payload is a fixture piped over stdin to a real subprocess (or the
in-process `main`); none is a real client hook invocation. Output-byte
assertions check the literal subprocess stdout, not a parsed structure,
because the design's invariant is "no bytes," not "no unexpected bytes."
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from laconic.observe.audit import read_chain, verify_chain
from laconic.observe.entrypoint import main
from laconic.observe.privacy import validate_receipt_json

_VALID_CLAUDE_CODE_PAYLOAD = {
    "session_id": "abc123",
    "hook_event_name": "PostToolUse",
    "tool_name": "Read",
    "tool_input": {"file_path": "/x"},
    "tool_response": {"success": True},
}

_VALID_OMP_PAYLOAD = {
    "session_id": "abc123",
    "event": "tool_result",
    "toolName": "read",
    "isError": False,
}


def _run_subprocess(
    *, client: str, stdin_text: str, env: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "laconic.observe.entrypoint", "--client", client],
        input=stdin_text.encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=10,
    )


def _env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["LACONIC_OBSERVE_AUDIT_PATH"] = str(tmp_path / "audit.jsonl")
    return env


def test_valid_payload_exits_zero_with_empty_stdout(tmp_path: Path) -> None:
    result = _run_subprocess(
        client="claude-code", stdin_text=json.dumps(_VALID_CLAUDE_CODE_PAYLOAD), env=_env(tmp_path)
    )
    assert result.returncode == 0
    assert result.stdout == b""


def test_valid_payload_writes_exactly_one_verifiable_receipt(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    env = dict(**_env(tmp_path))
    _run_subprocess(
        client="claude-code", stdin_text=json.dumps(_VALID_CLAUDE_CODE_PAYLOAD), env=env
    )
    chain = read_chain(audit_path)
    assert len(chain) == 1
    verify_chain(chain)
    validate_receipt_json(chain[0].receipt)


def test_malformed_json_exits_zero_with_empty_stdout(tmp_path: Path) -> None:
    result = _run_subprocess(client="claude-code", stdin_text="not json{{{", env=_env(tmp_path))
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr != b""


def test_unsupported_event_exits_zero_with_empty_stdout(tmp_path: Path) -> None:
    payload = {**_VALID_CLAUDE_CODE_PAYLOAD, "hook_event_name": "PreToolUse"}
    result = _run_subprocess(
        client="claude-code", stdin_text=json.dumps(payload), env=_env(tmp_path)
    )
    assert result.returncode == 0
    assert result.stdout == b""


def test_empty_stdin_exits_zero_with_empty_stdout(tmp_path: Path) -> None:
    result = _run_subprocess(client="claude-code", stdin_text="", env=_env(tmp_path))
    assert result.returncode == 0
    assert result.stdout == b""


def test_json_array_instead_of_object_exits_zero_with_empty_stdout(tmp_path: Path) -> None:
    result = _run_subprocess(client="claude-code", stdin_text="[1, 2, 3]", env=_env(tmp_path))
    assert result.returncode == 0
    assert result.stdout == b""


def test_unwritable_audit_directory_exits_zero_with_empty_stdout(tmp_path: Path) -> None:
    """A storage failure -- here, a parent path that is actually a file,
    so `mkdir` cannot create the audit directory -- must not surface any
    output or a non-zero exit."""
    blocking_file = tmp_path / "blocked"
    blocking_file.write_text("not a directory")

    env = dict(os.environ)
    env["LACONIC_OBSERVE_AUDIT_PATH"] = str(blocking_file / "nested" / "audit.jsonl")
    result = _run_subprocess(
        client="claude-code", stdin_text=json.dumps(_VALID_CLAUDE_CODE_PAYLOAD), env=env
    )
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr != b""


def test_omp_valid_payload_exits_zero_with_empty_stdout(tmp_path: Path) -> None:
    result = _run_subprocess(
        client="omp", stdin_text=json.dumps(_VALID_OMP_PAYLOAD), env=_env(tmp_path)
    )
    assert result.returncode == 0
    assert result.stdout == b""


def test_completes_well_within_a_bounded_wall_clock_budget(tmp_path: Path) -> None:
    """Guards against an accidental blocking call (network, unbounded
    retry) -- this program does one local file append and nothing else."""
    started = time.monotonic()
    _run_subprocess(
        client="claude-code", stdin_text=json.dumps(_VALID_CLAUDE_CODE_PAYLOAD), env=_env(tmp_path)
    )
    assert time.monotonic() - started < 5.0


def test_main_in_process_never_raises_on_malformed_stdin() -> None:
    exit_code = main(["--client", "claude-code"], stdin=io.StringIO("garbage"))
    assert exit_code == 0


def test_main_in_process_returns_zero_on_missing_required_argument() -> None:
    """`--client` is required; a missing argument makes argparse raise
    `SystemExit`, which must still resolve to a silent 0, not propagate."""
    exit_code = main([], stdin=io.StringIO("{}"))
    assert exit_code == 0


def test_main_in_process_returns_zero_on_unknown_client_value() -> None:
    exit_code = main(["--client", "cursor"], stdin=io.StringIO("{}"))
    assert exit_code == 0
