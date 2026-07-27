"""Content-addressed store of every observation the codec has elided.

The ledger upholds the design's central invariant: compression is lossy in
presentation, lossless in reach. Anything an encoder removes from what the
model sees stays addressable here, so every elision is reversible.

This module owns the SQLite schema from ``docs/system-design.md`` §5.1, the
zstd codec that keeps a heavy session's raw payloads on disk, and the
connection lifecycle. Raw payloads round-trip through UTF-8 with
``surrogatepass`` rather than a lossy error handler: recoverability means the
exact registered text comes back, including code points no strict encoder
would accept.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType

import zstandard

#: Raw text is stored as UTF-8. ``surrogatepass`` makes ``str`` -> ``bytes``
#: -> ``str`` a total bijection, so no payload is silently mangled on the way
#: in and no two distinct payloads collapse onto one encoding.
RAW_ENCODING = "utf-8"
RAW_ERRORS = "surrogatepass"

#: Content hashes are truncated to 16 hex characters, per the design. That is
#: 64 bits of collision resistance over one session's observations.
SHA_PREFIX_CHARS = 16

#: zstd level 3 is the library default: strong ratio on text, and cheap enough
#: to sit on the observation path. Fixed here because the stored bytes must be
#: reproducible for identical input.
COMPRESSION_LEVEL = 3

#: Stamped into ``PRAGMA user_version`` so a database written by a later,
#: structurally different schema is refused instead of silently half-matched.
SCHEMA_VERSION = 1

#: ``docs/system-design.md`` §5.1. ``raw_chars`` and ``encoded_chars`` are
#: stored rather than re-derived so realised compression is reportable without
#: decompressing every row, and a declined compaction leaves an auditable row.
SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    session_id    TEXT    NOT NULL,
    handle        TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    subject       TEXT    NOT NULL,
    content_sha   TEXT    NOT NULL,
    raw           BLOB    NOT NULL,
    encoded       TEXT    NOT NULL,
    raw_chars     INTEGER NOT NULL,
    encoded_chars INTEGER NOT NULL,
    turn          INTEGER NOT NULL,
    resident      INTEGER NOT NULL DEFAULT 1,
    created_at    REAL    NOT NULL,
    PRIMARY KEY (session_id, handle)
);

CREATE INDEX IF NOT EXISTS obs_dedup ON observations (session_id, subject, content_sha);
CREATE INDEX IF NOT EXISTS obs_resident ON observations (session_id, resident, turn);

CREATE TABLE IF NOT EXISTS compactions (
    session_id      TEXT    NOT NULL,
    turn            INTEGER NOT NULL,
    prefix_before   INTEGER NOT NULL,
    prefix_after    INTEGER NOT NULL,
    breakeven_turns REAL    NOT NULL,
    applied         INTEGER NOT NULL,
    PRIMARY KEY (session_id, turn)
);
"""


class ObservationKind(StrEnum):
    """The shape of an observation, and the letter its handles carry."""

    FILE = "F"
    COMMAND = "B"
    SEARCH = "S"
    FETCH = "W"
    OTHER = "X"


@dataclass(frozen=True, slots=True)
class Record:
    """One observation, stored whole, surfaced partially."""

    handle: str
    kind: ObservationKind
    subject: str
    content_sha: str
    raw: str
    encoded: str
    created_at: float
    turn: int
    resident: bool

    @property
    def raw_chars(self) -> int:
        return len(self.raw)

    @property
    def encoded_chars(self) -> int:
        return len(self.encoded)


def compress_raw(raw: str) -> bytes:
    """Compress a raw payload for storage."""
    return zstandard.ZstdCompressor(level=COMPRESSION_LEVEL).compress(
        raw.encode(RAW_ENCODING, RAW_ERRORS)
    )


def decompress_raw(blob: bytes) -> str:
    """Recover the exact payload ``compress_raw`` was given."""
    return zstandard.ZstdDecompressor().decompress(blob).decode(RAW_ENCODING, RAW_ERRORS)


def _require_storable(field: str, value: str) -> None:
    """Reject text SQLite cannot hold, naming the field that carries it."""
    try:
        value.encode(RAW_ENCODING)
    except UnicodeEncodeError as error:
        raise ValueError(f"{field} is not UTF-8 encodable: {value!r}") from error


def content_sha(raw: str) -> str:
    """Hash a raw payload over exactly the bytes that get stored."""
    digest = hashlib.sha256(raw.encode(RAW_ENCODING, RAW_ERRORS)).hexdigest()
    return digest[:SHA_PREFIX_CHARS]


def _record_from(row: sqlite3.Row) -> Record:
    return Record(
        handle=row["handle"],
        kind=ObservationKind(row["kind"]),
        subject=row["subject"],
        content_sha=row["content_sha"],
        raw=decompress_raw(row["raw"]),
        encoded=row["encoded"],
        created_at=row["created_at"],
        turn=row["turn"],
        resident=bool(row["resident"]),
    )


