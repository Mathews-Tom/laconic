"""Fail-closed environment contracts for K1 contemporary replay."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from laconic.k1.evidence import JsonValue
from laconic.k1.manifest import is_sha256

_MAX_SNAPSHOT_READ_BYTES: Final = 1024 * 1024

_SNAPSHOT_CHUNK_BYTES: Final = 1024 * 1024


class EnvironmentError(ValueError):
    """Raised when a K1 tool environment is unsafe or cannot be reproduced."""


@dataclass(frozen=True, slots=True)
class SnapshotEnvironment:
    """An immutable directory tree identified by its canonical content digest."""

    root: Path
    tree_sha256: str

    def __post_init__(self) -> None:
        if not self.root.is_absolute():
            raise EnvironmentError("snapshot root must be absolute")
        if not is_sha256(self.tree_sha256):
            raise EnvironmentError("snapshot tree_sha256 must be 64 lowercase hex")


def validate_snapshot(snapshot: SnapshotEnvironment) -> Path:
    """Verify a root is immutable, symlink-free, and matches its declared tree digest."""
    root = snapshot.root
    try:
        root_stat = root.lstat()
    except OSError as error:
        raise EnvironmentError(f"cannot stat snapshot root {root}: {error}") from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise EnvironmentError("snapshot root must be a non-symlink directory")
    resolved_root = root.resolve(strict=True)
    _require_not_writable(root, root_stat)
    actual_digest = snapshot_tree_sha256(resolved_root)
    if actual_digest != snapshot.tree_sha256:
        raise EnvironmentError("snapshot tree digest does not match declared tree_sha256")
    return resolved_root


def snapshot_tree_sha256(root: Path) -> str:
    """Return a deterministic digest for a safe immutable directory tree."""
    try:
        root_stat = root.lstat()
    except OSError as error:
        raise EnvironmentError(f"cannot stat snapshot root {root}: {error}") from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise EnvironmentError("snapshot root must be a non-symlink directory")

    digest = hashlib.sha256()
    _digest_directory(root, root, digest)
    return digest.hexdigest()


def resolve_snapshot_path(root: Path, requested_path: str) -> Path:
    """Resolve one historical path only when its target remains inside root."""
    if not requested_path:
        raise EnvironmentError("requested path must not be empty")
    candidate = Path(requested_path)
    try:
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve(strict=True)
    except OSError as error:
        raise EnvironmentError(f"cannot resolve requested snapshot path: {error}") from error
    if not resolved.is_relative_to(root):
        raise EnvironmentError("requested snapshot path escapes the approved root")
    return resolved


def _digest_directory(root: Path, directory: Path, digest: hashlib._Hash) -> None:
    try:
        directory_stat = directory.lstat()
    except OSError as error:
        raise EnvironmentError(f"cannot stat snapshot directory {directory}: {error}") from error
    _require_not_writable(directory, directory_stat)
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as error:
        raise EnvironmentError(f"cannot list snapshot directory {directory}: {error}") from error
    for entry in entries:
        try:
            entry_stat = entry.lstat()
        except OSError as error:
            raise EnvironmentError(f"cannot stat snapshot entry {entry}: {error}") from error
        _require_not_writable(entry, entry_stat)
        relative = entry.relative_to(root).as_posix().encode("utf-8")
        if stat.S_ISLNK(entry_stat.st_mode):
            raise EnvironmentError(f"snapshot contains symlink {entry}")
        if stat.S_ISDIR(entry_stat.st_mode):
            digest.update(b"D")
            _update_digest_field(digest, relative)
            _digest_directory(root, entry, digest)
            continue
        if not stat.S_ISREG(entry_stat.st_mode):
            raise EnvironmentError(f"snapshot contains unsupported filesystem entry {entry}")
        digest.update(b"F")
        _update_digest_field(digest, relative)
        _update_digest_length(digest, entry_stat.st_size)
        written = 0
        try:
            with entry.open("rb") as stream:
                while chunk := stream.read(_SNAPSHOT_CHUNK_BYTES):
                    written += len(chunk)
                    digest.update(chunk)
        except OSError as error:
            raise EnvironmentError(f"cannot read snapshot entry {entry}: {error}") from error
        if written != entry_stat.st_size:
            raise EnvironmentError(f"snapshot entry changed during hashing: {entry}")


def _update_digest_field(digest: hashlib._Hash, value: bytes) -> None:
    _update_digest_length(digest, len(value))
    digest.update(value)


def _update_digest_length(digest: hashlib._Hash, length: int) -> None:
    digest.update(struct.pack(">Q", length))


def _require_not_writable(path: Path, entry_stat: os.stat_result) -> None:
    if entry_stat.st_mode & 0o222:
        raise EnvironmentError(f"snapshot entry is writable: {path}")


ResolutionStatus = Literal["resolved", "unsupported"]


@dataclass(frozen=True, slots=True)
class ToolResolution:
    """The result of resolving one contemporary tool call."""

    status: ResolutionStatus
    output: JsonValue | None
    reason: str

    def __post_init__(self) -> None:
        if self.status not in {"resolved", "unsupported"}:
            raise EnvironmentError(f"unknown tool resolution status {self.status!r}")
        if not self.reason.strip():
            raise EnvironmentError("tool resolution reason must not be empty")
        if self.status == "unsupported" and self.output is not None:
            raise EnvironmentError("unsupported tool result must not carry an output")


@dataclass(frozen=True, slots=True)
class RecordedToolObservation:
    """One recorded tool action and its recorded result, without inference."""

    name: str
    input: dict[str, JsonValue]
    output: JsonValue
    _input_json: str = field(init=False, repr=False)
    _output_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise EnvironmentError("recorded tool name must not be empty")
        object.__setattr__(self, "_input_json", _canonical_json(self.input))
        object.__setattr__(self, "_output_json", _canonical_json(self.output))


class RecordedToolResolver:
    """Serve a recorded result only for the exact next recorded tool call."""

    def __init__(self, observations: Sequence[RecordedToolObservation]) -> None:
        self._observations = tuple(observations)
        self._position = 0
        self._terminated = False

    @property
    def position(self) -> int:
        """Return the number of exactly matched observations served."""
        return self._position

    @property
    def terminated(self) -> bool:
        """Return whether an unsupported call has terminated this resolver."""
        return self._terminated

    def resolve(self, name: str, tool_input: dict[str, JsonValue]) -> ToolResolution:
        """Resolve one exact call or terminate without advancing on a mismatch."""
        if self._position == len(self._observations):
            self._terminated = True
            return ToolResolution("unsupported", None, "recorded tool trace is exhausted")
        if self._terminated:
            return ToolResolution("unsupported", None, "replay already terminated")
        if not name.strip():
            self._terminated = True
            return ToolResolution("unsupported", None, "tool name is empty")
        try:
            actual_input = _canonical_json(tool_input)
        except EnvironmentError:
            self._terminated = True
            return ToolResolution("unsupported", None, "tool input is not valid JSON")
        expected = self._observations[self._position]
        if name != expected.name or actual_input != expected._input_json:
            self._terminated = True
            return ToolResolution("unsupported", None, "tool call differs from recorded trace")
        self._position += 1
        return ToolResolution(
            "resolved",
            json.loads(expected._output_json),
            "exact recorded tool call",
        )


def _canonical_json(value: JsonValue) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise EnvironmentError("tool payload must be valid JSON") from error


class SnapshotToolResolver:
    """Resolve root-contained read-only tools against an immutable snapshot."""

    def __init__(self, snapshot: SnapshotEnvironment) -> None:
        self._snapshot = snapshot
        self._root = validate_snapshot(snapshot)
        self._terminated = False

    @property
    def terminated(self) -> bool:
        """Return whether a compromised environment or unsupported call stopped replay."""
        return self._terminated

    def resolve(self, name: str, tool_input: dict[str, JsonValue]) -> ToolResolution:
        """Resolve a permitted read-only tool or fail closed as unsupported."""
        if self._terminated:
            return ToolResolution("unsupported", None, "replay already terminated")
        try:
            self._root = validate_snapshot(self._snapshot)
            if name == "Read":
                return _resolve_snapshot_read(self._root, tool_input)
        except EnvironmentError as error:
            self._terminated = True
            return ToolResolution("unsupported", None, str(error))
        self._terminated = True
        return ToolResolution("unsupported", None, "tool is not supported by snapshot environment")


def _resolve_snapshot_read(root: Path, tool_input: dict[str, JsonValue]) -> ToolResolution:
    if set(tool_input) != {"path"} or not isinstance(tool_input["path"], str):
        raise EnvironmentError("Read requires exactly one string path")
    path = resolve_snapshot_path(root, tool_input["path"])
    try:
        entry_stat = path.stat()
    except OSError as error:
        raise EnvironmentError(f"cannot stat snapshot target: {error}") from error
    if not stat.S_ISREG(entry_stat.st_mode):
        raise EnvironmentError("Read target must be a regular file")
    if entry_stat.st_size > _MAX_SNAPSHOT_READ_BYTES:
        raise EnvironmentError("Read target exceeds the snapshot output limit")
    try:
        output = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise EnvironmentError("Read target is not UTF-8 text") from error
    except OSError as error:
        raise EnvironmentError(f"cannot read snapshot target: {error}") from error
    return ToolResolution("resolved", output, "immutable snapshot read")
