"""Tests for `laconic.k1corpus.deny_overlay` (K1 Stage C M1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from laconic.k1corpus.deny_overlay import (
    BUILTIN_TOOL_NAMES,
    SAFE_READONLY_TOOLS,
    UnclassifiedToolError,
    denied_tools,
    render_deny_overlay_yaml,
    write_deny_overlay,
)

#: Every mutating or dynamic-tier built-in tool the design gate (H-68)
#: reviewed and confirmed must be denied -- including two OMP declares
#: `"read"` tier (`memory_edit`, `retain`, real side effects on the
#: memory vault) and one whose write-capable branch cannot be gated at
#: the tool-name level (`lsp`). See `deny_overlay.py`'s module docstring.
EXPECTED_DENIED_TOOLS = frozenset(BUILTIN_TOOL_NAMES) - SAFE_READONLY_TOOLS


def test_every_builtin_tool_is_classified() -> None:
    """Every real OMP built-in tool name is either explicitly allowlisted
    as safe or denied -- no third state, no silent omission."""
    denied = set(denied_tools())
    assert denied | SAFE_READONLY_TOOLS == set(BUILTIN_TOOL_NAMES)
    assert denied.isdisjoint(SAFE_READONLY_TOOLS)


def test_a_newly_added_tool_is_denied_without_any_further_code_change() -> None:
    """The deny set is an allowlist-complement: a hypothetical tool name
    added to the registry (without touching `SAFE_READONLY_TOOLS`) lands
    in the deny set automatically -- the "a newly-added tool cannot land
    unguarded" property the design gate requires."""
    hypothetical_registry = (*BUILTIN_TOOL_NAMES, "totally_new_tool")
    assert "totally_new_tool" in denied_tools(hypothetical_registry)


def test_denied_tools_is_sorted_and_deduplicated() -> None:
    denied = denied_tools()
    assert denied == tuple(sorted(set(denied)))
    assert len(denied) == len(set(denied))


def test_denied_tools_matches_the_reviewed_set() -> None:
    assert set(denied_tools()) == EXPECTED_DENIED_TOOLS


@pytest.mark.parametrize("tool", sorted(SAFE_READONLY_TOOLS))
def test_known_safe_tools_stay_off_the_deny_list(tool: str) -> None:
    assert tool not in denied_tools()


@pytest.mark.parametrize(
    "tool",
    sorted(EXPECTED_DENIED_TOOLS),
    ids=sorted(EXPECTED_DENIED_TOOLS),
)
def test_every_reviewed_mutating_tool_is_denied(tool: str) -> None:
    assert tool in denied_tools()


def test_memory_edit_and_retain_are_denied_despite_read_tier() -> None:
    """OMP declares both `"read"` tier, but both mutate the memory vault
    -- the design gate's central finding (module docstring)."""
    denied = denied_tools()
    assert "memory_edit" in denied
    assert "retain" in denied


def test_lsp_is_denied_wholesale() -> None:
    """`lsp`'s write-capable branch has no per-tool-name way to gate
    selectively, so the whole tool is denied (module docstring)."""
    assert "lsp" in denied_tools()


def test_unclassified_safe_tool_name_raises() -> None:
    with pytest.raises(UnclassifiedToolError):
        denied_tools(tool_names=("bash",), safe=frozenset({"read"}))


def test_render_deny_overlay_yaml_forces_always_ask_mode() -> None:
    text = render_deny_overlay_yaml()
    assert text.startswith("tools:\n  approvalMode: always-ask\n  approval:\n")


def test_render_deny_overlay_yaml_sets_deny_for_every_non_safe_tool() -> None:
    text = render_deny_overlay_yaml()
    for tool in denied_tools():
        assert f"\n    {tool}: deny\n" in text
    for tool in SAFE_READONLY_TOOLS:
        assert f"    {tool}: deny" not in text


def test_render_deny_overlay_yaml_matches_h64_validated_shape() -> None:
    """H-64/H-65/H-66's real spikes used bare `tools.approval.<tool>: deny`
    entries and this exact syntax was empirically confirmed to block
    execution while still surfacing the proposed action -- the generator
    must reproduce that literal shape, not an equivalent-looking
    approximation."""
    text = render_deny_overlay_yaml(tool_names=("bash", "write"), safe=frozenset())
    expected = "tools:\n  approvalMode: always-ask\n  approval:\n    bash: deny\n    write: deny\n"
    assert text == expected


def test_write_deny_overlay_is_mode_restricted(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deny-overlay.yml"
    write_deny_overlay(target)
    assert oct(target.parent.stat().st_mode)[-3:] == "700"
    assert oct(target.stat().st_mode)[-3:] == "600"
    assert target.read_text() == render_deny_overlay_yaml()


def test_write_deny_overlay_is_atomic_and_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "deny-overlay.yml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("stale content")
    write_deny_overlay(target)
    assert target.read_text() == render_deny_overlay_yaml()
