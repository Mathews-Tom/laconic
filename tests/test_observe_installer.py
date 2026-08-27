"""Real filesystem installer: ownership scanning, dry-run preview, and
atomic apply/remove against `tmp_path`-scoped fixtures only.

No test in this file touches a real home directory or this repository's
own `.claude`/`.omp` state -- every path is explicit and rooted under
pytest's `tmp_path`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from laconic.observe.installer import (
    ConfigParseError,
    OwnershipConflictError,
    apply_claude_code_install,
    apply_claude_code_remove,
    apply_omp_install,
    apply_omp_remove,
    preview_claude_code,
    preview_omp,
)
from laconic.observe.preview import CLAUDE_CODE_OWNED_MARKER, OMP_OWNED_MARKER

_PYTHON = "/usr/bin/python3.12"


def test_preview_on_missing_file_reports_add_for_every_event(tmp_path: Path) -> None:
    plan = preview_claude_code(tmp_path / "settings.json")
    assert {action.kind for action in plan.actions} == {"add"}


def test_preview_never_writes_to_disk(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    preview_claude_code(target)
    assert not target.exists()


def test_apply_install_creates_the_file_and_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "settings.json"
    result = apply_claude_code_install(target, python=_PYTHON)
    assert result.applied is True
    assert target.exists()
    data = json.loads(target.read_text())
    assert CLAUDE_CODE_OWNED_MARKER in json.dumps(data)


def test_apply_install_is_idempotent_on_second_call(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    apply_claude_code_install(target, python=_PYTHON)
    first_bytes = target.read_bytes()
    second = apply_claude_code_install(target, python=_PYTHON)
    assert second.applied is False
    assert target.read_bytes() == first_bytes


def test_apply_install_preserves_hand_written_foreign_content(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"model": "sonnet", "hooks": {}}), encoding="utf-8")
    apply_claude_code_install(target, python=_PYTHON)
    data = json.loads(target.read_text())
    assert data["model"] == "sonnet"


def test_apply_install_rejects_malformed_existing_json(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text("not json {{{", encoding="utf-8")
    with pytest.raises(ConfigParseError):
        apply_claude_code_install(target, python=_PYTHON)
    # A rejected read must not have modified the untouched original file.
    assert target.read_text(encoding="utf-8") == "not json {{{"


def test_apply_remove_on_an_installed_file_strips_owned_entries(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    apply_claude_code_install(target, python=_PYTHON)
    result = apply_claude_code_remove(target)
    assert result.applied is True
    data = json.loads(target.read_text())
    assert CLAUDE_CODE_OWNED_MARKER not in json.dumps(data)


def test_apply_remove_never_deletes_the_settings_file(tmp_path: Path) -> None:
    """The file may predate Observe and belong to the operator; removing
    Observe's own entries must not delete it even when nothing remains."""
    target = tmp_path / "settings.json"
    apply_claude_code_install(target, python=_PYTHON)
    apply_claude_code_remove(target)
    assert target.exists()


def test_apply_remove_on_a_missing_file_is_a_noop_and_creates_nothing(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    result = apply_claude_code_remove(target)
    assert result.applied is False
    assert not target.exists()


def test_apply_remove_preserves_a_foreign_handler(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {"matcher": "Edit", "hooks": [{"type": "command", "command": "./lint.sh"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    apply_claude_code_install(target, python=_PYTHON)
    apply_claude_code_remove(target)
    data = json.loads(target.read_text())
    assert data["hooks"]["PostToolUse"] == [
        {"matcher": "Edit", "hooks": [{"type": "command", "command": "./lint.sh"}]}
    ]


def test_omp_preview_on_missing_directory_reports_add(tmp_path: Path) -> None:
    plan = preview_omp(tmp_path / "extensions")
    assert plan.actions[0].kind == "add"


def test_omp_apply_install_creates_the_owned_file(tmp_path: Path) -> None:
    directory = tmp_path / "extensions"
    result = apply_omp_install(directory, python=_PYTHON)
    assert result.applied is True
    assert result.path.exists()
    assert OMP_OWNED_MARKER in result.path.read_text(encoding="utf-8")


def test_omp_apply_install_is_idempotent(tmp_path: Path) -> None:
    directory = tmp_path / "extensions"
    apply_omp_install(directory, python=_PYTHON)
    first = (directory / "laconic-observe.ts").read_bytes()
    second = apply_omp_install(directory, python=_PYTHON)
    assert second.applied is False
    assert (directory / "laconic-observe.ts").read_bytes() == first


def test_omp_apply_install_preserves_an_unrelated_file(tmp_path: Path) -> None:
    directory = tmp_path / "extensions"
    directory.mkdir()
    (directory / "guardrails.ts").write_text("// unrelated", encoding="utf-8")
    apply_omp_install(directory, python=_PYTHON)
    assert (directory / "guardrails.ts").read_text(encoding="utf-8") == "// unrelated"


def test_omp_apply_install_refuses_to_overwrite_a_foreign_file_with_the_owned_name(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "extensions"
    directory.mkdir()
    (directory / "laconic-observe.ts").write_text("// someone else's file", encoding="utf-8")
    with pytest.raises(OwnershipConflictError):
        apply_omp_install(directory, python=_PYTHON)
    assert (directory / "laconic-observe.ts").read_text(
        encoding="utf-8"
    ) == "// someone else's file"


def test_omp_apply_remove_deletes_only_the_owned_file(tmp_path: Path) -> None:
    directory = tmp_path / "extensions"
    directory.mkdir()
    (directory / "guardrails.ts").write_text("// unrelated", encoding="utf-8")
    apply_omp_install(directory, python=_PYTHON)
    result = apply_omp_remove(directory)
    assert result.applied is True
    assert not (directory / "laconic-observe.ts").exists()
    assert (directory / "guardrails.ts").exists()


def test_omp_apply_remove_on_missing_directory_is_a_noop(tmp_path: Path) -> None:
    result = apply_omp_remove(tmp_path / "extensions")
    assert result.applied is False
