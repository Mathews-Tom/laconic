"""Tests for K1 paired contemporary replay."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from laconic.k1.paired_config import (
    PairedReplayConfig,
    PairedReplayConfigError,
    PairedRunProvenance,
    PriceTable,
    UsageMapping,
    read_paired_config,
    write_paired_config,
)


def _config(tmp_path: Path) -> PairedReplayConfig:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    return PairedReplayConfig(
        manifest_path=(private / "manifest.json").resolve(),
        eligibility_ledger_path=(private / "eligibility.json").resolve(),
        environment_ledger_path=(private / "environment.json").resolve(),
        artifact_root=(private / "artifacts").resolve(),
        provider="anthropic",
        endpoint="https://api.anthropic.com",
        privacy_boundary="approved-private-provider",
        model="claude-sonnet-4-6",
        decoding_parameters={"max_tokens": 1024, "temperature": 0},
        seed_supported=False,
        seed=None,
        repeat_count=2,
        split="redesign",
        candidate_ids=("candidate-a",),
        pricing=PriceTable("2026-07-30", "3", "0.3", "3.75", "15"),
        usage_mapping=UsageMapping(
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        ),
        cost_cap_per_pair_usd="1.00",
        cost_cap_run_usd="5.00",
        unsupported_policy="terminate_pair",
        induced_policy="include_in_codec_cost",
    )


def test_private_paired_config_round_trip_integrity_checks_every_setting(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = tmp_path / "private" / "paired-replay.json"

    write_paired_config(path, config)

    assert path.stat().st_mode & 0o777 == 0o600
    assert read_paired_config(path) == config
    assert set(config.payload_without_digest()) == {
        "artifact_root",
        "candidate_ids",
        "cost_cap_per_pair_usd",
        "cost_cap_run_usd",
        "decoding_parameters",
        "eligibility_ledger_path",
        "endpoint",
        "environment_ledger_path",
        "induced_policy",
        "manifest_path",
        "model",
        "pricing",
        "privacy_boundary",
        "provider",
        "repeat_count",
        "schema_version",
        "seed",
        "seed_supported",
        "split",
        "unsupported_policy",
        "usage_mapping",
    }
    changed = replace(config, model="claude-opus-4-6")
    assert changed.digest != config.digest
    assert changed.settings_digest != config.settings_digest


def test_paired_config_canonicalizes_absolute_paths_before_digesting(tmp_path: Path) -> None:
    private = tmp_path / "private"
    alias = tmp_path / "private-alias"
    alias.symlink_to(private, target_is_directory=True)
    config = replace(
        _config(tmp_path),
        manifest_path=alias / "nested" / ".." / "manifest.json",
        eligibility_ledger_path=alias / "eligibility.json",
        environment_ledger_path=alias / "environment.json",
        artifact_root=alias / "artifacts",
    )
    path = private / "paired-replay.json"

    write_paired_config(path, config)

    assert config.manifest_path == private / "manifest.json"
    assert read_paired_config(path) == config


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ("http://api.example.test", "HTTPS"),
        ("https://user:secret@api.example.test", "credentials"),
        ("https://api.example.test?api_key=secret", "query"),
    ],
)
def test_paired_config_rejects_credential_bearing_endpoints(
    tmp_path: Path, endpoint: str, message: str
) -> None:
    with pytest.raises(PairedReplayConfigError, match=message):
        replace(_config(tmp_path), endpoint=endpoint)


def test_paired_config_freezes_parameters_and_rejects_zero_live_prices(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    with pytest.raises(TypeError):
        config.decoding_parameters["temperature"] = 1
    with pytest.raises(PairedReplayConfigError, match="input_per_mtok must be positive"):
        PriceTable("2026-07-30", "0", "0", "0", "15")
    with pytest.raises(PairedReplayConfigError, match="output_per_mtok must be positive"):
        PriceTable("2026-07-30", "3", "0", "0", "0")


def test_paired_config_rejects_missing_seed_repetition_and_caps(tmp_path: Path) -> None:
    config = _config(tmp_path)

    with pytest.raises(PairedReplayConfigError, match="at least 2"):
        replace(config, repeat_count=1)
    with pytest.raises(PairedReplayConfigError, match="must not exceed"):
        replace(config, cost_cap_per_pair_usd="6.00")
    with pytest.raises(PairedReplayConfigError, match="must be positive"):
        replace(config, cost_cap_run_usd="0")
    with pytest.raises(PairedReplayConfigError, match="seed must be null"):
        replace(config, seed="not-supported")


def test_paired_config_rejects_tampering_and_nonprivate_paths(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = tmp_path / "private" / "paired-replay.json"
    write_paired_config(path, config)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["privacy_boundary"] = "changed"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(PairedReplayConfigError, match="digest mismatch"):
        read_paired_config(path)

    write_paired_config(path, config)
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["pricing"]["cache_read_per_mtok"]
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(PairedReplayConfigError, match="pricing has invalid fields"):
        read_paired_config(path)

    write_paired_config(path, config)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["usage_mapping"]["injected"] = "attacker-controlled"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(PairedReplayConfigError, match="usage_mapping has invalid fields"):
        read_paired_config(path)

    write_paired_config(path, config)
    path.chmod(0o644)
    with pytest.raises(PairedReplayConfigError, match="mode 0600"):
        read_paired_config(path)


def test_paired_config_rejects_non_utf8_and_symlink_paths(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = tmp_path / "private" / "paired-replay.json"
    path.write_bytes(b"\xff\xfe")
    path.chmod(0o600)

    with pytest.raises(PairedReplayConfigError, match="cannot read"):
        read_paired_config(path)

    write_paired_config(path, config)
    linked = tmp_path / "private" / "linked-paired-replay.json"
    linked.symlink_to(path)
    with pytest.raises(PairedReplayConfigError, match="non-symlink regular file"):
        read_paired_config(linked)


def test_paired_run_provenance_rejects_invalid_receipts() -> None:
    valid = {
        "config_digest": "a" * 64,
        "manifest_digest": "a" * 64,
        "eligibility_ledger_digest": "a" * 64,
        "environment_ledger_digest": "a" * 64,
        "candidate_id": "candidate-a",
        "source_sha256": "a" * 64,
        "environment_digest": "a" * 64,
        "run_id": "run-1",
        "arm": "raw",
        "repeat_index": 0,
        "response_artifact_sha256": "b" * 64,
    }

    with pytest.raises(PairedReplayConfigError, match="config_digest"):
        PairedRunProvenance(**(valid | {"config_digest": "invalid"}))
    with pytest.raises(PairedReplayConfigError, match="candidate_id"):
        PairedRunProvenance(**(valid | {"candidate_id": ""}))
    with pytest.raises(PairedReplayConfigError, match="unknown arm"):
        PairedRunProvenance(**(valid | {"arm": "invalid"}))
    with pytest.raises(PairedReplayConfigError, match="repeat_index"):
        PairedRunProvenance(**(valid | {"repeat_index": -1}))


def test_paired_run_provenance_serializes_complete_noncontent_receipt() -> None:
    receipt = PairedRunProvenance(
        config_digest="a" * 64,
        manifest_digest="a" * 64,
        eligibility_ledger_digest="a" * 64,
        environment_ledger_digest="a" * 64,
        candidate_id="candidate-a",
        source_sha256="a" * 64,
        environment_digest="a" * 64,
        run_id="run-1",
        arm="codec",
        repeat_index=1,
        response_artifact_sha256="b" * 64,
    )

    assert receipt.to_payload() == {
        "arm": "codec",
        "candidate_id": "candidate-a",
        "config_digest": "a" * 64,
        "eligibility_ledger_digest": "a" * 64,
        "environment_digest": "a" * 64,
        "environment_ledger_digest": "a" * 64,
        "manifest_digest": "a" * 64,
        "repeat_index": 1,
        "response_artifact_sha256": "b" * 64,
        "run_id": "run-1",
        "source_sha256": "a" * 64,
    }
