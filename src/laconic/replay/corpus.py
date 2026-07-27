"""Session transcript ingest and channel attribution.

A corpus is a directory tree of newline-delimited JSON session transcripts
(``*.jsonl``) as emitted by Claude Code and compatible agent harnesses. Ingest
decomposes every record into the four channels that make up a context window:

    tool results  -- observations the agent reads back
    tool_use args -- actions the agent emits
    prose         -- human-facing explanatory text, fenced code removed
    user prompts  -- what the human typed
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from laconic.costs import ModelUsage, session_cost

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type Record = dict[str, JsonValue]

TRANSCRIPT_GLOB = "*.jsonl"

FENCED_CODE = re.compile(r"```.*?```", re.S)

#: ``ModelUsage.add_turn`` keyword -> transcript ``usage`` key.
_TOKEN_FIELDS = {
    "input_tokens": "input_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
    "output_tokens": "output_tokens",
}


class EmptyCorpusError(RuntimeError):
    """Raised when a corpus path holds no usable session transcripts."""


class MalformedRecordError(ValueError):
    """Raised when a record's token counters cannot be read as written.

    A mistyped counter is not a partial reading: counting the turn with a zero
    token total would understate the very spend this module measures, and
    dropping the record would corrupt the channel decomposition around it. The
    scan stops and names the record instead.
    """


@dataclass(slots=True)
class Channels:
    """Character volume per communication channel."""

    prose: int = 0
    fenced_code_in_prose: int = 0
    tool_args: int = 0
    tool_results: int = 0
    user_prompts: int = 0
    prose_per_turn: list[int] = field(default_factory=list)
    result_chars_by_tool: Counter[str] = field(default_factory=Counter)
    calls_by_tool: Counter[str] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        """Characters entering the context window across all four channels."""
        return self.tool_results + self.tool_args + self.prose + self.user_prompts

    @property
    def emitted(self) -> int:
        """Characters the model emitted: prose, fenced code inside it, actions."""
        return self.prose + self.fenced_code_in_prose + self.tool_args


@dataclass(frozen=True, slots=True)
class CorpusScan:
    """Everything one pass over a corpus produced."""

    channels: Channels
    usage: dict[str, ModelUsage]
    transcripts: int
    records: int
    malformed_lines: int


def find_transcripts(roots: Sequence[Path]) -> list[Path]:
    """Return every transcript under ``roots`` in a stable order.

    Ordering is sorted rather than filesystem order so that a scan of the same
    corpus is byte-identical across machines.

    Raises:
        EmptyCorpusError: if a root does not exist. Measuring the rest of a
            mistyped path list would silently report a smaller corpus than the
            caller asked for.
    """
    found: set[Path] = set()
    for root in roots:
        if root.is_file():
            found.add(root)
            continue
        if not root.is_dir():
            raise EmptyCorpusError(f"corpus path does not exist: {root}")
        found.update(root.rglob(TRANSCRIPT_GLOB))
    return sorted(found)


def iter_records(path: Path) -> Iterator[tuple[int, Record | None]]:
    """Yield ``(line number, record)`` for each line; ``None`` if malformed.

    The line number is the real one-based line in the file, so a diagnostic can
    be followed to the source. Malformed lines are surfaced rather than skipped
    silently: a transcript still being written ends in a partial line, but a
    systematically unparseable corpus is a defect the caller must be able to
    see.
    """
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed: JsonValue = json.loads(stripped)
            except json.JSONDecodeError:
                yield line_number, None
                continue
            yield line_number, (parsed if isinstance(parsed, dict) else None)


def scan(paths: Sequence[Path]) -> CorpusScan:
    """Decompose every transcript in ``paths`` into channels and token usage."""
    channels = Channels()
    usage: dict[str, ModelUsage] = {}
    records = 0
    malformed = 0
    # tool_use id -> tool name, so tool_result volume can be attributed.
    tool_name_by_id: dict[str, str] = {}

    for path in paths:
        for line_number, record in iter_records(path):
            if record is None:
                malformed += 1
                continue
            records += 1
            message = _as_dict(record.get("message"))
            kind = _as_str(record.get("type"))
            if kind == "assistant":
                _ingest_assistant(
                    message, channels, usage, tool_name_by_id, f"{path}:{line_number}"
                )
            elif kind == "user":
                _ingest_user(message, channels, tool_name_by_id)

    return CorpusScan(
        channels=channels,
        usage=usage,
        transcripts=len(paths),
        records=records,
        malformed_lines=malformed,
    )


def scan_corpus(roots: Sequence[Path]) -> CorpusScan:
    """Scan every transcript under ``roots``.

    Raises:
        EmptyCorpusError: if no transcript is found, if the transcripts hold no
            assistant usage records, if they record no billable tokens, or if
            no channel accumulated content. Every one of those would otherwise
            reach the reporter as a division by zero.
    """
    paths = find_transcripts(roots)
    if not paths:
        listed = ", ".join(str(root) for root in roots)
        raise EmptyCorpusError(f"no {TRANSCRIPT_GLOB} transcripts found under {listed}")
    result = scan(paths)
    if not result.usage:
        raise EmptyCorpusError(f"{len(paths)} transcript(s) contained no assistant usage records")
    if session_cost(result.usage).total <= 0.0:
        raise EmptyCorpusError(f"{len(paths)} transcript(s) recorded no billable tokens")
    if result.channels.total <= 0 or result.channels.emitted <= 0:
        raise EmptyCorpusError(
            f"{len(paths)} transcript(s) contained no channel content to apportion"
        )
    return result


def _ingest_assistant(
    message: dict[str, JsonValue],
    channels: Channels,
    usage: dict[str, ModelUsage],
    tool_name_by_id: dict[str, str],
    origin: str,
) -> None:
    """Fold one assistant record into the channels and the token usage.

    Raises:
        MalformedRecordError: if the ``usage`` block carries a counter that is
            not an integer.
    """
    raw_usage = message.get("usage")
    if raw_usage is None:
        return
    if not isinstance(raw_usage, dict):
        raise MalformedRecordError(f"{origin}: usage is not an object: {raw_usage!r}")
    tokens = raw_usage
    if not tokens:
        return
    counts: dict[str, int] = {}
    for field_name, key in _TOKEN_FIELDS.items():
        count = _token_count(tokens.get(key))
        if count is None:
            raise MalformedRecordError(f"{origin}: {key} is not an integer: {tokens.get(key)!r}")
        counts[field_name] = count
    model = _as_str(message.get("model")) or "unknown"
    usage[model] = usage.get(model, ModelUsage()).add_turn(**counts)

    turn_prose = 0
    for block in _as_list(message.get("content")):
        if not isinstance(block, dict):
            continue
        block_type = _as_str(block.get("type"))
        if block_type == "text":
            text = _as_str(block.get("text"))
            stripped = FENCED_CODE.sub("", text)
            channels.fenced_code_in_prose += len(text) - len(stripped)
            channels.prose += len(stripped)
            turn_prose += len(stripped)
        elif block_type == "tool_use":
            name = _as_str(block.get("name")) or "unknown"
            tool_name_by_id[_as_str(block.get("id"))] = name
            channels.tool_args += len(_dumps(block.get("input", {})))
            channels.calls_by_tool[name] += 1
    channels.prose_per_turn.append(turn_prose)


def _ingest_user(
    message: dict[str, JsonValue],
    channels: Channels,
    tool_name_by_id: dict[str, str],
) -> None:
    content = message.get("content")
    if isinstance(content, str):
        channels.user_prompts += len(content)
        return
    for block in _as_list(content):
        if not isinstance(block, dict) or _as_str(block.get("type")) != "tool_result":
            continue
        body = block.get("content")
        text = body if isinstance(body, str) else _dumps(body)
        channels.tool_results += len(text)
        name = tool_name_by_id.get(_as_str(block.get("tool_use_id")), "?")
        channels.result_chars_by_tool[name] += len(text)


def _dumps(value: JsonValue) -> str:
    """Serialise ``value`` for measurement.

    ``ensure_ascii`` is off deliberately: the model sees the character, not its
    six-character escape, and escaping would make a redacted transcript measure
    smaller than the original it stands in for.
    """
    return json.dumps(value, ensure_ascii=False)


def _as_dict(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _as_list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _as_str(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""


def _token_count(value: JsonValue) -> int | None:
    """Return an absent counter as 0 and a present-but-mistyped one as ``None``."""
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
