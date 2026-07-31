"""Tests for private K1 eligibility ledger validation."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from laconic.cli import EXIT_K1_MANIFEST, EXIT_OK, main
from laconic.k1.eligibility import (
    EligibilityLedger,
    EligibilityLedgerError,
    EligibilityRecord,
    read_eligibility_ledger,
    write_eligibility_ledger,
)
from laconic.k1.epoch import create_epoch, read_access_audit
from laconic.k1.manifest import Candidate, Manifest, Split, source_sha256, write_manifest

_DIGEST = "a" * 64


def _ledger() -> EligibilityLedger:
    return EligibilityLedger(
        _DIGEST,
        _DIGEST,
        "redesign",
        (
            EligibilityRecord(
                "session-a",
                "b" * 64,
                "confirmatory",
                "native evidence satisfies K1 confirmatory contract",
                "claude-code-jsonl-v1",
                "claude-sonnet-4-6",
                3,
            ),
        ),
    )


def test_private_ledger_round_trip_enforces_permissions(tmp_path: Path) -> None:
    path = tmp_path / "private" / "nested" / "eligibility.json"

    write_eligibility_ledger(path, _ledger())

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.parent.parent.stat().st_mode & 0o777 == 0o700
    assert read_eligibility_ledger(path) == _ledger()


def test_private_ledger_rejects_nonprivate_parent(tmp_path: Path) -> None:
    path = tmp_path / "eligibility.json"
    os.chmod(tmp_path, 0o755)

    with pytest.raises(EligibilityLedgerError, match="directory must have mode 0700"):
        write_eligibility_ledger(path, _ledger())


def test_confirmatory_record_requires_complete_evidence() -> None:
    with pytest.raises(EligibilityLedgerError, match="requires positive event_count"):
        EligibilityRecord(
            "session-a",
            "b" * 64,
            "confirmatory",
            "incomplete",
            "claude-code-jsonl-v1",
            "claude-sonnet-4-6",
            None,
        )


def test_eligibility_cli_builds_and_rechecks_confirmatory_sources(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    manifest_path = private_dir / "manifest.json"
    ledger_path = private_dir / "eligibility.json"
    epoch_path = private_dir / "epoch.json"
    candidate_specs: tuple[tuple[str, str, str, Split], ...] = (
        ("session-a", "acme/redesign", "issue-42", "redesign"),
        ("session-b", "acme/holdout", "issue-43", "holdout"),
    )
    candidates = tuple(
        _confirmatory_candidate(
            tmp_path,
            candidate_id,
            project=project,
            lineage=lineage,
            split=split,
        )
        for candidate_id, project, lineage, split in candidate_specs
    )
    write_manifest(manifest_path, Manifest(candidates))
    create_epoch(
        manifest_path,
        epoch_path,
        audit_path=private_dir / "access-audit.json",
        approved_roots=(private_dir,),
        epoch_id="k1-test-eligibility",
        created_at="2026-07-31T11:00:00Z",
    )
    build_args = [
        "k1",
        "eligibility",
        "build",
        "--epoch",
        str(epoch_path),
        "--manifest",
        str(manifest_path),
        "--ledger",
        str(ledger_path),
    ]
    verify_args = [
        "k1",
        "eligibility",
        "verify",
        "--epoch",
        str(epoch_path),
        "--manifest",
        str(manifest_path),
        "--ledger",
        str(ledger_path),
    ]

    assert main(build_args) == EXIT_OK
    assert "confirmatory=1" in capsys.readouterr().out
    audit = read_access_audit(private_dir / "access-audit.json")
    assert [record.candidate_id for record in audit.records] == ["session-a"]
    assert main(verify_args) == EXIT_OK

    candidates[0].source_path.write_text('{"changed":true}\n', encoding="utf-8")

    assert main(verify_args) == EXIT_K1_MANIFEST
    assert "confirmatory evidence no longer extracts" in capsys.readouterr().err


def _confirmatory_candidate(
    tmp_path: Path,
    candidate_id: str,
    *,
    project: str,
    lineage: str,
    split: Split,
) -> Candidate:
    source_path = tmp_path / "private" / f"{candidate_id}.jsonl"
    source_path.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {
                    "type": "user",
                    "timestamp": "2026-07-30T17:00:00Z",
                    "message": {"role": "user", "content": "Inspect the code."},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-07-30T17:00:01Z",
                    "message": {
                        "id": f"{candidate_id}-assistant",
                        "role": "assistant",
                        "model": "claude-sonnet-4-6",
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "cache_read_input_tokens": 10,
                            "cache_creation_input_tokens": 0,
                        },
                        "content": [{"type": "text", "text": "I will inspect it."}],
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return Candidate(
        candidate_id=candidate_id,
        source_path=source_path.resolve(),
        source_sha256=source_sha256(source_path),
        provider="claude-code",
        model="claude-sonnet-4-6",
        model_family="claude-4",
        project=project,
        timestamp="2026-07-30T17:00:00Z",
        session_length=2,
        message_count=2,
        has_code=True,
        tool_density=0.0,
        time_period="2026-Q3",
        session_size_band="small",
        selection_stratum="claude-code|claude-4|2026-Q3|small",
        lineage=lineage,
        eligibility_disposition="unreviewed",
        split=split,
    )
