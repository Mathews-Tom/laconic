"""M3 PR-2: real filesystem installer.

Ownership scanning, dry-run preview, and atomic apply/remove against real
Claude Code / OMP configuration locations. Every function takes an
explicit :class:`~pathlib.Path`; nothing here reads ``Path.home()`` or
``Path.cwd()`` itself. Real default path resolution belongs to the CLI
layer (M3 PR-3), so this module stays fully testable against
``tmp_path``-scoped fixtures and never touches a real home directory or
this repository's own ``.claude``/``.omp`` state.

Every write is atomic: content is written to a sibling temp file, then
renamed into place with :func:`os.replace`, so a crash mid-write can
never leave a half-written settings file or extension module behind.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from laconic.observe.preview import (
    OMP_OWNED_FILENAME,
    OMP_OWNED_MARKER,
    InstallPlan,
    preview_claude_code_install,
    preview_claude_code_remove,
    preview_omp_install,
    preview_omp_remove,
)
from laconic.observe.render import (
    render_claude_code_settings_installed,
    render_claude_code_settings_removed,
    render_omp_extension_source,
)


class ConfigParseError(ValueError):
    """Raised when an existing configuration file cannot be read as the
    shape Observe expects."""


class OwnershipConflictError(RuntimeError):
    """Raised when a real file already occupies Observe's owned path but
    lacks the ownership marker -- installing would silently overwrite
    someone else's file rather than merely add Observe's own."""


def _read_claude_code_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigParseError(f"{path}: not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ConfigParseError(f"{path}: top-level value must be a JSON object")
    return data


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".laconic-tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _read_omp_extensions_dir(directory: Path) -> tuple[tuple[str, str], ...]:
    if not directory.exists():
        return ()
    entries = []
    for entry in sorted(directory.iterdir()):
        if entry.is_file() and entry.suffix in (".ts", ".js"):
            entries.append((entry.name, entry.read_text(encoding="utf-8")))
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class InstallResult:
    """The outcome of an apply/remove call: the plan that was computed,
    whether anything was actually written, and the path touched (if
    ``applied``)."""

    plan: InstallPlan
    applied: bool
    path: Path


def preview_claude_code(path: Path, *, remove: bool = False) -> InstallPlan:
    """Dry-run preview against the real file at ``path``. Never writes."""
    existing = _read_claude_code_settings(path)
    return preview_claude_code_remove(existing) if remove else preview_claude_code_install(existing)


def apply_claude_code_install(path: Path, *, python: str | None = None) -> InstallResult:
    """Read ``path`` (if present), and atomically write an installed
    document back only if anything would actually change. Idempotent:
    calling this twice in a row writes once."""
    existing = _read_claude_code_settings(path)
    plan = preview_claude_code_install(existing)
    changed = any(action.kind == "add" for action in plan.actions)
    if changed:
        rendered = render_claude_code_settings_installed(existing, python=python or sys.executable)
        _atomic_write_text(path, json.dumps(rendered, indent=2, sort_keys=True) + "\n")
    return InstallResult(plan=plan, applied=changed, path=path)


def apply_claude_code_remove(path: Path) -> InstallResult:
    """Read ``path`` (if present), and atomically write a document with
    every Observe-owned entry stripped, only if anything would actually
    change. Never deletes the settings file itself, even when the result
    is an empty document -- the file may predate Observe and belong to
    the operator, not to this installer."""
    existing = _read_claude_code_settings(path)
    plan = preview_claude_code_remove(existing)
    changed = any(action.kind == "remove" for action in plan.actions)
    if changed:
        rendered = render_claude_code_settings_removed(existing)
        _atomic_write_text(path, json.dumps(rendered, indent=2, sort_keys=True) + "\n")
    return InstallResult(plan=plan, applied=changed, path=path)


def preview_omp(directory: Path, *, remove: bool = False) -> InstallPlan:
    """Dry-run preview against the real directory listing at
    ``directory``. Never writes."""
    existing = _read_omp_extensions_dir(directory)
    if remove:
        return preview_omp_remove(existing, scope_dir=str(directory))
    return preview_omp_install(existing, scope_dir=str(directory))


def apply_omp_install(directory: Path, *, python: str | None = None) -> InstallResult:
    """Read ``directory``'s listing, and atomically write Observe's owned
    extension file only if it is not already present. Idempotent.

    Raises :class:`OwnershipConflictError` instead of overwriting a real
    file that already occupies the owned path but lacks the ownership
    marker -- a foreign file's presence is a conflict Observe must
    surface, never silently resolve by overwriting.
    """
    existing = _read_omp_extensions_dir(directory)
    plan = preview_omp_install(existing, scope_dir=str(directory))
    target = directory / OMP_OWNED_FILENAME
    owned_present = any(
        name == OMP_OWNED_FILENAME and OMP_OWNED_MARKER in content for name, content in existing
    )
    if target.exists() and not owned_present:
        raise OwnershipConflictError(
            f"{target}: exists and is not Observe-owned; refusing to overwrite"
        )
    changed = any(action.kind == "add" for action in plan.actions)
    if changed:
        source = render_omp_extension_source(python=python or sys.executable)
        _atomic_write_text(target, source)
    return InstallResult(plan=plan, applied=changed, path=target)


def apply_omp_remove(directory: Path) -> InstallResult:
    """Delete Observe's owned extension file from ``directory`` if
    present. A file-drop install owns its whole file, unlike Claude
    Code's per-entry JSON merge, so removal deletes it outright rather
    than rewriting it."""
    existing = _read_omp_extensions_dir(directory)
    plan = preview_omp_remove(existing, scope_dir=str(directory))
    changed = any(action.kind == "remove" for action in plan.actions)
    target = directory / OMP_OWNED_FILENAME
    if changed:
        target.unlink(missing_ok=True)
    return InstallResult(plan=plan, applied=changed, path=target)
