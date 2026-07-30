"""Tests for the optional, metadata-only Searchat manifest adapter."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from laconic.cli import EXIT_K1_MANIFEST, EXIT_OK, main
from laconic.k1.manifest import source_sha256, verify_manifest
from laconic.k1.searchat_export import SearchatExportError, produce_manifest


def _record(
    source: Path, candidate_id: str, *, model_family: str = "claude-4"
) -> dict[str, object]:
    return {
        "conversation_id": candidate_id,
        "eligibility_disposition": "unreviewed",
        "file_hash": source_sha256(source),
        "file_path": str(source),
        "has_code": True,
        "lineage": f"lineage-{candidate_id}",
        "message_count": 8,
        "model": "claude-sonnet-4-6",
        "model_family": model_family,
        "project_id": f"project-{candidate_id}",
        "provider": "claude-code",
        "session_length": 12,
        "session_size_band": "small",
        "time_period": "2026-Q3",
        "timestamp": "2026-07-30T17:00:00Z",
        "tool_density": 0.5,
    }


def _export(tmp_path: Path) -> Path:
    records: list[dict[str, object]] = []
    for candidate_id, model_family in (
        ("a", "claude-4"),
        ("b", "claude-4"),
        ("c", "gpt-4"),
        ("d", "gpt-4"),
    ):
        source = tmp_path / f"{candidate_id}.jsonl"
        source.write_text(f"native transcript body {candidate_id}\n", encoding="utf-8")
        records.append(_record(source, candidate_id, model_family=model_family))
    export_path = tmp_path / "searchat-metadata.json"
    export_path.write_text(json.dumps({"schema_version": 1, "records": records}), encoding="utf-8")
    return export_path


def test_producer_works_without_searchat_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_path = _export(tmp_path)
    manifest_path = tmp_path / ".laconic" / "k1" / "manifest.json"
    monkeypatch.setitem(sys.modules, "searchat", None)

    manifest = produce_manifest(export_path, manifest_path)

    assert verify_manifest(manifest_path).digest == manifest.digest
    rendered = manifest_path.read_text(encoding="utf-8")
    assert "native transcript body" not in rendered
    assert "searchat" not in rendered.lower()


def test_producer_rejects_body_field(tmp_path: Path) -> None:
    export_path = _export(tmp_path)
    document = json.loads(export_path.read_text(encoding="utf-8"))
    document["records"][0]["body"] = "private transcript"
    export_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SearchatExportError, match="unexpected body"):
        produce_manifest(export_path, tmp_path / "manifest.json")


def test_producer_rejects_stale_catalog_hash(tmp_path: Path) -> None:
    export_path = _export(tmp_path)
    document = json.loads(export_path.read_text(encoding="utf-8"))
    document["records"][0]["file_hash"] = "0" * 64
    export_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SearchatExportError, match="file_hash does not match"):
        produce_manifest(export_path, tmp_path / "manifest.json")


def test_cli_writes_frozen_manifest_from_searchat_export(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export_path = _export(tmp_path)
    manifest_path = tmp_path / ".laconic" / "k1" / "manifest.json"

    exit_code = main(
        [
            "k1",
            "manifest",
            "from-searchat",
            "--input",
            str(export_path),
            "--output",
            str(manifest_path),
            "--holdout-fraction",
            "0.4",
            "--seed",
            "adapter-test",
        ]
    )

    assert exit_code == EXIT_OK
    assert "wrote K1 manifest" in capsys.readouterr().out
    assert verify_manifest(manifest_path).candidates


def test_cli_rejects_invalid_searchat_export(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export_path = _export(tmp_path)
    document = json.loads(export_path.read_text(encoding="utf-8"))
    document["records"][0]["body"] = "private transcript"
    export_path.write_text(json.dumps(document), encoding="utf-8")

    exit_code = main(
        [
            "k1",
            "manifest",
            "from-searchat",
            "--input",
            str(export_path),
            "--output",
            str(tmp_path / "manifest.json"),
        ]
    )

    assert exit_code == EXIT_K1_MANIFEST
    assert "unexpected body" in capsys.readouterr().err


def test_cli_rejects_non_ascii_catalog_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export_path = _export(tmp_path)
    document = json.loads(export_path.read_text(encoding="utf-8"))
    document["records"][0]["file_hash"] = "é" * 64
    export_path.write_text(json.dumps(document), encoding="utf-8")

    assert (
        main(
            [
                "k1",
                "manifest",
                "from-searchat",
                "--input",
                str(export_path),
                "--output",
                str(tmp_path / "manifest.json"),
            ]
        )
        == EXIT_K1_MANIFEST
    )
    assert "file_hash must be 64 lowercase hex" in capsys.readouterr().err


def test_cli_rejects_tool_density_outside_float_range(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export_path = _export(tmp_path)
    document = json.loads(export_path.read_text(encoding="utf-8"))
    document["records"][0]["tool_density"] = 10**400
    export_path.write_text(json.dumps(document), encoding="utf-8")

    assert (
        main(
            [
                "k1",
                "manifest",
                "from-searchat",
                "--input",
                str(export_path),
                "--output",
                str(tmp_path / "manifest.json"),
            ]
        )
        == EXIT_K1_MANIFEST
    )
    assert "outside the float range" in capsys.readouterr().err


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_producer_rejects_non_integer_schema_version(
    tmp_path: Path, schema_version: object
) -> None:
    export_path = _export(tmp_path)
    document = json.loads(export_path.read_text(encoding="utf-8"))
    document["schema_version"] = schema_version
    export_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(
        SearchatExportError, match="unsupported Searchat metadata export schema_version"
    ):
        produce_manifest(export_path, tmp_path / "manifest.json")
