"""Fail-closed environment contracts for K1 contemporary replay."""

from __future__ import annotations

import hashlib
import os
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from laconic.k1.manifest import is_sha256

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
