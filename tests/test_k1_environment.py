"""Tests for K1's fail-closed tool-environment gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from laconic.k1.environment import (
    EnvironmentError,
    SnapshotEnvironment,
    resolve_snapshot_path,
    snapshot_tree_sha256,
    validate_snapshot,
)


def _immutable_snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "snapshot"
    root.mkdir(mode=0o700)
    source = root / "src"
    source.mkdir(mode=0o700)
    file_path = source / "main.py"
    file_path.write_text("print('safe')\n", encoding="utf-8")
    file_path.chmod(0o400)
    source.chmod(0o500)
    root.chmod(0o500)
    return root


def test_snapshot_validation_accepts_immutable_tree(tmp_path: Path) -> None:
    root = _immutable_snapshot(tmp_path)
    snapshot = SnapshotEnvironment(root, snapshot_tree_sha256(root))

    assert validate_snapshot(snapshot) == root.resolve()


def test_snapshot_validation_rejects_digest_drift(tmp_path: Path) -> None:
    root = _immutable_snapshot(tmp_path)
    snapshot = SnapshotEnvironment(root, snapshot_tree_sha256(root))
    source = root / "src" / "main.py"
    source.chmod(0o600)
    source.write_text("print('changed')\n", encoding="utf-8")
    source.chmod(0o400)

    with pytest.raises(EnvironmentError, match="tree digest"):
        validate_snapshot(snapshot)


def test_snapshot_validation_rejects_writable_entry(tmp_path: Path) -> None:
    root = _immutable_snapshot(tmp_path)
    (root / "src").chmod(0o700)
    snapshot = SnapshotEnvironment(root, "a" * 64)

    with pytest.raises(EnvironmentError, match="writable"):
        validate_snapshot(snapshot)


def test_snapshot_path_resolution_rejects_escape_and_symlink(tmp_path: Path) -> None:
    root = _immutable_snapshot(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    root.chmod(0o700)
    link = root / "link"
    link.symlink_to(outside)
    root.chmod(0o500)

    assert resolve_snapshot_path(root.resolve(), "src/main.py") == (root / "src/main.py").resolve()
    with pytest.raises(EnvironmentError, match="escapes"):
        resolve_snapshot_path(root.resolve(), "../outside.txt")
    with pytest.raises(EnvironmentError, match="escapes"):
        resolve_snapshot_path(root.resolve(), str(link))


def test_snapshot_digest_frames_file_boundaries(tmp_path: Path) -> None:
    first = tmp_path / "first"
    first.mkdir(mode=0o700)
    (first / "a").write_bytes(b"")
    (first / "b").write_bytes(b"hello")
    second = tmp_path / "second"
    second.mkdir(mode=0o700)
    (second / "a").write_bytes(b"F\x00b\x00hello")
    for root in (first, second):
        for entry in root.iterdir():
            entry.chmod(0o400)
        root.chmod(0o500)

    assert snapshot_tree_sha256(first) != snapshot_tree_sha256(second)
