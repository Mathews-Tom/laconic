"""M1 deliverable: non-destructive install/remove plan previews.

Every function here computes what *would* change against a caller-supplied
synthetic configuration and returns an :class:`InstallPlan`. Nothing is
read from a real path and nothing is written -- there is no filesystem
access in this module at all. Real, atomic, CLI-controlled installation is
M3's scope (`docs/observe-design.md` § Installer and runtime behavior).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from laconic.observe.contracts import ClientId, InstallMechanism

#: Substring embedded in every Observe-owned Claude Code hook command,
#: used to detect and remove only entries Observe itself would add.
CLAUDE_CODE_OWNED_MARKER = "__laconic_observe__"

#: Substring embedded in every Observe-owned OMP extension file's content,
#: used to detect ownership independent of the file's name.
OMP_OWNED_MARKER = "// laconic-observe: owned"

#: The file Observe's OMP adapter would occupy in a given scope directory.
OMP_OWNED_FILENAME = "laconic-observe.ts"

_CLAUDE_CODE_EVENTS = ("PostToolUse", "PostToolUseFailure", "SessionEnd")


@dataclass(frozen=True, slots=True)
class PlanAction:
    """One computed change (or explicit non-change) in a plan."""

    kind: str
    """``"add"``, ``"remove"``, or ``"noop"``."""

    description: str


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """A previewed set of actions for one client, with everything the
    preview left untouched named explicitly."""

    client: ClientId
    mechanism: InstallMechanism
    actions: tuple[PlanAction, ...]
    preserved: tuple[str, ...]


def _claude_code_owned_handlers(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        handler
        for group in groups
        for handler in group.get("hooks", [])
        if CLAUDE_CODE_OWNED_MARKER in str(handler.get("command", ""))
    ]


def _claude_code_foreign_handlers(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        handler
        for group in groups
        for handler in group.get("hooks", [])
        if CLAUDE_CODE_OWNED_MARKER not in str(handler.get("command", ""))
    ]


def preview_claude_code_install(existing_settings: dict[str, Any]) -> InstallPlan:
    """Preview adding Observe's three hook entries to a synthetic Claude
    Code settings document. Idempotent: an event that already carries an
    owned entry previews as a no-op, not a duplicate add."""
    hooks = existing_settings.get("hooks", {})
    actions: list[PlanAction] = []
    preserved: list[str] = []

    for event in _CLAUDE_CODE_EVENTS:
        groups = hooks.get(event, [])
        if _claude_code_owned_handlers(groups):
            actions.append(PlanAction("noop", f"{event}: Observe entry already present"))
        else:
            actions.append(PlanAction("add", f"{event}: add one Observe-owned command hook"))
        for handler in _claude_code_foreign_handlers(groups):
            preserved.append(f"{event}: preserved existing handler {handler.get('command')!r}")

    for key in existing_settings:
        if key != "hooks":
            preserved.append(f"top-level key {key!r} left untouched")

    return InstallPlan(
        client=ClientId.CLAUDE_CODE,
        mechanism=InstallMechanism.JSON_ENTRY_MERGE,
        actions=tuple(actions),
        preserved=tuple(preserved),
    )


def preview_claude_code_remove(existing_settings: dict[str, Any]) -> InstallPlan:
    """Preview removing only Observe-owned entries from a synthetic Claude
    Code settings document."""
    hooks = existing_settings.get("hooks", {})
    actions: list[PlanAction] = []
    preserved: list[str] = []

    for event in _CLAUDE_CODE_EVENTS:
        groups = hooks.get(event, [])
        if _claude_code_owned_handlers(groups):
            actions.append(PlanAction("remove", f"{event}: remove Observe-owned command hook(s)"))
        for handler in _claude_code_foreign_handlers(groups):
            preserved.append(f"{event}: preserved existing handler {handler.get('command')!r}")

    for key in existing_settings:
        if key != "hooks":
            preserved.append(f"top-level key {key!r} left untouched")

    if not actions:
        actions.append(PlanAction("noop", "no Observe-owned entries found"))

    return InstallPlan(
        client=ClientId.CLAUDE_CODE,
        mechanism=InstallMechanism.JSON_ENTRY_MERGE,
        actions=tuple(actions),
        preserved=tuple(preserved),
    )


def preview_omp_install(
    existing_files: tuple[tuple[str, str], ...], *, scope_dir: str
) -> InstallPlan:
    """Preview writing Observe's owned extension file into a synthetic OMP
    extensions directory listing. ``existing_files`` is a tuple of
    ``(filename, content)`` pairs; ownership is decided by content marker,
    never by filename alone, so a foreign file that happens to share the
    owned name previews as preserved, not overwritten."""
    owned = next(
        (
            name
            for name, content in existing_files
            if name == OMP_OWNED_FILENAME and OMP_OWNED_MARKER in content
        ),
        None,
    )
    if owned is not None:
        actions = (PlanAction("noop", f"{scope_dir}/{OMP_OWNED_FILENAME}: already present"),)
    else:
        actions = (
            PlanAction(
                "add", f"{scope_dir}/{OMP_OWNED_FILENAME}: write one Observe-owned extension module"
            ),
        )
    preserved = tuple(
        f"{scope_dir}/{name}: left untouched"
        for name, content in existing_files
        if not (name == OMP_OWNED_FILENAME and OMP_OWNED_MARKER in content)
    )
    return InstallPlan(
        client=ClientId.OMP,
        mechanism=InstallMechanism.FILE_DROP,
        actions=actions,
        preserved=preserved,
    )


def preview_omp_remove(
    existing_files: tuple[tuple[str, str], ...], *, scope_dir: str
) -> InstallPlan:
    """Preview deleting only the content-marked, Observe-owned extension
    file from a synthetic OMP extensions directory listing."""
    owned = next(
        (
            name
            for name, content in existing_files
            if name == OMP_OWNED_FILENAME and OMP_OWNED_MARKER in content
        ),
        None,
    )
    if owned is not None:
        actions = (
            PlanAction(
                "remove",
                f"{scope_dir}/{OMP_OWNED_FILENAME}: delete Observe-owned extension module",
            ),
        )
    else:
        actions = (PlanAction("noop", f"{scope_dir}/{OMP_OWNED_FILENAME}: nothing to remove"),)
    preserved = tuple(
        f"{scope_dir}/{name}: left untouched"
        for name, content in existing_files
        if not (name == OMP_OWNED_FILENAME and OMP_OWNED_MARKER in content)
    )
    return InstallPlan(
        client=ClientId.OMP,
        mechanism=InstallMechanism.FILE_DROP,
        actions=actions,
        preserved=preserved,
    )
