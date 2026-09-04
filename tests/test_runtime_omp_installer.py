from __future__ import annotations

import json
from pathlib import Path

import pytest

from laconic.runtime.omp_installer import (
    OBSERVE_OWNED_MARKER,
    OMP_EXTENSION_FILENAME,
    OMP_OWNED_MARKER,
    DuplicateAdapterError,
    InvalidProfileError,
    OwnershipConflictError,
    active_profile,
    apply_omp_install,
    apply_omp_uninstall,
    normalize_profile,
    omp_extensions_directory,
    preview_omp_install,
    preview_omp_uninstall,
)


def test_project_install_preview_is_content_bounded_and_does_not_write(tmp_path: Path) -> None:
    extension_dir = tmp_path / ".omp" / "extensions"

    plan = preview_omp_install(
        extension_dir,
        python="/usr/bin/python3",
        data_directory=tmp_path / "private-data",
    )

    assert plan.operation == "add"
    assert plan.path == extension_dir / OMP_EXTENSION_FILENAME
    assert plan.entrypoint == ("-m", "laconic.runtime")
    assert plan.preserved == ()
    assert not extension_dir.exists()
    assert not (tmp_path / "private-data").exists()


def test_install_is_idempotent_and_records_exact_invocation(tmp_path: Path) -> None:
    extension_dir = tmp_path / ".omp" / "extensions"
    data_dir = tmp_path / "data"

    first = apply_omp_install(
        extension_dir,
        python="/usr/bin/python3",
        data_directory=data_dir,
    )
    installed = first.plan.path.read_text(encoding="utf-8")
    second = apply_omp_install(
        extension_dir,
        python="/usr/bin/python3",
        data_directory=data_dir,
    )

    assert first.applied is True
    assert second.applied is False
    assert second.plan.operation == "none"
    assert OMP_OWNED_MARKER in installed
    assert json.dumps("/usr/bin/python3") in installed
    assert '["-m", "laconic.runtime"]' in installed
    assert json.dumps(str(data_dir)) in installed


def test_owned_install_can_update_its_recorded_interpreter(tmp_path: Path) -> None:
    extension_dir = tmp_path / "extensions"
    apply_omp_install(
        extension_dir,
        python="/usr/bin/python3",
        data_directory=tmp_path / "data",
    )

    plan = preview_omp_install(
        extension_dir,
        python="/bin/sh",
        data_directory=tmp_path / "data",
    )

    assert plan.operation == "update"


def test_install_refuses_foreign_target_without_modification(tmp_path: Path) -> None:
    extension_dir = tmp_path / "extensions"
    extension_dir.mkdir()
    target = extension_dir / OMP_EXTENSION_FILENAME
    target.write_text("export default () => {};\n", encoding="utf-8")

    with pytest.raises(OwnershipConflictError, match="not Laconic-owned"):
        apply_omp_install(extension_dir, python="/usr/bin/python3")

    assert target.read_text(encoding="utf-8") == "export default () => {};\n"


@pytest.mark.parametrize("marker", [OMP_OWNED_MARKER, OBSERVE_OWNED_MARKER])
def test_install_rejects_a_second_laconic_adapter(tmp_path: Path, marker: str) -> None:
    extension_dir = tmp_path / "extensions"
    extension_dir.mkdir()
    other = extension_dir / "other-laconic.ts"
    other.write_text(f"{marker}\nexport default () => {{}};\n", encoding="utf-8")

    with pytest.raises(DuplicateAdapterError, match="another Laconic OMP adapter"):
        preview_omp_install(extension_dir, python="/usr/bin/python3")


def test_uninstall_removes_only_owned_asset_and_keeps_data(tmp_path: Path) -> None:
    extension_dir = tmp_path / "extensions"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sentinel = data_dir / "ledger.sqlite3"
    sentinel.write_text("private", encoding="utf-8")
    foreign = extension_dir / "foreign.ts"
    extension_dir.mkdir(exist_ok=True)
    foreign.write_text("export default () => {};\n", encoding="utf-8")
    apply_omp_install(extension_dir, python="/usr/bin/python3", data_directory=data_dir)

    preview = preview_omp_uninstall(extension_dir)
    result = apply_omp_uninstall(extension_dir)

    assert preview.operation == "remove"
    assert result.applied is True
    assert not (extension_dir / OMP_EXTENSION_FILENAME).exists()
    assert foreign.exists()
    assert sentinel.read_text(encoding="utf-8") == "private"
    assert apply_omp_uninstall(extension_dir).applied is False


def test_profile_resolution_matches_omp_precedence(tmp_path: Path) -> None:
    env = {
        "OMP_PROFILE": "work",
        "PI_PROFILE": "ignored",
        "PI_CONFIG_DIR": ".custom-omp",
        "PI_CODING_AGENT_DIR": str(tmp_path / "ignored-agent"),
    }

    assert active_profile(env) == "work"
    assert (
        omp_extensions_directory(scope="user", cwd=tmp_path, home=tmp_path, env=env)
        == tmp_path / ".custom-omp" / "profiles" / "work" / "agent" / "extensions"
    )
    assert active_profile({"OMP_PROFILE": "", "PI_PROFILE": "ignored"}) is None


def test_default_profile_honors_agent_directory_override(tmp_path: Path) -> None:
    target = omp_extensions_directory(
        scope="user",
        cwd=tmp_path,
        home=tmp_path,
        env={"PI_CODING_AGENT_DIR": str(tmp_path / "agent")},
    )
    assert target == tmp_path / "agent" / "extensions"


def test_explicit_user_directory_and_project_scope_take_precedence(tmp_path: Path) -> None:
    override = tmp_path / "explicit"
    assert (
        omp_extensions_directory(
            scope="user",
            cwd=tmp_path,
            home=tmp_path,
            user_dir=override,
            profile="work",
            env={},
        )
        == override
    )
    assert (
        omp_extensions_directory(
            scope="project",
            cwd=tmp_path,
            home=tmp_path,
            user_dir=override,
            profile="work",
            env={},
        )
        == tmp_path / ".omp" / "extensions"
    )


@pytest.mark.parametrize("profile", ["..", "Work", "bad/profile", "con", "name."])
def test_profile_names_follow_omp_grammar(profile: str) -> None:
    with pytest.raises(InvalidProfileError):
        normalize_profile(profile)
