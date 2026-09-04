"""Runtime storage containment, permissions, and reopen behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from laconic.ledger import ObservationKind
from laconic.runtime import storage as storage_module
from laconic.runtime.storage import (
    DATA_DIR_ENV_VAR,
    PrivateStorageUnavailableError,
    RuntimeStorage,
    default_data_dir,
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
