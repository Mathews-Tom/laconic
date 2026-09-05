from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from laconic.cli import EXIT_OK, EXIT_OMP_INSTALL_ERROR, main
from laconic.runtime.omp_installer import (
    OBSERVE_OWNED_MARKER,
    OMP_EXTENSION_FILENAME,
    OMP_OWNED_MARKER,
)


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
