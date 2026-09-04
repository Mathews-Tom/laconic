"""Ownership-safe installer for Laconic's OMP runtime extension."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal

from laconic.runtime.storage import default_data_dir

OMP_EXTENSION_FILENAME = "zz-laconic-runtime.ts"
OMP_OWNED_MARKER = "// laconic-runtime: owned"
OBSERVE_OWNED_MARKER = "// laconic-observe: owned"
RUNTIME_ENTRYPOINT = ("-I", "-m", "laconic.runtime")
_PYTHON_PLACEHOLDER = "__LACONIC_PYTHON__"
_DATA_DIRECTORY_PLACEHOLDER = "__LACONIC_DATA_DIRECTORY__"
_PROFILE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_WINDOWS_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?", re.IGNORECASE)

InstallScope = Literal["project", "user"]
InstallOperation = Literal["add", "update", "remove", "none"]


class OmpInstallError(RuntimeError):
    """Base class for rejected OMP adapter installation operations."""


class OwnershipConflictError(OmpInstallError):
    """Raised when Laconic's target path contains a foreign file."""


class DuplicateAdapterError(OmpInstallError):
    """Raised when another Laconic adapter is already installed in the scope."""


class InvalidProfileError(OmpInstallError):
    """Raised when a profile cannot map to an OMP agent directory."""


@dataclass(frozen=True, slots=True)
class OmpInstallPlan:
    """Content-bounded preview of one install or uninstall operation."""

    operation: InstallOperation
    path: Path
    python: str | None
    entrypoint: tuple[str, ...]
    data_directory: Path | None
    preserved: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OmpInstallResult:
    """Applied state transition and the exact plan that authorized it."""

    plan: OmpInstallPlan
    applied: bool


def normalize_profile(profile: str | None) -> str | None:
    """Mirror OMP's named-profile grammar and default-profile sentinels."""
    normalized = profile.strip() if profile is not None else ""
    if not normalized or normalized == "default":
        return None
    if (
        normalized in (".", "..")
        or normalized.endswith(".")
        or _PROFILE.fullmatch(normalized) is None
        or _WINDOWS_RESERVED.fullmatch(normalized) is not None
    ):
        raise InvalidProfileError(f"invalid OMP profile: {profile!r}")
    return normalized


def active_profile(env: Mapping[str, str]) -> str | None:
    """Apply OMP_PROFILE precedence over the legacy PI_PROFILE fallback."""
    value = env.get("OMP_PROFILE") if "OMP_PROFILE" in env else env.get("PI_PROFILE")
    return normalize_profile(value)


def omp_extensions_directory(
    *,
    scope: InstallScope,
    cwd: Path,
    home: Path,
    user_dir: Path | None = None,
    profile: str | None = None,
    env: Mapping[str, str] = os.environ,
) -> Path:
    """Resolve the native OMP extension directory without invoking OMP."""
    if scope == "project":
        return cwd / ".omp" / "extensions"
    if user_dir is not None:
        return user_dir.expanduser()

    selected = normalize_profile(profile) if profile is not None else active_profile(env)
    config_dir = env.get("PI_CONFIG_DIR") or ".omp"
    if selected is not None:
        return home / config_dir / "profiles" / selected / "agent" / "extensions"
    if override := env.get("PI_CODING_AGENT_DIR"):
        return Path(override).expanduser() / "extensions"
    return home / config_dir / "agent" / "extensions"


def _reject_symlink_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path.expanduser()))
    for component in (absolute, *absolute.parents):
        if component.is_symlink():
            raise OwnershipConflictError(f"{component}: extension path must not contain symlinks")


def _extension_files(directory: Path) -> tuple[tuple[Path, str], ...]:
    _reject_symlink_components(directory)
    if not directory.exists():
        return ()
    if not directory.is_dir() or directory.is_symlink():
        raise OwnershipConflictError(f"{directory}: extension root is not an ordinary directory")
    entries: list[tuple[Path, str]] = []
    for path in sorted(directory.iterdir()):
        if path.suffix not in (".ts", ".js"):
            continue
        if not path.is_file() or path.is_symlink():
            raise OwnershipConflictError(f"{path}: extension entry is not an ordinary file")
        entries.append((path, path.read_text(encoding="utf-8")))
    return tuple(entries)


