"""Install/remove plan previews: no filesystem access, computed against
synthetic configuration fixtures only."""

from __future__ import annotations

from laconic.observe.preview import (
    OMP_OWNED_FILENAME,
    OMP_OWNED_MARKER,
    preview_claude_code_install,
    preview_claude_code_remove,
    preview_omp_install,
    preview_omp_remove,
)


def _foreign_claude_code_settings() -> dict[str, object]:
    return {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Edit|Write",
                    "hooks": [{"type": "command", "command": "./scripts/lint.sh"}],
                }
            ]
        },
        "model": "sonnet",
    }


def test_claude_code_install_on_empty_settings_adds_all_three_events() -> None:
    plan = preview_claude_code_install({})
    kinds = {action.kind for action in plan.actions}
    assert kinds == {"add"}
    assert len(plan.actions) == 3
    assert plan.preserved == ()


def test_claude_code_install_preserves_foreign_handlers_and_top_level_keys() -> None:
    plan = preview_claude_code_install(_foreign_claude_code_settings())
    assert any("./scripts/lint.sh" in item for item in plan.preserved)
    assert any("model" in item for item in plan.preserved)


_OWNED_HANDLER = {
    "type": "command",
    "command": "laconic diagnostics observe emit __laconic_observe__",
}


def test_claude_code_install_is_idempotent_on_already_owned_entries() -> None:
    already_installed = {
        "hooks": {
            "PostToolUse": [{"hooks": [_OWNED_HANDLER]}],
            "PostToolUseFailure": [{"hooks": [_OWNED_HANDLER]}],
            "SessionEnd": [{"hooks": [_OWNED_HANDLER]}],
        }
    }
    plan = preview_claude_code_install(already_installed)
    assert {action.kind for action in plan.actions} == {"noop"}


def test_claude_code_remove_on_empty_settings_is_noop() -> None:
    plan = preview_claude_code_remove({})
    assert len(plan.actions) == 1
    assert plan.actions[0].kind == "noop"


def test_claude_code_remove_only_touches_owned_entries() -> None:
    settings = {
        "hooks": {
            "PostToolUse": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "laconic diagnostics observe emit __laconic_observe__",
                        },
                        {"type": "command", "command": "./scripts/lint.sh"},
                    ]
                }
            ]
        }
    }
    plan = preview_claude_code_remove(settings)
    remove_actions = [a for a in plan.actions if a.kind == "remove"]
    assert len(remove_actions) == 1
    assert any("./scripts/lint.sh" in item for item in plan.preserved)


def test_omp_install_on_empty_directory_adds_owned_file() -> None:
    plan = preview_omp_install((), scope_dir=".omp/extensions")
    assert plan.actions[0].kind == "add"
    assert plan.preserved == ()


def test_omp_install_preserves_unrelated_files() -> None:
    files = (("guardrails.ts", "// unrelated"),)
    plan = preview_omp_install(files, scope_dir=".omp/extensions")
    assert plan.actions[0].kind == "add"
    assert any("guardrails.ts" in item for item in plan.preserved)


def test_omp_install_is_idempotent_on_owned_file() -> None:
    files = ((OMP_OWNED_FILENAME, f"{OMP_OWNED_MARKER}\nexport default function () {{}}"),)
    plan = preview_omp_install(files, scope_dir=".omp/extensions")
    assert plan.actions[0].kind == "noop"


def test_omp_install_does_not_claim_a_foreign_file_with_the_owned_name() -> None:
    """A user's own file happening to share the owned filename, but lacking
    the content marker, must not be treated as already-installed."""
    files = ((OMP_OWNED_FILENAME, "// not ours"),)
    plan = preview_omp_install(files, scope_dir=".omp/extensions")
    assert plan.actions[0].kind == "add"
    assert any(OMP_OWNED_FILENAME in item for item in plan.preserved)


def test_omp_remove_on_empty_directory_is_noop() -> None:
    plan = preview_omp_remove((), scope_dir=".omp/extensions")
    assert plan.actions[0].kind == "noop"


def test_omp_remove_only_targets_owned_file() -> None:
    files = (
        (OMP_OWNED_FILENAME, f"{OMP_OWNED_MARKER}\nexport default function () {{}}"),
        ("guardrails.ts", "// unrelated"),
    )
    plan = preview_omp_remove(files, scope_dir=".omp/extensions")
    assert plan.actions[0].kind == "remove"
    assert any("guardrails.ts" in item for item in plan.preserved)
