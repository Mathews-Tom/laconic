from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from laconic.cli import EXIT_OK, EXIT_OMP_INSTALL_ERROR, EXIT_RUNTIME_REFERENCE_ERROR, main
from laconic.ledger import ObservationKind
from laconic.runtime.omp_installer import (
    OBSERVE_OWNED_MARKER,
    OMP_EXTENSION_FILENAME,
    OMP_OWNED_MARKER,
)
from laconic.runtime.storage import RuntimeStorage


def test_install_omp_dry_run_defaults_to_project_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "private-ledgers"

    exit_code = main(
        [
            "install",
            "omp",
            "--dry-run",
            "--format",
            "json",
            "--python",
            sys.executable,
            "--data-dir",
            str(data_dir),
        ]
    )

    assert exit_code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "adapter": "omp",
        "applied": False,
        "data_directory": str(data_dir),
        "entrypoint": ["-I", "-m", "laconic.runtime"],
        "operation": "add",
        "path": str(tmp_path / ".omp" / "extensions" / OMP_EXTENSION_FILENAME),
        "preserved": [],
        "preview": True,
        "python": os.path.abspath(sys.executable),
    }
    assert not (tmp_path / ".omp").exists()
    assert not data_dir.exists()


def test_install_and_uninstall_omp_round_trip_only_owned_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "ledgers"
    data_dir.mkdir()
    sentinel = data_dir / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")

    install_args = [
        "install",
        "omp",
        "--python",
        sys.executable,
        "--data-dir",
        str(data_dir),
        "--format",
        "json",
    ]
    assert main(install_args) == EXIT_OK
    installed = json.loads(capsys.readouterr().out)
    target = Path(installed["path"])
    assert OMP_OWNED_MARKER in target.read_text(encoding="utf-8")

    assert main(install_args) == EXIT_OK
    unchanged = json.loads(capsys.readouterr().out)
    assert unchanged["operation"] == "none"
    assert unchanged["applied"] is False

    assert main(["uninstall", "omp", "--format", "json"]) == EXIT_OK
    removed = json.loads(capsys.readouterr().out)
    assert removed["operation"] == "remove"
    assert removed["applied"] is True
    assert not target.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_install_omp_refuses_foreign_target_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    extension_dir = tmp_path / ".omp" / "extensions"
    extension_dir.mkdir(parents=True)
    target = extension_dir / OMP_EXTENSION_FILENAME
    target.write_text("// foreign\n", encoding="utf-8")

    assert main(["install", "omp", "--python", sys.executable]) == EXIT_OMP_INSTALL_ERROR

    result = capsys.readouterr()
    assert result.out == ""
    assert "not Laconic-owned" in result.err
    assert target.read_text(encoding="utf-8") == "// foreign\n"


def test_install_omp_refuses_existing_observe_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    extension_dir = tmp_path / ".omp" / "extensions"
    extension_dir.mkdir(parents=True)
    (extension_dir / "laconic-observe.ts").write_text(
        f"{OBSERVE_OWNED_MARKER}\n",
        encoding="utf-8",
    )

    assert main(["install", "omp", "--python", sys.executable]) == EXIT_OMP_INSTALL_ERROR
    assert "another Laconic OMP adapter" in capsys.readouterr().err
    assert not (extension_dir / OMP_EXTENSION_FILENAME).exists()


def test_user_profile_matches_omp_native_profile_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PI_CONFIG_DIR", ".custom-omp")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "ignored"))

    assert (
        main(
            [
                "install",
                "omp",
                "--scope",
                "user",
                "--profile",
                "work",
                "--dry-run",
                "--format",
                "json",
                "--python",
                sys.executable,
            ]
        )
        == EXIT_OK
    )

    payload = json.loads(capsys.readouterr().out)
    assert Path(payload["path"]) == (
        tmp_path
        / ".custom-omp"
        / "profiles"
        / "work"
        / "agent"
        / "extensions"
        / OMP_EXTENSION_FILENAME
    )


def test_project_scope_rejects_user_target_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["install", "omp", "--profile", "work"]) == EXIT_OMP_INSTALL_ERROR

    assert "require --scope user" in capsys.readouterr().err
    assert not (tmp_path / ".omp").exists()


