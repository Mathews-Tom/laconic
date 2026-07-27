"""Ledger storage: schema shape, connection lifecycle, and the raw codec."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import laconic.ledger
from laconic.ledger import (
    SCHEMA_VERSION,
    Ledger,
    SchemaVersionError,
    compress_raw,
    decompress_raw,
)


@contextmanager
def inspect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Read the database as an outside observer, not through the ledger."""
    db = sqlite3.connect(db_path)
    try:
        yield db
    finally:
        db.close()


def _names(db: sqlite3.Connection, kind: str) -> set[str]:
    rows = db.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,)).fetchall()
    return {name for (name,) in rows}


def _primary_key(db: sqlite3.Connection, table: str) -> list[str]:
    # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk.
    # ``pk`` is the 1-based position in the key, or 0 when not part of it.
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    return [row[1] for row in sorted(rows, key=lambda row: row[5]) if row[5]]


def test_schema_creates_the_designed_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    with Ledger(db_path, "s1"):
        pass
    with inspect(db_path) as db:
        assert {"observations", "compactions"} <= _names(db, "table")


def test_schema_creates_the_dedup_and_residency_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    with Ledger(db_path, "s1"):
        pass
    with inspect(db_path) as db:
        assert {"obs_dedup", "obs_resident"} <= _names(db, "index")


def test_schema_pins_the_observation_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    with Ledger(db_path, "s1"):
        pass
    with inspect(db_path) as db:
        columns = db.execute("SELECT * FROM observations LIMIT 0").description
    assert [column[0] for column in columns] == [
        "session_id",
        "handle",
        "kind",
        "subject",
        "content_sha",
        "raw",
        "encoded",
        "raw_chars",
        "encoded_chars",
        "turn",
        "resident",
        "created_at",
    ]


def test_schema_keys_observations_by_session_and_handle(tmp_path: Path) -> None:
    """Two sessions may carry the same handle; one session may not repeat it."""
    db_path = tmp_path / "ledger.db"
    with Ledger(db_path, "s1"):
        pass
    with inspect(db_path) as db:
        assert _primary_key(db, "observations") == ["session_id", "handle"]


def test_schema_keys_compactions_by_session_and_turn(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    with Ledger(db_path, "s1"):
        pass
    with inspect(db_path) as db:
        assert _primary_key(db, "compactions") == ["session_id", "turn"]


def test_schema_initialisation_preserves_existing_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    with Ledger(db_path, "s1"):
        pass
    with inspect(db_path) as db, db:
        db.execute("INSERT INTO compactions VALUES (?, ?, ?, ?, ?, ?)", ("s1", 1, 100, 40, 3.5, 1))

    with Ledger(db_path, "s1"):
        pass
    with inspect(db_path) as db:
        assert db.execute("SELECT turn FROM compactions").fetchall() == [(1,)]


def test_schema_stamps_its_version(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    with Ledger(db_path, "s1"):
        pass
    with inspect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)


def test_a_database_from_a_newer_schema_is_refused(tmp_path: Path) -> None:
    """Create-if-not-exists would otherwise run against a schema it never made."""
    db_path = tmp_path / "ledger.db"
    with Ledger(db_path, "s1"):
        pass
    with inspect(db_path) as db, db:
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(SchemaVersionError, match=str(SCHEMA_VERSION + 1)):
        Ledger(db_path, "s1")


def _count_connection_closes(monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    """Patch connect so a test can see whether the connection was closed."""
    closes = 0

    class CountingConnection(sqlite3.Connection):
        def close(self) -> None:
            nonlocal closes
            closes += 1
            super().close()

    real_connect = sqlite3.connect
    monkeypatch.setattr(
        laconic.ledger.sqlite3,
        "connect",
        lambda path: real_connect(path, factory=CountingConnection),
    )
    return lambda: closes


def test_a_failed_open_closes_its_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising __init__ returns no object, so nothing else can close it."""
    db_path = tmp_path / "ledger.db"
    with Ledger(db_path, "s1"):
        pass
    with inspect(db_path) as db, db:
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    closes = _count_connection_closes(monkeypatch)
    with pytest.raises(SchemaVersionError):
        Ledger(db_path, "s1")
    assert closes() == 1


def test_an_interrupted_open_closes_its_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C during a reopen is not an Exception, and must still not strand it."""

    def interrupt(self: Ledger) -> None:
        raise KeyboardInterrupt

    closes = _count_connection_closes(monkeypatch)
    monkeypatch.setattr(laconic.ledger.Ledger, "_init_schema", interrupt)
    with pytest.raises(KeyboardInterrupt):
        Ledger(tmp_path / "ledger.db", "s1")
    assert closes() == 1


def test_an_empty_session_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="session_id"):
        Ledger(tmp_path / "ledger.db", "")


def test_a_missing_parent_directory_is_created(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "deeper" / "ledger.db"
    with Ledger(db_path, "s1"):
        pass
    assert db_path.is_file()


def test_a_relative_path_needs_no_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with Ledger("ledger.db", "s1"):
        pass
    assert (tmp_path / "ledger.db").is_file()


def test_closing_releases_the_file(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = Ledger(db_path, "s1")
    ledger.close()
    ledger.close()  # idempotent
    with inspect(db_path) as db, db:
        db.execute("INSERT INTO compactions VALUES (?, ?, ?, ?, ?, ?)", ("s1", 1, 10, 5, 1.0, 0))


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "plain ascii",
        "unicode: \u00e9\u4e2d\U0001f600",
        "trailing newline\n",
        "\r\n mixed \n line \r endings \n",
        "lone surrogate: \ud800",
        "null byte: \x00 inside",
        "x" * 100_000,
    ],
)
def test_raw_payloads_round_trip_exactly(raw: str) -> None:
    assert decompress_raw(compress_raw(raw)) == raw


def test_distinct_payloads_do_not_collapse_onto_the_same_bytes() -> None:
    """A lossy error handler would map both of these onto one replacement."""
    assert compress_raw("\ud800") != compress_raw("\udc00")


def test_compression_shrinks_a_repetitive_payload() -> None:
    raw = "def handler(request):\n    return None\n" * 500
    assert len(compress_raw(raw)) < len(raw.encode("utf-8")) // 10


def test_compression_is_reproducible() -> None:
    raw = "the stored bytes must not drift between calls\n" * 100
    assert compress_raw(raw) == compress_raw(raw)
