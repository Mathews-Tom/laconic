"""Session transcript ingest, channel attribution, and redaction.

A corpus is a directory tree of newline-delimited JSON session transcripts
(``*.jsonl``) as emitted by Claude Code and compatible agent harnesses. Ingest
decomposes every record into the four channels that make up a context window:

    tool results  -- observations the agent reads back
    tool_use args -- actions the agent emits
    prose         -- human-facing explanatory text, fenced code removed
    user prompts  -- what the human typed

Redaction exists because the measurements that motivate Laconic were taken over
private transcripts containing proprietary source. A redacted transcript keeps
every channel size and every structural field, and loses the content.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from laconic.costs import ModelUsage, session_cost

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type Record = dict[str, JsonValue]

TRANSCRIPT_GLOB = "*.jsonl"

FENCED_CODE = re.compile(r"```.*?```", re.S)
_REDACTABLE = re.compile(r"[^\W_]")
_REDACTION_CHAR = "x"

#: Structure the measurement reads, allowlisted per position rather than by
#: name at arbitrary depth: a key called ``name`` inside a tool payload is the
#: tool's own and carries content.
_RECORD_STRUCTURAL_KEYS = frozenset({"type"})
_MESSAGE_STRUCTURAL_KEYS = frozenset({"model"})
_BLOCK_STRUCTURAL_KEYS = frozenset({"type", "name", "id", "tool_use_id"})

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


def redact_text(text: str) -> str:
    """Replace every alphanumeric character with a filler character.

    Punctuation, whitespace, and underscores survive, so the redacted string has
    the same length as the original and the same JSON-escaped length. That is
    what lets a redacted corpus reproduce the original channel sizes exactly.
    """
    return _REDACTABLE.sub(_REDACTION_CHAR, text)


def redact_value(value: JsonValue) -> JsonValue:
    """Redact every string in ``value``, whatever key it sits under.

    No allowlist applies here. Callers use this for opaque payloads — tool
    arguments and tool results — whose key names belong to the tool, not to the
    transcript schema, and where a key called ``name`` or ``id`` is as likely to
    hold a customer record as a structural label.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    return value


def redact_record(record: Record) -> Record:
    """Return ``record`` with content redacted and structure preserved.

    Redaction is positional, not name-based: only the fields the measurement
    reads at the positions it reads them — the record and block ``type``, the
    ``role``, the ``model``, a tool ``name``, and the ids that attribute a tool
    result to its call — survive. Everything else is redacted at every depth, so
    a redacted transcript scans to exactly the same channel sizes and cost as
    the original while carrying none of its values.

    Object *keys* are preserved. A length-preserving key redaction would have to
    be injective — two distinct keys of the same length collapsing to the same
    filler would merge their entries and change the measurement — and no such
    scheme is worth the risk here. Redact a transcript whose payload keys are
    themselves private by hand.
    """
    redacted: Record = {}
    for key, value in record.items():
        if key in _RECORD_STRUCTURAL_KEYS and isinstance(value, str):
            redacted[key] = value
        elif key == "message" and isinstance(value, dict):
            redacted[key] = _redact_message(value)
        else:
            redacted[key] = redact_value(value)
    return redacted


def _redact_message(message: dict[str, JsonValue]) -> dict[str, JsonValue]:
    redacted: dict[str, JsonValue] = {}
    for key, value in message.items():
        if key in _MESSAGE_STRUCTURAL_KEYS and isinstance(value, str):
            redacted[key] = value
        elif key == "content" and isinstance(value, list):
            redacted[key] = [
                _redact_block(item) if isinstance(item, dict) else redact_value(item)
                for item in value
            ]
        else:
            redacted[key] = redact_value(value)
    return redacted


def _redact_block(block: dict[str, JsonValue]) -> dict[str, JsonValue]:
    redacted: dict[str, JsonValue] = {}
    for key, value in block.items():
        if key in _BLOCK_STRUCTURAL_KEYS and isinstance(value, str):
            redacted[key] = value
        else:
            redacted[key] = redact_value(value)
    return redacted


def redact_transcript(source: Path, destination: Path) -> int:
    """Write a redacted copy of ``source`` to ``destination``.

    The redacted records are staged in a sibling temporary file and moved into
    place only after the whole source is read, so a refusal leaves no partial
    artifact next to private data.

    Returns:
        The number of records written.

    Raises:
        ValueError: if ``destination`` is ``source``, which would truncate the
            transcript before it could be read, or if ``source`` holds a line
            that is not a JSON object — copying it through would leak it
            unredacted.
    """
    if source.resolve() == destination.resolve():
        raise ValueError(f"refusing to redact {source} onto itself")
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, staged_name = tempfile.mkstemp(dir=destination.parent, suffix=".redacting")
    staged = Path(staged_name)
    written = 0
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            for line_number, record in iter_records(source):
                if record is None:
                    raise ValueError(
                        f"{source}:{line_number} is not a JSON object; refusing to redact"
                    )
                out.write(_dumps(redact_record(record)))
                out.write("\n")
                written += 1
        staged.replace(destination)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return written


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
