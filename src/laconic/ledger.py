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
import re
import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Literal, cast

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
#: Version 2 added the complete compaction decision. Version 3 adds
#: ``runtime_decisions`` so emitted candidates and pass-through outcomes are
#: reconstructible without reading raw observations.
SCHEMA_VERSION = 3

#: ``docs/system-design.md`` §5.1. ``raw_chars`` and ``encoded_chars`` are
#: stored rather than re-derived so realised compression is reportable without
#: decompressing every row, and a declined compaction leaves an auditable row.
#:
#: ``compactions.applied`` and ``.accepted`` are deliberately distinct:
#: ``accepted`` is the residency manager's verdict that a compaction would
#: pay off; ``applied`` is whether the prefix was actually rewritten inside
#: a live session, which is a later milestone's responsibility. Writing
#: ``applied = accepted`` would claim a cache write happened, and a real
#: bill was paid, when nothing was rewritten — exactly the flattering
#: accounting ``docs/system-design.md`` §9.4 exists to prevent.
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
    projected_turns INTEGER,
    accepted        INTEGER NOT NULL DEFAULT 0,
    applied         INTEGER NOT NULL DEFAULT 0,
    reason          TEXT    NOT NULL,
    PRIMARY KEY (session_id, turn)
);

CREATE TABLE IF NOT EXISTS runtime_decisions (
    session_id    TEXT    NOT NULL,
    sequence      INTEGER NOT NULL,
    request_id    TEXT    NOT NULL,
    tool_name     TEXT    NOT NULL,
    outcome       TEXT    NOT NULL,
    reason        TEXT    NOT NULL,
    candidate_reference TEXT,
    raw_chars     INTEGER NOT NULL,
    visible_chars INTEGER NOT NULL,
    latency_ms    REAL    NOT NULL,
    created_at    REAL    NOT NULL,
    PRIMARY KEY (session_id, sequence),
    UNIQUE (session_id, request_id)
);

CREATE TABLE IF NOT EXISTS runtime_expansions (
    session_id TEXT    NOT NULL,
    request_id TEXT    NOT NULL,
    reference  TEXT    NOT NULL,
    span       INTEGER NOT NULL,
    created_at REAL    NOT NULL,
    PRIMARY KEY (session_id, request_id)
);

