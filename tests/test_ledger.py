"""Ledger storage: schema shape, connection lifecycle, and the raw codec."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import laconic.ledger
from laconic.ledger import (
    SCHEMA_VERSION,
    Ledger,
    ObservationKind,
    SchemaVersionError,
    compress_raw,
    content_sha,
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


def test_register_mints_a_kind_prefixed_handle(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        record = ledger.register(ObservationKind.FILE, "src/auth.py", "body", "F1 outline", 1)
    assert record.handle == "F1"


def test_handles_stay_short_and_typeable(tmp_path: Path) -> None:
    """The model has to write these back, so they must stay terse."""
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        handles = [
            ledger.register(kind, f"subject-{index}", f"raw-{index}", "enc", 1).handle
            for index, kind in enumerate(ObservationKind)
        ]
    assert handles == ["F1", "B1", "S1", "W1", "X1"]
    assert all(re.fullmatch(r"[FBSWX]\d+", handle) for handle in handles)


def test_each_kind_numbers_independently(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        ledger.register(ObservationKind.FILE, "a.py", "a", "enc", 1)
        command = ledger.register(ObservationKind.COMMAND, "pytest -q", "out", "enc", 1)
        second_file = ledger.register(ObservationKind.FILE, "b.py", "b", "enc", 2)
    assert (command.handle, second_file.handle) == ("B1", "F2")


def test_get_returns_the_registered_record(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        registered = ledger.register(ObservationKind.FILE, "src/auth.py", "line\n" * 3, "F1", 7)
        assert ledger.get("F1") == registered


def test_get_returns_none_for_an_unknown_handle(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        assert ledger.get("F99") is None


def test_identical_content_under_the_same_subject_reuses_the_handle(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        first = ledger.register(ObservationKind.FILE, "src/auth.py", "same bytes", "enc", 1)
        second = ledger.register(ObservationKind.FILE, "src/auth.py", "same bytes", "enc", 4)
    assert second == first


def test_dedup_returns_the_first_row_minted_for_the_content(tmp_path: Path) -> None:
    """Insertion order decides, not the wall clock, which can step backwards.

    register alone cannot produce two rows for one (subject, content_sha) --
    it dedups first -- so the duplicate is written directly.
    """
    db_path = tmp_path / "ledger.db"
    with Ledger(db_path, "s1") as ledger:
        first = ledger.register(ObservationKind.FILE, "a.py", "body", "enc", 1)
    with inspect(db_path) as db, db:
        db.execute(
            "INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "s1",
                "F2",
                "F",
                "a.py",
                first.content_sha,
                compress_raw("body"),
                "enc",
                4,
                3,
                2,
                1,
                first.created_at - 100,
            ),
        )
    with Ledger(db_path, "s1") as reopened:
        reused = reopened.register(ObservationKind.FILE, "a.py", "body", "enc", 5)
    assert reused.handle == "F1"


def test_a_reused_record_keeps_its_kind(tmp_path: Path) -> None:
    """Dedup ignores kind by design, so the first observation's kind stands."""
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        first = ledger.register(ObservationKind.FILE, "shared", "same bytes", "enc", 1)
        second = ledger.register(ObservationKind.COMMAND, "shared", "same bytes", "enc", 2)
    assert (second.handle, second.kind) == (first.handle, ObservationKind.FILE)


