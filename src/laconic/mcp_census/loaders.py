"""Read-only OMP and Claude Code MCP result loaders.

Only the in-memory :class:`HostResult` carries source identities and text. The
report layer aggregates and discards them before any artifact is serialized.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from laconic.mcp_census.manifest import CensusManifest, ManifestError, SourceFile, parse_utc

Host = Literal["omp", "claude_code"]
Exclusion = Literal[
    "error",
    "mixed_content",
    "non_text",
    "empty",
    "streaming",
    "malformed",
    "unattributed",
    "unknown_shape",
]

EXCLUSIONS: Final = (
    "error",
    "mixed_content",
    "non_text",
    "empty",
    "streaming",
    "malformed",
    "unattributed",
    "unknown_shape",
)
_MCP_NAME = re.compile(r"^mcp__.+$")


class CensusSourceError(ValueError):
    """A known host result record is malformed or cannot be attributed."""


@dataclass(frozen=True, slots=True)
class HostResult:
    host: Host
    session_id: str | None
    tool_name: str | None
    timestamp: datetime
    text: str | None
    exclusion: Exclusion | None

    @property
    def is_mcp(self) -> bool:
        return isinstance(self.tool_name, str) and _MCP_NAME.fullmatch(self.tool_name) is not None

    @property
    def eligible(self) -> bool:
        return self.session_id is not None and self.text is not None and self.exclusion is None


def _timestamp(record: dict[str, Any], *, path: Path, line_number: int) -> datetime:
    value = record.get("timestamp")
    try:
        return parse_utc(value)
    except ManifestError as error:
        raise CensusSourceError(
            f"{path}:{line_number}: tool result has no valid timestamp"
        ) from error


def _within(timestamp: datetime, manifest: CensusManifest) -> bool:
    return manifest.inclusive_start <= timestamp < manifest.exclusive_end


def _single_text(content: object, *, is_error: bool) -> tuple[str | None, Exclusion | None]:
    if is_error:
        return None, "error"
    if isinstance(content, str):
        return (content, None) if content else (None, "empty")
    if not isinstance(content, list):
        return None, "unknown_shape"
    if len(content) != 1:
        return None, "mixed_content"
    raw_block = content[0]
    if not isinstance(raw_block, dict):
        return None, "malformed"
    block = cast(dict[str, Any], raw_block)
    block_type = block.get("type")
    if block_type == "text":
        text = block.get("text")
        if not isinstance(text, str):
            return None, "malformed"
        return (text, None) if text else (None, "empty")
    if block_type in {"image", "binary"}:
        return None, "non_text"
    if block_type in {"stream", "streaming"}:
        return None, "streaming"
    return None, "unknown_shape"


def _records(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    try:
        with path.open("r", encoding="utf-8", errors="strict") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    continue
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError as error:
                    raise CensusSourceError(
                        f"{path}:{line_number}: malformed JSON record"
                    ) from error
                if not isinstance(record, dict):
                    raise CensusSourceError(f"{path}:{line_number}: record must be an object")
                yield line_number, record
    except UnicodeDecodeError as error:
        raise CensusSourceError(f"{path}: transcript is not valid UTF-8") from error


def _source_path(manifest: CensusManifest, source: SourceFile) -> Path:
    if source.path is None:
        raise CensusSourceError("manifest source paths are not attached")
    candidate = source.path.resolve(strict=True)
    root = manifest.source_roots[source.host]
    if source.path.is_symlink() or (candidate.parent != root and root not in candidate.parents):
        raise CensusSourceError("manifest source path escapes its root")
    return candidate


def _omp_results(path: Path, manifest: CensusManifest) -> Iterator[HostResult]:
    session_id: str | None = None
    for line_number, record in _records(path):
        if record.get("type") == "session":
            value = record.get("id")
            if isinstance(value, str) and value:
                session_id = value
            continue
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "toolResult":
            continue
        timestamp = _timestamp(record, path=path, line_number=line_number)
        if not _within(timestamp, manifest):
            continue
        tool_name = message.get("toolName")
        if not isinstance(tool_name, str) or not tool_name:
            raise CensusSourceError(f"{path}:{line_number}: OMP tool result has no tool name")
        is_error = message.get("isError")
        if not isinstance(is_error, bool):
            raise CensusSourceError(f"{path}:{line_number}: OMP tool result has invalid isError")
        text, exclusion = _single_text(message.get("content"), is_error=is_error)
        if session_id is None:
            exclusion = "unattributed"
            text = None
        yield HostResult("omp", session_id, tool_name, timestamp, text, exclusion)


def _claude_results(path: Path, manifest: CensusManifest) -> Iterator[HostResult]:
    calls: dict[str, str] = {}
    for line_number, record in _records(path):
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if message.get("role") == "assistant":
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_id = block.get("id")
                name = block.get("name")
                if isinstance(tool_id, str) and isinstance(name, str) and tool_id and name:
                    calls[tool_id] = name
            continue
        if message.get("role") != "user":
            continue
        result_blocks = [
            block
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        if not result_blocks:
            continue
        if len(result_blocks) != 1:
            raise CensusSourceError(
                f"{path}:{line_number}: multiple Claude tool results in one record"
            )
        block = result_blocks[0]
        timestamp = _timestamp(record, path=path, line_number=line_number)
        if not _within(timestamp, manifest):
            continue
        tool_id = block.get("tool_use_id")
        tool_name = calls.get(tool_id) if isinstance(tool_id, str) else None
        session = record.get("sessionId")
        if not isinstance(session, str) or not session:
            alternate = record.get("session_id")
            session = alternate if isinstance(alternate, str) and alternate else None
        is_error = block.get("is_error", False)
        if not isinstance(is_error, bool):
            raise CensusSourceError(
                f"{path}:{line_number}: Claude tool result has invalid is_error"
            )
        text, exclusion = _single_text(block.get("content"), is_error=is_error)
        if tool_name is None or session is None:
            exclusion = "unattributed"
            text = None
        yield HostResult("claude_code", session, tool_name, timestamp, text, exclusion)


def iter_results(manifest: CensusManifest) -> Iterator[HostResult]:
    """Yield every frozen in-window host result without retaining source content."""
    for source in manifest.files:
        path = _source_path(manifest, source)
        if source.host == "omp":
            yield from _omp_results(path, manifest)
        elif source.host == "claude_code":
            yield from _claude_results(path, manifest)
        else:
            raise CensusSourceError(f"unsupported manifest host: {source.host}")