"""

#: Columns a version-1 database's ``compactions`` table predates. Added with
#: ``ALTER TABLE`` rather than by recreating the table, so existing rows —
#: none in practice, since version 1 shipped with no writer — survive.
_V2_COMPACTION_COLUMNS = (
    "ALTER TABLE compactions ADD COLUMN projected_turns INTEGER",
    "ALTER TABLE compactions ADD COLUMN accepted INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE compactions ADD COLUMN reason TEXT NOT NULL DEFAULT ''",
)


#: A span reference is ``<handle>:<first>-<last>``, both 1-based, inclusive.
#: ASCII digits only, and bounded: ``\d`` would accept Arabic-Indic digits the
#: ledger never mints, and an unbounded run of them hits CPython's integer
#: conversion limit with a ValueError that is not an ``InvalidSpanError``.
_SPAN = re.compile(r"([0-9]{1,9})-([0-9]{1,9})")


class UnknownHandleError(KeyError):
    """Raised when a reference names a handle this session never minted."""

    def __str__(self) -> str:
        # KeyError renders its argument with repr, which would wrap this
        # message in quotes on its way to a CLI, a log, or an MCP client.
        # Anything but the single-message form keeps KeyError's rendering.
        if len(self.args) != 1:
            return super().__str__()
        return str(self.args[0])


class InvalidSpanError(ValueError):
    """Raised when a span is malformed or reaches past the stored payload."""


def _select_lines(raw: str, span: str, ref: str) -> list[str]:
    """Return the 1-based inclusive line range ``span`` names.

    Lines are the newline-separated pieces of the payload, so joining every
    line back together reproduces the payload exactly, trailing newline and
    all.
    """
    if (match := _SPAN.fullmatch(span)) is None:
        raise InvalidSpanError(f"malformed reference: {ref!r}, expected <handle>:<first>-<last>")
    first, last = int(match[1]), int(match[2])
    lines = raw.split("\n")
    if first < 1 or last < first or last > len(lines):
        raise InvalidSpanError(f"span {first}-{last} is outside 1-{len(lines)} for {ref!r}")
    return lines[first - 1 : last]


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


@dataclass(frozen=True, slots=True)
class TraceRecord:
    """One renderer-visible observation projection without raw payload text."""

    handle: str
    kind: ObservationKind
    subject: str
    raw_chars: int
    turn: int


type RuntimeDecisionOutcome = Literal["emitted", "pass_through"]


@dataclass(frozen=True, slots=True)
class RuntimeDecision:
    """One content-free runtime outcome persisted for audit and metrics."""

    sequence: int
    request_id: str
    tool_name: str
    outcome: RuntimeDecisionOutcome
    reason: str
    candidate_reference: str | None
    raw_chars: int
    visible_chars: int
    latency_ms: float
    created_at: float


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


def _runtime_decision_from(row: sqlite3.Row) -> RuntimeDecision:
    return RuntimeDecision(
        sequence=int(row["sequence"]),
        request_id=str(row["request_id"]),
        tool_name=str(row["tool_name"]),
        outcome=cast("RuntimeDecisionOutcome", str(row["outcome"])),
        reason=str(row["reason"]),
        candidate_reference=(
            None if row["candidate_reference"] is None else str(row["candidate_reference"])
        ),
        raw_chars=int(row["raw_chars"]),
        visible_chars=int(row["visible_chars"]),
        latency_ms=float(row["latency_ms"]),
        created_at=float(row["created_at"]),
    )


class SchemaVersionError(RuntimeError):
    """Raised when a database was written by a newer ledger schema."""


class DuplicateCompactionError(RuntimeError):
    """Raised when a session already logged a compaction decision for a turn.

    One verdict per session and turn: a repeat must not silently overwrite
    an earlier decision's arithmetic.
    """


class DuplicateRuntimeDecisionError(RuntimeError):
    """Raised when a session repeats a runtime sequence or request id."""


class DuplicateRuntimeExpansionError(RuntimeError):
    """Raised when a session repeats an expansion request id."""


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
        # none, so there is no transaction to wrap here. A fresh (version 0)
        # database gets the current, already-final table shape from this
        # alone; only a real version-1 database predates it.
        self._db.executescript(SCHEMA)
        if version == 1:
            self._migrate_v1_compactions()
        self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate_v1_compactions(self) -> None:
        """Add the columns a version-1 ``compactions`` table predates.

        Existing rows are preserved: ``ALTER TABLE ADD COLUMN`` extends every
        row with the column's default rather than rewriting the table.
        """
        with self._db:
            for statement in _V2_COMPACTION_COLUMNS:
                self._db.execute(statement)

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

    def expand(self, ref: str) -> str:
        """Resolve ``F3`` or ``F3:61-94`` back to the raw payload.

        This is the escape hatch that makes every elision safe: it is exposed
        to the model as a tool and to the human as a CLI verb. Every way of
        asking for something that is not there fails loudly, because a
        recovery path that quietly returns less than it was asked for is the
        exact defect the ledger exists to prevent.
        """
        handle, colon, span = ref.partition(":")
        record = self.get(handle)
        if record is None:
            raise UnknownHandleError(f"unknown handle: {handle}")
        if not colon:
            return record.raw
        return "\n".join(_select_lines(record.raw, span, ref))

    def trace_records(self, first_turn: int, last_turn: int) -> tuple[TraceRecord, ...]:
        """Return renderer fields without reading or inflating raw payloads."""
        if first_turn < 0:
            raise ValueError(f"first turn must not be negative: {first_turn}")
        if last_turn < first_turn:
            raise ValueError(f"last turn must be at least first turn: {last_turn} < {first_turn}")
        rows = self._db.execute(
            "SELECT handle, kind, subject, raw_chars, turn FROM observations "
            "WHERE session_id = ? AND turn BETWEEN ? AND ? ORDER BY turn, rowid",
            (self._session, first_turn, last_turn),
        ).fetchall()
        return tuple(
            TraceRecord(
                handle=str(row["handle"]),
                kind=ObservationKind(str(row["kind"])),
                subject=str(row["subject"]),
                raw_chars=int(row["raw_chars"]),
                turn=int(row["turn"]),
            )
            for row in rows
        )

    def record_compaction(
        self,
        turn: int,
        prefix_before: int,
        prefix_after: int,
        breakeven_turns: float,
        projected_turns: int | None,
        accepted: bool,
        reason: str,
        applied: bool = False,
    ) -> None:
        """Append one audit row to the ``compactions`` table.

        Every compaction decision is recorded here, accepted or declined
        (``docs/system-design.md`` §5.1, §9.4): a tool that quietly busts
        your cache to shrink your context raises your bill while reporting a
        saving, so the arithmetic behind every decision — and ``reason`` for
        it — must stay reconstructible later, not only the ones that ran.

        ``accepted`` is the verdict this decision reached; ``applied``
        defaults to ``False`` and is deliberately a separate fact — whether
        the prefix was actually rewritten inside a live session is a later
        milestone's responsibility, and conflating the two would claim a
        real cache write happened when nothing was rewritten.

        One row per session and turn: a second decision recorded for a turn
        already logged raises ``DuplicateCompactionError`` rather than
        silently overwriting the earlier verdict.
        """
        if turn < 0:
            raise ValueError(f"turn must not be negative: {turn}")
        if prefix_before < 0 or prefix_after < 0:
            raise ValueError(
                f"prefix sizes must not be negative: {prefix_before=}, {prefix_after=}"
            )
        if not reason:
            raise ValueError("reason must not be empty")
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO compactions (session_id, turn, prefix_before, prefix_after, "
                    "breakeven_turns, projected_turns, accepted, applied, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._session,
                        turn,
                        prefix_before,
                        prefix_after,
                        breakeven_turns,
                        projected_turns,
                        int(accepted),
                        int(applied),
                        reason,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise DuplicateCompactionError(
                f"session {self._session!r} already logged a compaction decision for turn {turn}"
            ) from error

    def record_runtime_decision(
        self,
        *,
        sequence: int,
        request_id: str,
        tool_name: str,
        outcome: RuntimeDecisionOutcome,
        reason: str,
        candidate_reference: str | None,
        raw_chars: int,
        visible_chars: int,
        latency_ms: float,
        created_at: float | None = None,
    ) -> RuntimeDecision:
        """Persist one emitted or pass-through outcome without raw content."""
        if sequence < 0:
            raise ValueError(f"sequence must not be negative: {sequence}")
        if not request_id:
            raise ValueError("request_id must not be empty")
        if not tool_name:
            raise ValueError("tool_name must not be empty")
        if outcome not in ("emitted", "pass_through"):
            raise ValueError(f"unsupported runtime outcome: {outcome}")
        if not reason:
            raise ValueError("reason must not be empty")
        if raw_chars < 0 or visible_chars < 0:
            raise ValueError(
                f"character counts must not be negative: {raw_chars=}, {visible_chars=}"
            )
        if latency_ms < 0:
            raise ValueError(f"latency_ms must not be negative: {latency_ms}")
        if outcome == "emitted":
            if candidate_reference is None:
                raise ValueError("an emitted decision requires a candidate reference")
            if visible_chars >= raw_chars:
                raise ValueError("an emitted envelope must be strictly smaller than raw content")
        elif visible_chars != raw_chars:
            raise ValueError("a pass-through decision requires unchanged visible length")
        for field, value in (
            ("request_id", request_id),
            ("tool_name", tool_name),
            ("reason", reason),
        ):
            _require_storable(field, value)
        if candidate_reference is not None:
            _require_storable("candidate_reference", candidate_reference)
        decision = RuntimeDecision(
            sequence=sequence,
            request_id=request_id,
            tool_name=tool_name,
            outcome=outcome,
            reason=reason,
            candidate_reference=candidate_reference,
            raw_chars=raw_chars,
            visible_chars=visible_chars,
            latency_ms=latency_ms,
            created_at=time.time() if created_at is None else created_at,
        )
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO runtime_decisions "
                    "(session_id, sequence, request_id, tool_name, outcome, reason, "
                    "candidate_reference, raw_chars, visible_chars, latency_ms, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._session,
                        decision.sequence,
                        decision.request_id,
                        decision.tool_name,
                        decision.outcome,
                        decision.reason,
                        decision.candidate_reference,
                        decision.raw_chars,
                        decision.visible_chars,
                        decision.latency_ms,
                        decision.created_at,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise DuplicateRuntimeDecisionError(
                f"session {self._session!r} already recorded sequence {sequence} "
                f"or request {request_id!r}"
            ) from error
        return decision

    def runtime_decisions(self) -> tuple[RuntimeDecision, ...]:
        """Return this session's content-free outcomes in sequence order."""
        rows = self._db.execute(
            "SELECT * FROM runtime_decisions WHERE session_id = ? ORDER BY sequence",
            (self._session,),
        ).fetchall()
        return tuple(_runtime_decision_from(row) for row in rows)

    def record_runtime_expansion(
        self,
        *,
        request_id: str,
        reference: str,
        span: bool,
        created_at: float | None = None,
    ) -> None:
        """Persist one content-free full or span expansion event."""
        if not request_id:
            raise ValueError("request_id must not be empty")
        if not reference:
            raise ValueError("reference must not be empty")
        _require_storable("request_id", request_id)
        _require_storable("reference", reference)
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO runtime_expansions "
                    "(session_id, request_id, reference, span, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        self._session,
                        request_id,
                        reference,
                        int(span),
                        time.time() if created_at is None else created_at,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise DuplicateRuntimeExpansionError(
                f"session {self._session!r} already recorded expansion request {request_id!r}"
            ) from error

    def runtime_expansion_counts(self) -> tuple[int, int]:
        """Return persisted full and span expansion counts for this session."""
        row = self._db.execute(
            "SELECT count(*) - sum(span), sum(span) FROM runtime_expansions WHERE session_id = ?",
            (self._session,),
        ).fetchone()
        full = 0 if row[0] is None else int(row[0])
        spans = 0 if row[1] is None else int(row[1])
        return full, spans

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