def test_status_reports_adapter_and_storage_without_reading_observation_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "ledgers"
    storage = RuntimeStorage(data_dir)
    with storage.open_ledger("session-a") as ledger:
        ledger.record_runtime_decision(
            sequence=0,
            request_id="request-a",
            tool_name="Read",
            outcome="pass_through",
            reason="not_smaller",
            candidate_reference=None,
            raw_chars=12,
            visible_chars=12,
            latency_ms=1,
        )
    assert (
        main(
            [
                "install",
                "omp",
                "--python",
                sys.executable,
                "--data-dir",
                str(data_dir),
            ]
        )
        == EXIT_OK
    )
    capsys.readouterr()

    assert (
        main(
            [
                "status",
                "--user-dir",
                str(tmp_path / "fake-user-extensions"),
                "--data-dir",
                str(data_dir),
                "--format",
                "json",
            ]
        )
        == EXIT_OK
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["project_adapter"] == "installed"
    assert payload["user_adapter"] == "not_installed"
    assert payload["engine_health"] == "active-session-only; use /laconic status in OMP"
    assert payload["storage"]["sessions"] == 1
    assert payload["storage"]["eligible_observations"] == 1
    assert payload["storage"]["compressed_observations"] == 0
    assert payload["storage"]["pass_through_observations"] == 1


def test_status_on_absent_storage_does_not_create_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "absent"

    assert (
        main(
            [
                "status",
                "--user-dir",
                str(tmp_path / "fake-user-extensions"),
                "--data-dir",
                str(data_dir),
                "--format",
                "json",
            ]
        )
        == EXIT_OK
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["storage"]["exists"] is False
    assert payload["storage"]["sessions"] == 0
    assert not data_dir.exists()


def test_expand_recovers_exact_runtime_content_and_spans(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    raw = "alpha\nbeta\ngamma"
    with storage.open_ledger("session-a") as ledger:
        ledger.register(ObservationKind.FILE, "src/example.py", raw, "outline", 1)

    assert main(["expand", "session-a/F1", "--data-dir", str(storage.root)]) == EXIT_OK
    assert capsys.readouterr().out == raw

    assert main(["expand", "session-a/F1:2-3", "--data-dir", str(storage.root)]) == EXIT_OK
    assert capsys.readouterr().out == "beta\ngamma"


@pytest.mark.parametrize(
    ("reference", "error"),
    [
        ("not-a-reference", "invalid runtime reference"),
        ("missing/F1", "runtime session does not exist"),
        ("session-a/F2", "runtime handle does not exist"),
        ("session-a/F1:1-3", "invalid runtime span"),
    ],
)
def test_expand_rejects_invalid_or_unknown_runtime_references(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    reference: str,
    error: str,
) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    with storage.open_ledger("session-a") as ledger:
        ledger.register(ObservationKind.FILE, "src/example.py", "alpha\nbeta", "outline", 1)

    assert (
        main(["expand", reference, "--data-dir", str(storage.root)]) == EXIT_RUNTIME_REFERENCE_ERROR
    )
    result = capsys.readouterr()
    assert result.out == ""
    assert error in result.err


def test_expand_on_absent_storage_fails_without_creating_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / "absent"

    assert (
        main(["expand", "session-a/F1", "--data-dir", str(data_dir)])
        == EXIT_RUNTIME_REFERENCE_ERROR
    )
    assert "runtime session does not exist" in capsys.readouterr().err
    assert not data_dir.exists()


@pytest.mark.parametrize("legacy_command", ["measure", "observe", "view", "k1"])
def test_research_and_diagnostic_commands_are_not_top_level(
    legacy_command: str,
) -> None:
    with pytest.raises(SystemExit) as error:
        main([legacy_command, "--help"])

    assert error.value.code == 2


def test_purge_session_requires_explicit_apply_and_preserves_other_session(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    with storage.open_ledger("delete-me"):
        pass
    with storage.open_ledger("keep-me"):
        pass
    target = storage.ledger_path("delete-me")
    survivor = storage.ledger_path("keep-me")
    command = [
        "purge",
        "--session",
        "delete-me",
        "--data-dir",
        str(storage.root),
        "--format",
        "json",
    ]

    assert main([*command, "--dry-run"]) == EXIT_OK
    preview = json.loads(capsys.readouterr().out)
    assert preview["applied"] is False
    assert preview["sessions"] == 1
    assert target.exists()
    assert survivor.exists()

    assert main(command) == EXIT_OK
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied"] is True
    assert applied["deleted_files"] == 1
    assert not target.exists()
    assert survivor.exists()


def test_purge_older_than_dry_run_keeps_every_ledger(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    with storage.open_ledger("session-a"):
        pass
    target = storage.ledger_path("session-a")

    assert (
        main(
            [
                "purge",
                "--older-than",
                "1s",
                "--data-dir",
                str(storage.root),
                "--dry-run",
                "--format",
                "json",
            ]
        )
        == EXIT_OK
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["selector"] == "older-than=1s"
    assert payload["applied"] is False
    assert target.exists()
