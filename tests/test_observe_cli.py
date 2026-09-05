"""End-to-end `laconic diagnostics observe` CLI tests.

Every test runs in an isolated `tmp_path`: project scope goes through
`monkeypatch.chdir`, user scope always passes an explicit `--user-dir`
override. No test ever resolves a real `~/.claude` or `~/.omp` path, and
no test invokes a real client hook -- these tests only prove the CLI
itself works, safely, against synthetic locations.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from laconic.cli import (
    EXIT_OBSERVE_CONFIG_PARSE_ERROR,
    EXIT_OBSERVE_OWNERSHIP_CONFLICT,
    EXIT_OK,
    main,
)
from laconic.observe.audit import append_to_file
from laconic.observe.preview import CLAUDE_CODE_OWNED_MARKER, OMP_OWNED_MARKER


def test_install_dry_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    exit_code = main(["diagnostics", "observe", "install", "--client", "claude-code", "--dry-run"])
    assert exit_code == EXIT_OK
    assert not (tmp_path / ".claude").exists()
    out = capsys.readouterr().out
    assert "preview" in out
    assert "[add]" in out


def test_install_claude_code_project_scope_writes_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    exit_code = main(["diagnostics", "observe", "install", "--client", "claude-code"])
    assert exit_code == EXIT_OK
    settings_path = tmp_path / ".claude" / "settings.json"
    assert settings_path.exists()
    data = json.loads(settings_path.read_text())
    assert CLAUDE_CODE_OWNED_MARKER in json.dumps(data)


def test_install_is_idempotent_over_two_cli_invocations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    main(["diagnostics", "observe", "install", "--client", "claude-code"])
    settings_path = tmp_path / ".claude" / "settings.json"
    first_bytes = settings_path.read_bytes()
    main(["diagnostics", "observe", "install", "--client", "claude-code"])
    assert settings_path.read_bytes() == first_bytes


def test_install_then_remove_round_trips_claude_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    main(["diagnostics", "observe", "install", "--client", "claude-code"])
    exit_code = main(["diagnostics", "observe", "remove", "--client", "claude-code"])
    assert exit_code == EXIT_OK
    settings_path = tmp_path / ".claude" / "settings.json"
    assert settings_path.exists()
    data = json.loads(settings_path.read_text())
    assert CLAUDE_CODE_OWNED_MARKER not in json.dumps(data)


def test_install_omp_project_scope_writes_extension_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    exit_code = main(["diagnostics", "observe", "install", "--client", "omp"])
    assert exit_code == EXIT_OK
    target = tmp_path / ".omp" / "extensions" / "laconic-observe.ts"
    assert target.exists()
    assert OMP_OWNED_MARKER in target.read_text(encoding="utf-8")


def test_install_user_scope_uses_explicit_user_dir_override(tmp_path: Path) -> None:
    """User scope must go exactly where `--user-dir` points, never to a
    real home directory this test process happens to run under."""
    user_dir = tmp_path / "fake-home" / ".omp" / "agent" / "extensions"
    exit_code = main(
        [
            "diagnostics",
            "observe",
            "install",
            "--client",
            "omp",
            "--scope",
            "user",
            "--user-dir",
            str(user_dir),
        ]
    )
    assert exit_code == EXIT_OK
    assert (user_dir / "laconic-observe.ts").exists()


def test_install_ownership_conflict_returns_dedicated_exit_code_and_does_not_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    conflict_dir = tmp_path / ".omp" / "extensions"
    conflict_dir.mkdir(parents=True)
    (conflict_dir / "laconic-observe.ts").write_text("// not ours", encoding="utf-8")
    exit_code = main(["diagnostics", "observe", "install", "--client", "omp"])
    assert exit_code == EXIT_OBSERVE_OWNERSHIP_CONFLICT
    assert (conflict_dir / "laconic-observe.ts").read_text(encoding="utf-8") == "// not ours"


def test_install_malformed_settings_returns_dedicated_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text("not json {{{", encoding="utf-8")
    exit_code = main(["diagnostics", "observe", "install", "--client", "claude-code"])
    assert exit_code == EXIT_OBSERVE_CONFIG_PARSE_ERROR
    assert (settings_dir / "settings.json").read_text(encoding="utf-8") == "not json {{{"


def test_status_json_reflects_written_receipts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    append_to_file(audit_path, {"adapter": "claude-code"})
    exit_code = main(
        ["diagnostics", "observe", "status", "--audit-path", str(audit_path), "--format", "json"]
    )
    assert exit_code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["entry_count"] == 1
    assert payload["chain_valid"] is True


def test_report_json_breaks_down_by_adapter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    append_to_file(audit_path, {"adapter": "claude-code"})
    append_to_file(audit_path, {"adapter": "omp"})
    exit_code = main(
        ["diagnostics", "observe", "report", "--audit-path", str(audit_path), "--format", "json"]
    )
    assert exit_code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["by_adapter"] == {"claude-code": 1, "omp": 1}


def test_status_on_a_project_with_no_receipts_yet_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    exit_code = main(["diagnostics", "observe", "status"])
    assert exit_code == EXIT_OK
    out = capsys.readouterr().out
    assert "exists: False" in out
    assert "entries: 0" in out


def test_observe_help_lists_all_four_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["diagnostics", "observe", "--help"])
    out = capsys.readouterr().out
    for name in ("install", "remove", "status", "report"):
        assert name in out
