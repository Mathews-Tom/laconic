"""Contract tests for K1's metadata-only manifest boundary."""

from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from laconic.cli import EXIT_K1_MANIFEST, EXIT_OK, main
from laconic.k1.manifest import (
    Candidate,
    Manifest,
    ManifestError,
    Split,
    read_manifest,
    source_sha256,
    verify_manifest,
    write_manifest,
)


def _candidate(
    tmp_path: Path,
    candidate_id: str,
    *,
    project: str = "acme/api",
    lineage: str = "issue-42",
    provider: str = "claude-code",
    split: Split = "redesign",
) -> Candidate:
    source = tmp_path / f"{candidate_id}.jsonl"
    source.write_text(f'{{"session":"{candidate_id}"}}\n', encoding="utf-8")
    return Candidate(
        candidate_id=candidate_id,
        source_path=source.resolve(),
        source_sha256=source_sha256(source),
        provider=provider,
        model="claude-sonnet-4-6",
        model_family="claude-4",
        project=project,
        timestamp="2026-07-30T17:00:00Z",
        session_length=12,
        message_count=8,
        has_code=True,
        tool_density=0.5,
        time_period="2026-Q3",
        session_size_band="small",
        selection_stratum=f"{provider}|claude-4|2026-Q3|small",
        lineage=lineage,
        eligibility_disposition="unreviewed",
        split=split,
    )


def test_write_read_and_verify_static_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / ".laconic" / "k1" / "manifest.json"
    first = _candidate(tmp_path, "session-b", split="holdout")
    second = _candidate(tmp_path, "session-a")
    manifest = Manifest((first, second))

    write_manifest(manifest_path, manifest)

    loaded = read_manifest(manifest_path)
    assert [candidate.candidate_id for candidate in loaded.candidates] == ["session-a", "session-b"]
    assert verify_manifest(manifest_path).digest == manifest.digest
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "body" not in json.dumps(document)
    assert document["digest"] == manifest.digest


def test_digest_rejects_tampered_metadata(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, Manifest((_candidate(tmp_path, "session-a"),)))
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["candidates"][0]["split"] = "holdout"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ManifestError, match="digest mismatch"):
        read_manifest(manifest_path)


def test_verify_rejects_source_hash_mismatch(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    candidate = _candidate(tmp_path, "session-a")
    write_manifest(manifest_path, Manifest((candidate,)))
    candidate.source_path.write_text('{"session":"changed"}\n', encoding="utf-8")

    with pytest.raises(ManifestError, match="source hash mismatch"):
        verify_manifest(manifest_path)


def test_reader_rejects_transcript_content_field(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, Manifest((_candidate(tmp_path, "session-a"),)))
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["candidates"][0]["body"] = "private transcript"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ManifestError, match="unexpected body"):
        read_manifest(manifest_path)


def test_cli_verifies_and_rejects_tampered_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, Manifest((_candidate(tmp_path, "session-a"),)))

    assert main(["k1", "manifest", "verify", "--manifest", str(manifest_path)]) == EXIT_OK
    assert "verified K1 manifest" in capsys.readouterr().out

    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["candidates"][0]["session_length"] = 13
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    assert main(["k1", "manifest", "verify", "--manifest", str(manifest_path)]) == EXIT_K1_MANIFEST
    assert "digest mismatch" in capsys.readouterr().err


def test_manifest_rejects_duplicate_source_hashes(tmp_path: Path) -> None:
    original = _candidate(tmp_path, "session-a")
    copied_path = tmp_path / "copied.jsonl"
    copied_path.write_bytes(original.source_path.read_bytes())
    copied = replace(
        original,
        candidate_id="session-b",
        source_path=copied_path.resolve(),
        project="other/project",
        lineage="other-lineage",
    )

    with pytest.raises(ManifestError, match="duplicate source_sha256"):
        Manifest((original, copied))


def test_reader_rejects_tool_density_outside_float_range(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, Manifest((_candidate(tmp_path, "session-a"),)))
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["candidates"][0]["tool_density"] = 10**400
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    assert main(["k1", "manifest", "verify", "--manifest", str(manifest_path)]) == EXIT_K1_MANIFEST
    assert "outside the float range" in capsys.readouterr().err


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_reader_rejects_non_integer_schema_version(tmp_path: Path, schema_version: object) -> None:
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, Manifest((_candidate(tmp_path, "session-a"),)))
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["schema_version"] = schema_version
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ManifestError, match="unsupported manifest schema_version"):
        read_manifest(manifest_path)


def test_manifest_rejects_stratum_delimiter_injection(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="must not contain"):
        _candidate(tmp_path, "session-a", provider="claude|code")


def test_manifest_writer_restricts_file_permissions(tmp_path: Path) -> None:
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o755)
    private_dir.chmod(0o755)
    manifest_path = private_dir / "manifest.json"
    write_manifest(manifest_path, Manifest((_candidate(tmp_path, "session-a"),)))

    assert stat.S_IMODE(private_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