def test_a_subject_sqlite_cannot_store_is_rejected_by_name(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        with pytest.raises(ValueError, match="subject is not UTF-8 encodable"):
            ledger.register(ObservationKind.FILE, "\ud800.py", "body", "enc", 1)


def test_an_encoding_sqlite_cannot_store_is_rejected_by_name(tmp_path: Path) -> None:
    """The encoder derives this from raw, which may legitimately hold surrogates."""
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        with pytest.raises(ValueError, match="encoded is not UTF-8 encodable"):
            ledger.register(ObservationKind.FILE, "a.py", "body", "\ud800", 1)


def test_a_rejected_registration_leaves_no_trace(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    with Ledger(db_path, "s1") as ledger:
        with pytest.raises(ValueError):
            ledger.register(ObservationKind.FILE, "\ud800.py", "body", "enc", 1)
        assert ledger.register(ObservationKind.FILE, "a.py", "body", "enc", 1).handle == "F1"
    with inspect(db_path) as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone() == (1,)


def test_a_payload_sqlite_cannot_store_as_text_is_still_accepted(tmp_path: Path) -> None:
    """raw is a compressed blob, so it stays total where the TEXT columns are not."""
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        record = ledger.register(ObservationKind.FILE, "a.py", "\ud800", "enc", 1)
        assert ledger.get(record.handle) == record


def test_a_reused_record_keeps_its_original_encoding(tmp_path: Path) -> None:
    """Rewriting a resident encoding would change the prefix and bust the cache."""
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        ledger.register(ObservationKind.FILE, "src/auth.py", "same bytes", "first encoding", 1)
        reused = ledger.register(ObservationKind.FILE, "src/auth.py", "same bytes", "second", 2)
    assert reused.encoded == "first encoding"


def test_changed_content_under_the_same_subject_mints_a_new_handle(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        first = ledger.register(ObservationKind.FILE, "src/auth.py", "before", "enc", 1)
        second = ledger.register(ObservationKind.FILE, "src/auth.py", "after", "enc", 2)
    assert (first.handle, second.handle) == ("F1", "F2")


def test_identical_content_under_a_different_subject_mints_a_new_handle(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        first = ledger.register(ObservationKind.FILE, "a.py", "shared", "enc", 1)
        second = ledger.register(ObservationKind.FILE, "b.py", "shared", "enc", 1)
    assert (first.handle, second.handle) == ("F1", "F2")


def test_the_content_hash_is_a_truncated_sha256() -> None:
    raw = "some observation body"
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    assert content_sha(raw) == expected


def test_sessions_are_isolated_in_a_shared_database(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    with Ledger(db_path, "s1") as first, Ledger(db_path, "s2") as second:
        first.register(ObservationKind.FILE, "a.py", "first content", "enc", 1)
        second.register(ObservationKind.FILE, "a.py", "second content", "enc", 1)
        assert first.get("F1") is not None
        assert first.get("F1") != second.get("F1")


def test_handle_numbering_resumes_after_reopening_a_session(tmp_path: Path) -> None:
    """An in-memory counter would re-mint F1 over the row that already owns it."""
    db_path = tmp_path / "ledger.db"
    with Ledger(db_path, "s1") as ledger:
        ledger.register(ObservationKind.FILE, "a.py", "a", "enc", 1)
        ledger.register(ObservationKind.FILE, "b.py", "b", "enc", 1)
    with Ledger(db_path, "s1") as reopened:
        resumed = reopened.register(ObservationKind.FILE, "c.py", "c", "enc", 2)
        assert resumed.handle == "F3"
        assert reopened.get("F1") is not None


def test_a_reopened_session_recovers_the_exact_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    raw = "def f():\n    return \u00e9\n"
    with Ledger(db_path, "s1") as ledger:
        ledger.register(ObservationKind.FILE, "a.py", raw, "F1 outline", 1)
    with Ledger(db_path, "s1") as reopened:
        record = reopened.get("F1")
    assert record is not None
    assert record.raw == raw


def test_a_negative_turn_is_rejected(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        with pytest.raises(ValueError, match="turn"):
            ledger.register(ObservationKind.FILE, "a.py", "a", "enc", -1)


def test_a_registered_record_is_resident(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db", "s1") as ledger:
        assert ledger.register(ObservationKind.FILE, "a.py", "a", "enc", 1).resident


def test_the_stored_row_records_both_character_counts(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    with Ledger(db_path, "s1") as ledger:
        ledger.register(ObservationKind.FILE, "a.py", "x" * 40, "y" * 7, 1)
    with inspect(db_path) as db:
        row = db.execute("SELECT raw_chars, encoded_chars FROM observations").fetchone()
    assert row == (40, 7)


def test_the_raw_column_holds_compressed_bytes(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    raw = "repeated line\n" * 400
    with Ledger(db_path, "s1") as ledger:
        ledger.register(ObservationKind.FILE, "a.py", raw, "enc", 1)
    with inspect(db_path) as db:
        (blob,) = db.execute("SELECT raw FROM observations").fetchone()
    assert isinstance(blob, bytes)
    assert decompress_raw(blob) == raw
    assert len(blob) < len(raw)
