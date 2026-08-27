"""Immutable installer-plan render contracts: pure functions, no
filesystem access anywhere in this module or its tests."""

from __future__ import annotations

from laconic.observe.preview import (
    CLAUDE_CODE_EVENTS,
    CLAUDE_CODE_OWNED_MARKER,
    OMP_OWNED_MARKER,
    preview_claude_code_install,
    preview_claude_code_remove,
)
from laconic.observe.render import (
    DEFAULT_SESSION_END_TIMEOUT_SECONDS,
    DEFAULT_TOOL_EVENT_TIMEOUT_SECONDS,
    render_claude_code_settings_installed,
    render_claude_code_settings_removed,
    render_omp_extension_source,
)

_PYTHON = "/usr/bin/python3.12"


def test_install_on_empty_settings_adds_all_three_events() -> None:
    rendered = render_claude_code_settings_installed({}, python=_PYTHON)
    assert set(rendered["hooks"]) == set(CLAUDE_CODE_EVENTS)


def test_installed_entry_uses_exec_form_with_absolute_python() -> None:
    rendered = render_claude_code_settings_installed({}, python=_PYTHON)
    handler = rendered["hooks"]["PostToolUse"][0]["hooks"][0]
    assert handler["type"] == "command"
    assert handler["command"] == _PYTHON
    assert handler["args"] == ["-m", "laconic.observe.entrypoint", "--client", "claude-code"]


def test_installed_entry_marker_lives_in_status_message_not_command() -> None:
    """The marker must never be appended to `command`/`args`: the
    entrypoint's argparse accepts no positional arguments, so an extra
    token there would break argument parsing (H-50)."""
    rendered = render_claude_code_settings_installed({}, python=_PYTHON)
    handler = rendered["hooks"]["PostToolUse"][0]["hooks"][0]
    assert handler["statusMessage"] == CLAUDE_CODE_OWNED_MARKER
    assert CLAUDE_CODE_OWNED_MARKER not in handler["command"]
    assert CLAUDE_CODE_OWNED_MARKER not in " ".join(handler["args"])


def test_session_end_uses_its_own_shorter_default_timeout() -> None:
    rendered = render_claude_code_settings_installed({}, python=_PYTHON)
    session_end_timeout = rendered["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"]
    tool_timeout = rendered["hooks"]["PostToolUse"][0]["hooks"][0]["timeout"]
    assert session_end_timeout == DEFAULT_SESSION_END_TIMEOUT_SECONDS
    assert tool_timeout == DEFAULT_TOOL_EVENT_TIMEOUT_SECONDS
    assert session_end_timeout < tool_timeout


def test_install_is_idempotent() -> None:
    once = render_claude_code_settings_installed({}, python=_PYTHON)
    twice = render_claude_code_settings_installed(once, python=_PYTHON)
    assert once == twice


def test_install_preserves_foreign_handlers_and_top_level_keys() -> None:
    existing = {
        "hooks": {
            "PostToolUse": [
                {"matcher": "Edit", "hooks": [{"type": "command", "command": "./lint.sh"}]}
            ]
        },
        "model": "sonnet",
    }
    rendered = render_claude_code_settings_installed(existing, python=_PYTHON)
    assert rendered["model"] == "sonnet"
    assert {"matcher": "Edit", "hooks": [{"type": "command", "command": "./lint.sh"}]} in rendered[
        "hooks"
    ]["PostToolUse"]


def test_render_output_agrees_with_preview_after_install() -> None:
    """Cross-check between the decision plane (`preview`) and the data
    plane (`render`): once rendered, preview must report every event as
    already-owned, never as a pending add."""
    rendered = render_claude_code_settings_installed({}, python=_PYTHON)
    plan = preview_claude_code_install(rendered)
    assert {action.kind for action in plan.actions} == {"noop"}


def test_remove_strips_only_owned_entries() -> None:
    installed = render_claude_code_settings_installed(
        {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Edit", "hooks": [{"type": "command", "command": "./lint.sh"}]}
                ]
            }
        },
        python=_PYTHON,
    )
    removed = render_claude_code_settings_removed(installed)
    assert {"matcher": "Edit", "hooks": [{"type": "command", "command": "./lint.sh"}]} in removed[
        "hooks"
    ]["PostToolUse"]
    plan = preview_claude_code_remove(removed)
    assert all(action.kind == "noop" for action in plan.actions)


def test_remove_drops_empty_event_groups_entirely() -> None:
    installed = render_claude_code_settings_installed({}, python=_PYTHON)
    removed = render_claude_code_settings_removed(installed)
    assert "hooks" not in removed


def test_remove_on_a_document_with_no_owned_entries_is_unchanged() -> None:
    existing = {"model": "sonnet", "hooks": {"PostToolUse": [{"hooks": [{"command": "./x.sh"}]}]}}
    removed = render_claude_code_settings_removed(existing)
    assert removed == existing


def test_install_then_remove_round_trips_to_original_foreign_state() -> None:
    original = {
        "hooks": {
            "PostToolUse": [
                {"matcher": "Edit", "hooks": [{"type": "command", "command": "./x.sh"}]}
            ]
        },
        "model": "sonnet",
    }
    installed = render_claude_code_settings_installed(original, python=_PYTHON)
    round_tripped = render_claude_code_settings_removed(installed)
    assert round_tripped == original


def test_omp_extension_source_carries_the_owned_marker() -> None:
    source = render_omp_extension_source(python=_PYTHON)
    assert source.startswith(OMP_OWNED_MARKER)


def test_omp_extension_source_registers_both_events() -> None:
    source = render_omp_extension_source(python=_PYTHON)
    assert 'pi.on("tool_result"' in source
    assert 'pi.on("session_shutdown"' in source


def test_omp_extension_source_invokes_the_shared_entrypoint_with_omp_client() -> None:
    source = render_omp_extension_source(python=_PYTHON)
    assert _PYTHON in source
    assert '"--client", "omp"' in source


def test_omp_extension_source_args_are_a_real_json_array_not_a_joined_string() -> None:
    source = render_omp_extension_source(python=_PYTHON)
    assert '["-m", "laconic.observe.entrypoint", "--client", "omp"]' in source


def test_omp_extension_source_never_returns_a_handler_value() -> None:
    """Observe's OMP handlers must never override a tool result or
    inject context -- they only forward a fire-and-forget envelope."""
    source = render_omp_extension_source(python=_PYTHON)
    assert "return {" not in source


def test_omp_extension_source_wraps_the_exec_call_in_a_timeout_race() -> None:
    source = render_omp_extension_source(python=_PYTHON, timeout_ms=1234)
    assert "withTimeout" in source
    assert "1234" in source
