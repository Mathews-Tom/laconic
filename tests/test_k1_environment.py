"""Tests for K1's fail-closed tool-environment gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from laconic.k1.environment import (
    EnvironmentError,
    RecordedToolObservation,
    RecordedToolResolver,
    SnapshotEnvironment,
    SnapshotToolResolver,
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


def test_recorded_resolver_serves_only_the_exact_next_call() -> None:
    resolver = RecordedToolResolver(
        (
            RecordedToolObservation("Read", {"path": "/workspace/a.py"}, {"text": "one"}),
            RecordedToolObservation("Read", {"path": "/workspace/b.py"}, {"text": "two"}),
        )
    )

    first = resolver.resolve("Read", {"path": "/workspace/a.py"})

    assert first.status == "resolved"
    assert first.output == {"text": "one"}
    assert resolver.position == 1
    assert not resolver.terminated


def test_recorded_resolver_mismatch_terminates_without_advancing() -> None:
    resolver = RecordedToolResolver(
        (RecordedToolObservation("Read", {"path": "/workspace/a.py"}, {"text": "one"}),)
    )

    mismatch = resolver.resolve("Read", {"path": "/workspace/other.py"})
    after_mismatch = resolver.resolve("Read", {"path": "/workspace/a.py"})

    assert mismatch.status == "unsupported"
    assert mismatch.output is None
    assert resolver.position == 0
    assert resolver.terminated
    assert after_mismatch.status == "unsupported"
    assert after_mismatch.output is None


def test_recorded_resolver_rejects_json_near_matches_and_exhaustion() -> None:
    resolver = RecordedToolResolver((RecordedToolObservation("Search", {"limit": 1}, None),))

    near_match = resolver.resolve("Search", {"limit": True})
    assert near_match.status == "unsupported"
    assert resolver.position == 0

    resolver = RecordedToolResolver((RecordedToolObservation("Search", {"limit": 1}, None),))
    resolved = resolver.resolve("Search", {"limit": 1})
    exhausted = resolver.resolve("Search", {"limit": 1})

    assert resolved.status == "resolved"
    assert resolved.output is None
    assert exhausted.status == "unsupported"


def test_recorded_resolver_empty_trace_fails_closed() -> None:
    resolver = RecordedToolResolver(())

    result = resolver.resolve("Read", {"path": "src/main.py"})

    assert result.status == "unsupported"
    assert result.output is None
    assert resolver.position == 0
    assert resolver.terminated


def test_recorded_resolver_rejects_non_json_number() -> None:
    with pytest.raises(EnvironmentError, match="valid JSON"):
        RecordedToolObservation("Search", {"limit": float("nan")}, None)


def test_recorded_resolver_returns_independent_recorded_output() -> None:
    original_output = {"lines": ["one"]}
    resolver = RecordedToolResolver(
        (RecordedToolObservation("Read", {"path": "/workspace/a.py"}, original_output),)
    )

    result = resolver.resolve("Read", {"path": "/workspace/a.py"})
    assert isinstance(result.output, dict)
    result.output["lines"] = ["changed"]

    assert original_output == {"lines": ["one"]}


def test_snapshot_resolver_reads_only_contained_immutable_files(tmp_path: Path) -> None:
    root = _immutable_snapshot(tmp_path)
    resolver = SnapshotToolResolver(SnapshotEnvironment(root, snapshot_tree_sha256(root)))

    read = resolver.resolve("Read", {"path": "src/main.py"})
    escaped = resolver.resolve("Read", {"path": "../outside.txt"})

    assert read.status == "resolved"
    assert read.output == "print('safe')\n"
    assert escaped.status == "unsupported"
    assert escaped.output is None
    assert resolver.terminated


def test_snapshot_resolver_stops_when_snapshot_digest_changes(tmp_path: Path) -> None:
    root = _immutable_snapshot(tmp_path)
    resolver = SnapshotToolResolver(SnapshotEnvironment(root, snapshot_tree_sha256(root)))
    source = root / "src" / "main.py"
    source.chmod(0o600)
    source.write_text("print('changed')\n", encoding="utf-8")
    source.chmod(0o400)

    result = resolver.resolve("Read", {"path": "src/main.py"})

    assert result.status == "unsupported"
    assert "digest" in result.reason
    assert resolver.terminated


def test_snapshot_resolver_rejects_command_tools_without_execution(tmp_path: Path) -> None:
    root = _immutable_snapshot(tmp_path)
    resolver = SnapshotToolResolver(SnapshotEnvironment(root, snapshot_tree_sha256(root)))

    result = resolver.resolve("Git", {"argv": ["show", "--output", "/tmp/outside", "HEAD"]})

    assert result.status == "unsupported"
    assert result.output is None
    assert resolver.terminated


def test_snapshot_resolver_rejects_oversized_read(tmp_path: Path) -> None:
    root = _immutable_snapshot(tmp_path)
    source = root / "src"
    source.chmod(0o700)
    large = source / "large.txt"
    large.write_bytes(b"x" * (1024 * 1024 + 1))
    large.chmod(0o400)
    source.chmod(0o500)
    resolver = SnapshotToolResolver(SnapshotEnvironment(root, snapshot_tree_sha256(root)))

    result = resolver.resolve("Read", {"path": "src/large.txt"})

    assert result.status == "unsupported"
    assert "output limit" in result.reason
    assert resolver.terminated
