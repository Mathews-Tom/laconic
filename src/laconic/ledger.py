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

import sqlite3
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
            self._session = session_id
            self._init_schema()
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
