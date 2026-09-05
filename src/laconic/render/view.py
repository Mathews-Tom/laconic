"""Read-only assembly of a structural trace from ledger observations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from laconic.codec.observe import ObservationCodec, subject_for
from laconic.ledger import Ledger, TraceRecord
from laconic.replay.corpus import (
    EmptyCorpusError,
    JsonValue,
    MalformedRecordError,
    find_transcripts,
    iter_records,
)


@dataclass(frozen=True, slots=True)
class TraceEntry:
    """One structural observation with its one-based display turn."""

    turn: int
    record: TraceRecord


@dataclass(frozen=True, slots=True)
class FixtureLedger:
    """Ephemeral ledger and the transcript it was deterministically derived from."""

    ledger: Ledger
    transcript: Path


@dataclass(frozen=True, slots=True)
class _PendingAction:
    tool_name: str
    tool_input: Mapping[str, JsonValue]
    turn: int


class UnmatchedToolResultError(ValueError):
    """Raised when a transcript result has no structural source action."""


class UnsupportedToolResultError(ValueError):
    """Raised when an observation result cannot be recovered as source text."""


def _result_text(body: JsonValue, transcript: Path, line_number: int) -> str:
    """Recover text while ignoring non-text metadata blocks in result content."""
    if isinstance(body, str):
        return body
    if not isinstance(body, list):
        raise UnsupportedToolResultError(
            f"{transcript}:{line_number}: tool result content is not text"
        )

    text_parts: list[str] = []
    non_text_types: set[str] = set()
    for part in body:
        if not isinstance(part, dict):
            non_text_types.add(type(part).__name__)
            continue
        if part.get("type") != "text":
            block_type = part.get("type")
            non_text_types.add(block_type if isinstance(block_type, str) else "untyped")
            continue
        text = part.get("text")
        if not isinstance(text, str):
            raise UnsupportedToolResultError(
                f"{transcript}:{line_number}: text result block has no text"
            )
        text_parts.append(text)
    if text_parts:
        return "".join(text_parts)
    if non_text_types:
        types = ", ".join(sorted(non_text_types))
        raise UnsupportedToolResultError(
            f"{transcript}:{line_number}: tool result has no text blocks ({types})"
        )
    return ""


def assemble(ledger: Ledger, first_turn: int, last_turn: int) -> tuple[TraceEntry, ...]:
    """Assemble an inclusive, one-based display range without mutating ``ledger``.

    Ledger turns are zero-based because they are recorded as events arrive;
    people request turns as ``1-5`` at the CLI. The conversion lives here so
    every renderer receives a consistently numbered trace.
    """
    if first_turn < 1:
        raise ValueError(f"first turn must be at least 1: {first_turn}")
    if last_turn < first_turn:
        raise ValueError(f"last turn must be at least first turn: {last_turn} < {first_turn}")
    return tuple(
        TraceEntry(turn=record.turn + 1, record=record)
        for record in ledger.trace_records(first_turn - 1, last_turn - 1)
    )


def load_fixture_ledger(paths: Sequence[Path]) -> FixtureLedger:
    """Build one deterministic, in-memory ledger from a selected transcript.

    Fixture corpora have no persisted ledger database. This importer replays
    one sorted source transcript's assistant action/result pairs into an
    isolated ledger solely for a human-facing view; it never writes back to
    either the source corpus or a live session ledger.
    """
    ledger = Ledger(":memory:", "render-fixture")
    try:
        transcript = _populate_fixture_ledger(ledger, paths)
    except BaseException:
        ledger.close()
        raise
    return FixtureLedger(ledger, transcript)


def _populate_fixture_ledger(ledger: Ledger, paths: Sequence[Path]) -> Path:
    """Encode one deterministic fixture session into ``ledger``.

    ``laconic research view --turns`` names turns from one session, never a synthetic
    concatenation of unrelated transcripts. A corpus directory resolves to its
    sorted first baseline transcript; callers needing another session pass that
    transcript path directly.
    """
    transcripts = find_transcripts(list(paths))
    if not transcripts:
        listed = ", ".join(str(path) for path in paths)
        raise EmptyCorpusError(f"no *.jsonl transcripts found under {listed}")
    transcript = transcripts[0]
    codec = ObservationCodec(ledger)
    pending: dict[str, _PendingAction] = {}
    turn = 0
    for line_number, record in iter_records(transcript):
        if record is None:
            raise MalformedRecordError(f"{transcript}:{line_number}: invalid JSON record")
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if record.get("type") == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_id = block.get("id")
                tool_name = block.get("name")
                tool_input = block.get("input")
                if (
                    isinstance(tool_id, str)
                    and isinstance(tool_name, str)
                    and isinstance(tool_input, dict)
                ):
                    pending[tool_id] = _PendingAction(tool_name, tool_input, turn)
            turn += 1
            continue
        if record.get("type") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_id = block.get("tool_use_id")
            action = pending.pop(tool_id, None) if isinstance(tool_id, str) else None
            if action is None:
                raise UnmatchedToolResultError(
                    f"{transcript}:{line_number}: tool result has no matching tool use"
                )
            raw = _result_text(block.get("content"), transcript, line_number)
            codec.encode(
                action.tool_name,
                subject_for(action.tool_input),
                raw,
                action.tool_input,
                turn=action.turn,
            )
    return transcript