def _render_source(*, python: str, data_directory: Path) -> str:
    template = files("laconic.runtime.omp").joinpath("laconic.ts").read_text(encoding="utf-8")
    return template.replace(json.dumps(_PYTHON_PLACEHOLDER), json.dumps(python)).replace(
        json.dumps(_DATA_DIRECTORY_PLACEHOLDER), json.dumps(str(data_directory))
    )


def _resolved_python(value: str | None) -> str:
    # Preserve a virtual-environment or uv-tool symlink: resolving it records
    # the base interpreter, which cannot import the environment's Laconic.
    path = Path(os.path.abspath(Path(value or sys.executable).expanduser()))
    if not path.is_file() or not os.access(path, os.X_OK):
        raise OmpInstallError(f"Python interpreter is not executable: {path}")
    return str(path)


def _scan_for_duplicates(entries: tuple[tuple[Path, str], ...], target: Path) -> None:
    for path, source in entries:
        if path == target:
            continue
        if OMP_OWNED_MARKER in source or OBSERVE_OWNED_MARKER in source:
            raise DuplicateAdapterError(f"{path}: another Laconic OMP adapter is installed")


def preview_omp_install(
    directory: Path,
    *,
    python: str | None = None,
    data_directory: Path | None = None,
) -> OmpInstallPlan:
    """Preview an idempotent install without writing any file or storage state."""
    entries = _extension_files(directory)
    target = directory / OMP_EXTENSION_FILENAME
    _scan_for_duplicates(entries, target)
    by_path = dict(entries)
    current = by_path.get(target)
    if current is not None and OMP_OWNED_MARKER not in current:
        raise OwnershipConflictError(f"{target}: exists and is not Laconic-owned")

    resolved_python = _resolved_python(python)
    resolved_data = (data_directory or default_data_dir()).expanduser().resolve(strict=False)
    rendered = _render_source(python=resolved_python, data_directory=resolved_data)
    operation: InstallOperation
    if current is None:
        operation = "add"
    elif current == rendered:
        operation = "none"
    else:
        operation = "update"
    preserved = tuple(path.name for path, _source in entries if path != target)
    return OmpInstallPlan(
        operation=operation,
        path=target,
        python=resolved_python,
        entrypoint=RUNTIME_ENTRYPOINT,
        data_directory=resolved_data,
        preserved=preserved,
    )


def preview_omp_uninstall(directory: Path) -> OmpInstallPlan:
    """Preview removing only Laconic's marked runtime extension."""
    entries = _extension_files(directory)
    target = directory / OMP_EXTENSION_FILENAME
    by_path = dict(entries)
    current = by_path.get(target)
    if current is not None and OMP_OWNED_MARKER not in current:
        raise OwnershipConflictError(f"{target}: exists and is not Laconic-owned")
    preserved = tuple(path.name for path, _source in entries if path != target)
    return OmpInstallPlan(
        operation="remove" if current is not None else "none",
        path=target,
        python=None,
        entrypoint=RUNTIME_ENTRYPOINT,
        data_directory=None,
        preserved=preserved,
    )


def _atomic_write_text(path: Path, content: str) -> None:
    _reject_symlink_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def apply_omp_install(
    directory: Path,
    *,
    python: str | None = None,
    data_directory: Path | None = None,
) -> OmpInstallResult:
    """Atomically install or refresh the one owned extension asset."""
    plan = preview_omp_install(directory, python=python, data_directory=data_directory)
    if plan.operation in ("add", "update"):
        assert plan.python is not None
        assert plan.data_directory is not None
        source = _render_source(python=plan.python, data_directory=plan.data_directory)
        _atomic_write_text(plan.path, source)
        return OmpInstallResult(plan=plan, applied=True)
    return OmpInstallResult(plan=plan, applied=False)


def apply_omp_uninstall(directory: Path) -> OmpInstallResult:
    """Remove only the currently marked runtime extension, never its ledgers."""
    plan = preview_omp_uninstall(directory)
    if plan.operation == "remove":
        _reject_symlink_components(plan.path.parent)
        plan.path.unlink()
        return OmpInstallResult(plan=plan, applied=True)
    return OmpInstallResult(plan=plan, applied=False)
