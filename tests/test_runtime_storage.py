"""Runtime storage containment, permissions, and reopen behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from laconic.ledger import ObservationKind
from laconic.runtime import storage as storage_module
from laconic.runtime.operator import runtime_storage_status
from laconic.runtime.references import InvalidSessionIdError
from laconic.runtime.storage import (
    DATA_DIR_ENV_VAR,
    PrivateStorageUnavailableError,
    RuntimeStorage,
    UnsafeStoragePathError,
    default_data_dir,
    session_lock_path,
)


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_environment_override_selects_the_runtime_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DATA_DIR_ENV_VAR, str(tmp_path / "override"))
    assert default_data_dir() == tmp_path / "override"


def test_session_ids_never_become_path_components(tmp_path: Path) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    path = storage.ledger_path("omp-session.with-safe_chars-42")

    assert path.parent == storage.root / "sessions"
    assert path.name.endswith(".sqlite3")
    assert "omp-session" not in path.name


def test_runtime_storage_is_owner_only(tmp_path: Path) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    with storage.open_ledger("session-1"):
        pass

    assert _mode(storage.root) == 0o700
    assert _mode(storage.root / "sessions") == 0o700
    assert _mode(storage.ledger_path("session-1")) == 0o600


def test_runtime_storage_fails_closed_when_owner_only_access_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(storage_module, "_owner_only_storage_supported", lambda: False)

    with pytest.raises(PrivateStorageUnavailableError, match="owner-only"):
        RuntimeStorage(data_dir)

    assert not data_dir.exists()


def test_reopening_a_session_preserves_handles_and_exact_content(tmp_path: Path) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    raw = "line one\nline two\nline three"
    with storage.open_ledger("session-1") as ledger:
        first = ledger.register(ObservationKind.FILE, "a.py", raw, "encoded", 1)

    with storage.open_ledger("session-1") as reopened:
        second = reopened.register(ObservationKind.FILE, "b.py", "other", "encoded", 2)

    assert (first.handle, second.handle) == ("F1", "F2")
    assert storage.expand("session-1/F1:2-3") == "line two\nline three"


def test_session_lock_sidecars_are_opaque_private_and_persistent(tmp_path: Path) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    session_id = "opaque-lock-session"
    path = session_lock_path(session_id, storage.root)

    with storage.session_lock(session_id):
        assert path.exists()
        inode = path.stat().st_ino

    with storage.session_lock(session_id):
        assert path.stat().st_ino == inode

    assert path.parent == storage.root / "locks"
    assert session_id not in path.name
    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600
    status = runtime_storage_status(storage.root)
    assert (status.sessions, status.storage_bytes) == (0, 0)


def test_session_lock_rejects_invalid_identifiers_without_creating_sidecars(tmp_path: Path) -> None:
    storage = RuntimeStorage(tmp_path / "data")

    with pytest.raises(InvalidSessionIdError):
        with storage.session_lock("not/a-session"):
            pass

    assert not (storage.root / "locks").exists()


def test_session_lock_rejects_symlinked_lock_directory(tmp_path: Path) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    outside = tmp_path / "outside"
    outside.mkdir()
    (storage.root / "locks").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeStoragePathError):
        with storage.session_lock("session-1"):
            pass


def test_session_lock_rejects_nonordinary_sidecars(tmp_path: Path) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    path = storage.lock_path("session-1")
    path.mkdir()

    with pytest.raises(UnsafeStoragePathError):
        with storage.session_lock("session-1"):
            pass


def test_session_lock_fails_closed_when_owner_only_storage_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    monkeypatch.setattr(storage_module, "_owner_only_storage_supported", lambda: False)

    with pytest.raises(PrivateStorageUnavailableError, match="owner-only"):
        with storage.session_lock("session-1"):
            pass
