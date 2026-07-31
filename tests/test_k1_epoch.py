"""Contract tests for sealed K1 evidence epochs and access audits."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from laconic.k1 import epoch as epoch_module
from laconic.k1.epoch import (
    EpochError,
    HoldoutAccessDenied,
    create_epoch,
    read_access_audit,
    read_epoch,
    record_redesign_access,
    verify_epoch,
)
from laconic.k1.manifest import Candidate, Manifest, Split, source_sha256, write_manifest


def _candidate(private: Path, candidate_id: str, *, split: Split) -> Candidate:
    source = private / f"{candidate_id}.jsonl"
    source.write_text(f'{{"body":"private transcript {candidate_id}"}}\n', encoding="utf-8")
    project = "acme/redesign" if split == "redesign" else "acme/holdout"
    lineage = "issue-42" if split == "redesign" else "issue-43"
    return Candidate(
        candidate_id=candidate_id,
        source_path=source.resolve(),
        source_sha256=source_sha256(source),
        provider="claude-code",
        model="claude-sonnet-4-6",
        model_family="claude-4",
        project=project,
        timestamp="2026-07-31T11:00:00Z",
        session_length=12,
        message_count=8,
        has_code=True,
        tool_density=0.5,
        time_period="2026-Q3",
        session_size_band="small",
        selection_stratum="claude-code|claude-4|2026-Q3|small",
        lineage=lineage,
        eligibility_disposition="unreviewed",
        split=split,
    )


def _create_epoch(tmp_path: Path) -> tuple[Path, Path, Path, Candidate, Candidate]:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    redesign = _candidate(private, "redesign-a", split="redesign")
    holdout = _candidate(private, "holdout-a", split="holdout")
    manifest_path = private / "manifest.json"
    write_manifest(manifest_path, Manifest((holdout, redesign)))
    epoch_path = private / "epoch.json"
    audit_path = private / "access-audit.json"
    create_epoch(
        manifest_path,
        epoch_path,
        audit_path=audit_path,
        approved_roots=(private,),
        epoch_id="k1-20260731",
        created_at="2026-07-31T11:00:00Z",
    )
    return private, manifest_path, epoch_path, redesign, holdout


def test_epoch_seals_frozen_manifest_with_empty_private_audit(tmp_path: Path) -> None:
    private, manifest_path, epoch_path, _, _ = _create_epoch(tmp_path)

    epoch = verify_epoch(epoch_path, manifest_path)
    audit = read_access_audit(epoch.audit_path)

    assert audit.records == ()
    assert audit.head_digest == epoch.digest
    assert stat.S_IMODE(epoch_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(epoch.audit_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    serialized = epoch_path.read_text(encoding="utf-8") + epoch.audit_path.read_text(
        encoding="utf-8"
    )
    assert "private transcript" not in serialized


def test_epoch_creation_and_verification_never_open_holdout_content(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    redesign = _candidate(private, "redesign-a", split="redesign")
    holdout = _candidate(private, "holdout-a", split="holdout")
    manifest_path = private / "manifest.json"
    write_manifest(manifest_path, Manifest((redesign, holdout)))
    holdout.source_path.unlink()
    epoch_path = private / "epoch.json"

    create_epoch(
        manifest_path,
        epoch_path,
        audit_path=private / "access-audit.json",
        approved_roots=(private,),
        epoch_id="k1-20260731",
        created_at="2026-07-31T11:00:00Z",
    )

    assert (
        verify_epoch(epoch_path, manifest_path).manifest_digest
        == Manifest((redesign, holdout)).digest
    )


def test_redesign_access_extends_hash_chain_and_tampering_fails(tmp_path: Path) -> None:
    _, manifest_path, epoch_path, redesign, _ = _create_epoch(tmp_path)

    record = record_redesign_access(
        epoch_path,
        manifest_path,
        redesign.candidate_id,
        "native_extract",
        timestamp="2026-07-31T11:01:00Z",
    )
    audit_path = verify_epoch(epoch_path, manifest_path).audit_path
    audit = read_access_audit(audit_path)
    assert audit.records == (record,)
    assert audit.head_digest == record.digest

    document = json.loads(audit_path.read_text(encoding="utf-8"))
    document["records"][0]["operation"] = "environment_validate"
    audit_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(EpochError, match="record digest mismatch"):
        verify_epoch(epoch_path, manifest_path)


def test_holdout_request_is_rejected_without_audit_record(tmp_path: Path) -> None:
    _, manifest_path, epoch_path, _, holdout = _create_epoch(tmp_path)
    holdout.source_path.unlink()

    with pytest.raises(HoldoutAccessDenied, match="before path open"):
        record_redesign_access(
            epoch_path,
            manifest_path,
            holdout.candidate_id,
            "native_extract",
            timestamp="2026-07-31T11:01:00Z",
        )

    audit_path = verify_epoch(epoch_path, manifest_path).audit_path
    assert read_access_audit(audit_path).records == ()


def test_epoch_rejects_candidate_outside_approved_private_roots(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    source_root = tmp_path / "source"
    source_root.mkdir(mode=0o700)
    os.chmod(source_root, 0o700)
    redesign = _candidate(source_root, "redesign-a", split="redesign")
    holdout = _candidate(private, "holdout-a", split="holdout")
    manifest_path = private / "manifest.json"
    write_manifest(manifest_path, Manifest((redesign, holdout)))

    with pytest.raises(EpochError, match="outside approved roots"):
        create_epoch(
            manifest_path,
            private / "epoch.json",
            audit_path=private / "access-audit.json",
            approved_roots=(private,),
            epoch_id="k1-20260731",
            created_at="2026-07-31T11:00:00Z",
        )


def test_epoch_rejects_private_manifest_outside_approved_roots(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    manifest_root = tmp_path / "manifest-root"
    manifest_root.mkdir(mode=0o700)
    os.chmod(manifest_root, 0o700)
    redesign = _candidate(private, "redesign-a", split="redesign")
    holdout = _candidate(private, "holdout-a", split="holdout")
    manifest_path = manifest_root / "manifest.json"
    write_manifest(manifest_path, Manifest((redesign, holdout)))

    with pytest.raises(
        EpochError,
        match="manifest_path must be within approved private roots",
    ):
        create_epoch(
            manifest_path,
            private / "epoch.json",
            audit_path=private / "access-audit.json",
            approved_roots=(private,),
            epoch_id="k1-20260731",
            created_at="2026-07-31T11:00:00Z",
        )


def test_holdout_access_uses_only_the_epoch_authenticated_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manifest_path, epoch_path, _, holdout = _create_epoch(tmp_path)
    original = epoch_module.read_manifest
    calls = 0

    def sealed_manifest_only(path: Path) -> Manifest:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("holdout gate must not reread an unauthenticated manifest")
        return original(path)

    holdout.source_path.unlink()
    monkeypatch.setattr(epoch_module, "read_manifest", sealed_manifest_only)

    with pytest.raises(HoldoutAccessDenied):
        record_redesign_access(
            epoch_path,
            manifest_path,
            holdout.candidate_id,
            "native_extract",
            timestamp="2026-07-31T11:01:00Z",
        )

    assert calls == 1


def test_epoch_creation_never_resets_an_existing_audit(tmp_path: Path) -> None:
    private, manifest_path, epoch_path, redesign, _ = _create_epoch(tmp_path)
    record_redesign_access(
        epoch_path,
        manifest_path,
        redesign.candidate_id,
        "native_extract",
        timestamp="2026-07-31T11:01:00Z",
    )
    audit_path = verify_epoch(epoch_path, manifest_path).audit_path
    original_audit = audit_path.read_bytes()

    with pytest.raises(EpochError, match="refusing to overwrite"):
        create_epoch(
            manifest_path,
            epoch_path,
            audit_path=audit_path,
            approved_roots=(private,),
            epoch_id="k1-20260731",
            created_at="2026-07-31T11:00:00Z",
        )

    assert audit_path.read_bytes() == original_audit
    assert read_access_audit(audit_path).records


def test_audit_rejects_non_ascii_digest_as_tampering(tmp_path: Path) -> None:
    _, manifest_path, epoch_path, redesign, _ = _create_epoch(tmp_path)
    record_redesign_access(
        epoch_path,
        manifest_path,
        redesign.candidate_id,
        "native_extract",
        timestamp="2026-07-31T11:01:00Z",
    )
    audit_path = verify_epoch(epoch_path, manifest_path).audit_path
    document = json.loads(audit_path.read_text(encoding="utf-8"))
    document["records"][0]["digest"] = "ü" * 64
    audit_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(EpochError, match="digest must be 64 lowercase hex"):
        read_access_audit(audit_path)


def test_epoch_and_audit_reject_boolean_schema_versions(tmp_path: Path) -> None:
    _, manifest_path, epoch_path, redesign, _ = _create_epoch(tmp_path)
    epoch_document = json.loads(epoch_path.read_text(encoding="utf-8"))
    epoch_document["schema_version"] = True
    epoch_path.write_text(json.dumps(epoch_document), encoding="utf-8")

    with pytest.raises(EpochError, match="unsupported epoch schema_version"):
        read_epoch(epoch_path)

    audit_tmp_path = tmp_path / "audit"
    audit_tmp_path.mkdir()
    _, manifest_path, epoch_path, redesign, _ = _create_epoch(audit_tmp_path)
    record_redesign_access(
        epoch_path,
        manifest_path,
        redesign.candidate_id,
        "native_extract",
        timestamp="2026-07-31T11:01:00Z",
    )
    audit_path = verify_epoch(epoch_path, manifest_path).audit_path
    audit_document = json.loads(audit_path.read_text(encoding="utf-8"))
    audit_document["schema_version"] = True
    audit_path.write_text(json.dumps(audit_document), encoding="utf-8")

    with pytest.raises(EpochError, match="unsupported audit schema_version"):
        read_access_audit(audit_path)


def test_epoch_rejects_nonprivate_nested_artifact_directory(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    redesign = _candidate(private, "redesign-a", split="redesign")
    holdout = _candidate(private, "holdout-a", split="holdout")
    manifest_path = private / "manifest.json"
    write_manifest(manifest_path, Manifest((redesign, holdout)))
    nested = private / "nested"
    nested.mkdir(mode=0o755)
    os.chmod(nested, 0o755)

    with pytest.raises(EpochError, match="private directory must have mode 0700"):
        create_epoch(
            manifest_path,
            nested / "epoch.json",
            audit_path=nested / "access-audit.json",
            approved_roots=(private,),
            epoch_id="k1-20260731",
            created_at="2026-07-31T11:00:00Z",
        )