class SchemaVersionError(RuntimeError):
    """Raised when a database was written by a newer ledger schema."""


class Ledger:
    """Content-addressed store of observations, one session per instance.

    Several sessions may share a database file; every read and write is scoped
    to ``session_id``. One writer per session, though: two live instances on
    the same session mint handles from independent counters and collide.
    """

    def __init__(self, db_path: Path | str, session_id: str) -> None:
        if not session_id:
            raise ValueError("session_id must not be empty")
        self._path = Path(db_path)
        if self._path.parent != Path():
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._path)
        try:
            self._db.row_factory = sqlite3.Row
            self._session = session_id
            self._init_schema()
            self._counters = self._recover_counters()
        except BaseException:
            # Nothing else can close this connection: a raising __init__
            # returns no object, so the caller never gets a ``close`` to call
            # and ``with`` never reaches __exit__.
            self._db.close()
            raise

    def _init_schema(self) -> None:
        (version,) = self._db.execute("PRAGMA user_version").fetchone()
        if version > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"{self._path} was written by ledger schema {version}, "
                f"and this build understands {SCHEMA_VERSION}"
            )
        # ``executescript`` commits any pending transaction and DDL opens
        # none, so there is no transaction to wrap here.
        self._db.executescript(SCHEMA)
        self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _recover_counters(self) -> dict[ObservationKind, int]:
        """Resume handle numbering from what this session already stored.

        Counters live in the database rather than only in memory: reopening a
        session must not re-mint ``F1`` over the row that already owns it.
        """
        rows = self._db.execute(
            "SELECT kind, MAX(CAST(SUBSTR(handle, 2) AS INTEGER)) AS ordinal "
            "FROM observations WHERE session_id = ? GROUP BY kind",
            (self._session,),
        ).fetchall()
        return {ObservationKind(row["kind"]): int(row["ordinal"]) for row in rows}

    def register(
        self,
        kind: ObservationKind,
        subject: str,
        raw: str,
        encoded: str,
        turn: int,
    ) -> Record:
        """Store an observation and mint a handle for it.

        A re-observation of byte-identical content under the same subject
        reuses the existing record rather than minting a second handle, so the
        model's earlier reference keeps resolving and the prefix stays stable.
        The first encoding of a payload is the one that stands; re-registering
        it with different ``encoded`` text does not rewrite history.

        Dedup is keyed on ``(subject, content_sha)`` alone, matching the
        ``obs_dedup`` index: byte-identical content re-observed under the same
        subject but a different ``kind`` reuses the original record, and so
        the original kind. Subjects are per-kind in practice — a path, a
        command line, a query — so this costs nothing real and keeps one
        payload to one handle.

        ``raw`` accepts any ``str``. ``subject`` and ``encoded`` are SQL
        ``TEXT`` and must be UTF-8 encodable; a lone surrogate in either is
        rejected here rather than deeper in the driver.
        """
        if turn < 0:
            raise ValueError(f"turn must not be negative: {turn}")
        _require_storable("subject", subject)
        _require_storable("encoded", encoded)
        sha = content_sha(raw)
        if (existing := self._find(subject, sha)) is not None:
            return existing

        ordinal = self._counters.get(kind, 0) + 1
        record = Record(
            handle=f"{kind.value}{ordinal}",
            kind=kind,
            subject=subject,
            content_sha=sha,
            raw=raw,
            encoded=encoded,
            created_at=time.time(),
            turn=turn,
            resident=True,
        )
        self._insert(record)
        self._counters[kind] = ordinal
        return record

    def get(self, handle: str) -> Record | None:
        """Return the record ``handle`` names, or ``None`` when unknown."""
        row = self._db.execute(
            "SELECT * FROM observations WHERE session_id = ? AND handle = ?",
            (self._session, handle),
        ).fetchone()
        return None if row is None else _record_from(row)

    def _find(self, subject: str, sha: str) -> Record | None:
        row = self._db.execute(
            "SELECT * FROM observations "
            "WHERE session_id = ? AND subject = ? AND content_sha = ? "
            # rowid is insertion order, so the first handle minted for this
            # content wins. created_at is wall clock and can step backwards.
            "ORDER BY rowid LIMIT 1",
            (self._session, subject, sha),
        ).fetchone()
        return None if row is None else _record_from(row)

    def _insert(self, record: Record) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO observations (session_id, handle, kind, subject, content_sha, "
                "raw, encoded, raw_chars, encoded_chars, turn, resident, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._session,
                    record.handle,
                    record.kind.value,
                    record.subject,
                    record.content_sha,
                    compress_raw(record.raw),
                    record.encoded,
                    record.raw_chars,
                    record.encoded_chars,
                    record.turn,
                    int(record.resident),
                    record.created_at,
                ),
            )

    def close(self) -> None:
        """Release the database connection. Idempotent."""
        self._db.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
