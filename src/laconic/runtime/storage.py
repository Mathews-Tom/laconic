"""Owner-only, path-contained storage for one ledger per runtime session."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from laconic.ledger import Ledger
from laconic.runtime.references import RuntimeReference, validate_session_id

DATA_DIR_ENV_VAR = "LACONIC_DATA_DIR"


class SessionLedgerNotFoundError(FileNotFoundError):
    """Raised when a reference names a session with no local ledger."""


class UnsafeStoragePathError(RuntimeError):
    """Raised when a ledger path is not an ordinary contained file."""


class PrivateStorageUnavailableError(UnsafeStoragePathError):
    """Raised when the platform cannot enforce owner-only runtime storage."""


def _owner_only_storage_supported() -> bool:
    return os.name == "posix"


def default_data_dir() -> Path:
    """Return the platform-native application data directory for Laconic."""
    if override := os.environ.get(DATA_DIR_ENV_VAR):
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Laconic"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "Laconic"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "laconic"


def resolve_data_dir(data_dir: Path | None = None) -> Path:
    """Resolve the runtime root without creating or inspecting it."""
    return (data_dir or default_data_dir()).expanduser().resolve(strict=False)


def session_ledger_path(session_id: str, data_dir: Path | None = None) -> Path:
    """Derive one opaque, contained session path without creating storage."""
    checked = validate_session_id(session_id)
    root = resolve_data_dir(data_dir)
    sessions = root / "sessions"
    path = sessions / f"{_session_digest(checked)}.sqlite3"
    if path.parent.resolve(strict=False) != sessions.resolve(strict=False):
        raise UnsafeStoragePathError(f"runtime ledger escaped storage root: {path}")
    return path


def _session_digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _make_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise UnsafeStoragePathError(f"runtime storage is not a directory: {path}")
    path.chmod(0o700)


def _make_private_file(path: Path) -> None:
    if path.is_symlink():
        raise UnsafeStoragePathError(f"runtime ledger must not be a symlink: {path}")
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        if not path.is_file() or path.is_symlink():
            raise UnsafeStoragePathError(f"runtime ledger is not an ordinary file: {path}")
    else:
        os.close(descriptor)
    path.chmod(0o600)


class RuntimeStorage:
    """Routes namespaced references to private per-session SQLite ledgers."""

    def __init__(self, data_dir: Path | None = None) -> None:
        if not _owner_only_storage_supported():
            raise PrivateStorageUnavailableError(
                "owner-only runtime storage is unavailable on this platform"
            )
        self._root = resolve_data_dir(data_dir)
        self._sessions = self._root / "sessions"
        _make_private_directory(self._root)
        _make_private_directory(self._sessions)

    @property
    def root(self) -> Path:
        return self._root

    def ledger_path(self, session_id: str) -> Path:
        """Derive a contained opaque path without interpolating the session id."""
        path = session_ledger_path(session_id, self._root)
        if path.parent.resolve(strict=True) != self._sessions.resolve(strict=True):
            raise UnsafeStoragePathError(f"runtime ledger escaped storage root: {path}")
        if path.is_symlink():
            raise UnsafeStoragePathError(f"runtime ledger must not be a symlink: {path}")
        return path

    def open_ledger(self, session_id: str) -> Ledger:
        """Create or reopen the private ledger bound to ``session_id``."""
        checked = validate_session_id(session_id)
        path = self.ledger_path(checked)
        _make_private_file(path)
        try:
            ledger = Ledger(path, checked)
        except BaseException:
            path.chmod(0o600)
            raise
        path.chmod(0o600)
        return ledger

    def open_existing_ledger(self, session_id: str) -> Ledger:
        """Open an existing session ledger without creating missing state."""
        checked = validate_session_id(session_id)
        path = self.ledger_path(checked)
        if not path.is_file():
            raise SessionLedgerNotFoundError(f"unknown runtime session: {checked}")
        _make_private_file(path)
        return Ledger(path, checked)

    def expand(self, value: str) -> str:
        """Resolve exactly the session and internal handle named by ``value``."""
        reference = RuntimeReference.parse(value)
        with self.open_existing_ledger(reference.session_id) as ledger:
            return ledger.expand(reference.ledger_reference)
