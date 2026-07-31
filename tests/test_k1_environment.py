"""Tests for K1's fail-closed tool-environment gate."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from laconic.cli import EXIT_K1_MANIFEST, EXIT_OK, main
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
from laconic.k1.environment_ledger import (
    EnvironmentLedger,
    EnvironmentLedgerError,
    EnvironmentRecord,
    assess_environments,
    read_environment_ledger,
    write_environment_ledger,
)
from laconic.k1.epoch import create_epoch, read_epoch
from laconic.k1.manifest import Candidate, Manifest, source_sha256, write_manifest


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


def test_environment_cli_verifies_private_non_content_admission_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path, manifest, private = _confirmatory_manifest(tmp_path)
    epoch_path = _seal_epoch(manifest_path, private)
    ledger_path = private / "environment.json"
    build_exit = main(
        [
            "k1",
            "environment",
            "build",
            "--epoch",
            str(epoch_path),
            "--manifest",
            str(manifest_path),
            "--ledger",
            str(ledger_path),
        ]
    )
    assert build_exit == EXIT_OK
    capsys.readouterr()

    exit_code = main(
        [
            "k1",
            "environment",
            "verify",
            "--epoch",
            str(epoch_path),
            "--manifest",
            str(manifest_path),
            "--ledger",
            str(ledger_path),
        ]
    )

    assert exit_code == EXIT_OK
    assert "valid=1, unsupported=0, unavailable=0" in capsys.readouterr().out
    assert str(manifest.candidates[0].source_path) not in ledger_path.read_text(encoding="utf-8")


def test_environment_build_marks_nonfinite_tool_payload_unsupported(tmp_path: Path) -> None:
    manifest_path, manifest, private = _confirmatory_manifest(tmp_path)
    source = manifest.candidates[0].source_path
    source.write_text(
        source.read_text(encoding="utf-8").replace('"text": "print(\'ok\')"', '"text": NaN'),
        encoding="utf-8",
    )
    rebuilt = Manifest(
        (
            replace(manifest.candidates[0], source_sha256=source_sha256(source)),
            manifest.candidates[1],
        )
    )
    write_manifest(manifest_path, rebuilt)
    epoch_path = _seal_epoch(manifest_path, private)

    ledger = assess_environments(epoch_path, manifest_path)

    assert ledger.records[0].status == "unsupported"
    assert ledger.records[0].reason == "unsupported_tool"


def test_environment_cli_revalidates_snapshot_receipts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path, manifest, private = _confirmatory_manifest(tmp_path)
    epoch_path = _seal_epoch(manifest_path, private)
    root = _immutable_snapshot(tmp_path)
    ledger_path = private / "environment.json"
    ledger = EnvironmentLedger(
        read_epoch(epoch_path).digest,
        manifest.digest,
        "redesign",
        tuple(
            EnvironmentRecord(
                candidate.candidate_id,
                candidate.source_sha256,
                "valid",
                "snapshot",
                snapshot_tree_sha256(root),
                "snapshot_validated",
                str(root),
            )
            for candidate in manifest.candidates
            if candidate.split == "redesign"
        ),
    )
    write_environment_ledger(ledger_path, ledger)

    assert (
        main(
            [
                "k1",
                "environment",
                "verify",
                "--epoch",
                str(epoch_path),
                "--manifest",
                str(manifest_path),
                "--ledger",
                str(ledger_path),
            ]
        )
        == EXIT_OK
    )

    root.chmod(0o700)
    (root / "src" / "main.py").chmod(0o600)
    (root / "src").chmod(0o700)
    (root / "src" / "main.py").write_text("changed\n", encoding="utf-8")

    assert (
        main(
            [
                "k1",
                "environment",
                "verify",
                "--epoch",
                str(epoch_path),
                "--manifest",
                str(manifest_path),
                "--ledger",
                str(ledger_path),
            ]
        )
        == EXIT_K1_MANIFEST
    )
    assert "cannot revalidate snapshot environment" in capsys.readouterr().err


def test_environment_verification_rejects_confirmatory_candidate_without_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path, manifest, private = _confirmatory_manifest(tmp_path)
    epoch_path = _seal_epoch(manifest_path, private)
    ledger_path = private / "environment.json"
    ledger = EnvironmentLedger(
        read_epoch(epoch_path).digest,
        manifest.digest,
        "redesign",
        tuple(
            EnvironmentRecord(
                candidate.candidate_id,
                candidate.source_sha256,
                "unavailable",
                None,
                None,
                "environment_unavailable",
            )
            for candidate in manifest.candidates
            if candidate.split == "redesign"
        ),
    )
    write_environment_ledger(ledger_path, ledger)

    exit_code = main(
        [
            "k1",
            "environment",
            "verify",
            "--epoch",
            str(epoch_path),
            "--manifest",
            str(manifest_path),
            "--ledger",
            str(ledger_path),
        ]
    )

    assert exit_code == EXIT_K1_MANIFEST
    assert "lacks valid environment" in capsys.readouterr().err


def test_environment_ledger_rejects_tampering_and_insecure_paths(tmp_path: Path) -> None:
    manifest_path, manifest, private = _confirmatory_manifest(tmp_path)
    ledger_path = private / "environment.json"
    snapshot_root = str(_immutable_snapshot(tmp_path))
    ledger = EnvironmentLedger(
        "a" * 64,
        manifest.digest,
        "redesign",
        tuple(
            EnvironmentRecord(
                candidate.candidate_id,
                candidate.source_sha256,
                "valid",
                "snapshot",
                "a" * 64,
                "snapshot_validated",
                snapshot_root,
            )
            for candidate in manifest.candidates
            if candidate.split == "redesign"
        ),
    )
    write_environment_ledger(ledger_path, ledger)
    document = json.loads(ledger_path.read_text(encoding="utf-8"))
    document["digest"] = "b" * 64
    ledger_path.write_text(json.dumps(document), encoding="utf-8")
    ledger_path.chmod(0o600)

    with pytest.raises(EnvironmentLedgerError, match="digest mismatch"):
        read_environment_ledger(ledger_path)

    write_environment_ledger(ledger_path, ledger)
    ledger_path.chmod(0o644)
    with pytest.raises(EnvironmentLedgerError, match="mode 0600"):
        read_environment_ledger(ledger_path)

    ledger_path.chmod(0o600)
    private.chmod(0o755)
    with pytest.raises(EnvironmentLedgerError, match="directory must have mode 0700"):
        read_environment_ledger(ledger_path)


def test_environment_ledger_rejects_symlink(tmp_path: Path) -> None:
    manifest_path, manifest, private = _confirmatory_manifest(tmp_path)
    ledger_path = private / "environment.json"
    snapshot_root = str(_immutable_snapshot(tmp_path))
    ledger = EnvironmentLedger(
        "a" * 64,
        manifest.digest,
        "redesign",
        tuple(
            EnvironmentRecord(
                candidate.candidate_id,
                candidate.source_sha256,
                "valid",
                "snapshot",
                "a" * 64,
                "snapshot_validated",
                snapshot_root,
            )
            for candidate in manifest.candidates
            if candidate.split == "redesign"
        ),
    )
    write_environment_ledger(ledger_path, ledger)
    target = tmp_path / "target.json"
    target.write_text(ledger_path.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o600)
    link = private / "linked.json"
    link.symlink_to(target)

    with pytest.raises(EnvironmentLedgerError, match="non-symlink"):
        read_environment_ledger(link)


def test_environment_ledger_rejects_content_bearing_reason() -> None:
    with pytest.raises(EnvironmentLedgerError, match="reason"):
        EnvironmentRecord(
            "candidate",
            "a" * 64,
            "unsupported",
            None,
            None,
            "historical output",
        )


def _confirmatory_manifest(tmp_path: Path) -> tuple[Path, Manifest, Path]:
    private = tmp_path / "private"
    sources = private / "sources"
    sources.mkdir(parents=True, mode=0o700)
    candidates: list[Candidate] = []
    for candidate_id, split in (("redesign", "redesign"), ("holdout", "holdout")):
        source = sources / f"{candidate_id}.jsonl"
        records = [
            {
                "timestamp": "2026-07-30T21:00:00Z",
                "type": "user",
                "message": {"role": "user", "content": f"Inspect {candidate_id}."},
            },
            {
                "timestamp": "2026-07-30T21:00:01Z",
                "message": {
                    "id": f"message-{candidate_id}",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-1",
                            "name": "Read",
                            "input": {"path": "src/app.py"},
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-07-30T21:00:02Z",
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-1",
                            "content": {"text": "print('ok')"},
                        }
                    ],
                },
            },
        ]
        source.write_text(
            "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
            encoding="utf-8",
        )
        candidates.append(
            Candidate(
                candidate_id=candidate_id,
                source_path=source.resolve(),
                source_sha256=source_sha256(source),
                provider="claude-code",
                model="claude-sonnet-4-6",
                model_family="claude-4",
                project=f"project-{candidate_id}",
                timestamp="2026-07-30T21:00:00Z",
                session_length=1,
                message_count=1,
                has_code=True,
                tool_density=1.0,
                time_period="2026-Q3",
                session_size_band="small",
                selection_stratum="claude-code|claude-4|2026-Q3|small",
                lineage=f"lineage-{candidate_id}",
                eligibility_disposition="confirmatory",
                split=split,
            )
        )
    manifest = Manifest(tuple(candidates))
    manifest_path = private / "manifest.json"
    write_manifest(manifest_path, manifest)
    return manifest_path, manifest, private


def _seal_epoch(manifest_path: Path, private: Path) -> Path:
    epoch_path = private / "epoch.json"
    create_epoch(
        manifest_path,
        epoch_path,
        audit_path=private / "access-audit.json",
        approved_roots=(private,),
        epoch_id="k1-test-environment",
        created_at="2026-07-31T11:00:00Z",
    )
    return epoch_path
