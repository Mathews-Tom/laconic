"""Tests for K1 paired contemporary replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest

from laconic.cli import EXIT_K1_MANIFEST, EXIT_OK, main
from laconic.k1.eligibility import assess_manifest, write_eligibility_ledger
from laconic.k1.environment_ledger import (
    assess_environments,
    write_environment_ledger,
)
from laconic.k1.epoch import read_access_audit, read_epoch
from laconic.k1.manifest import Candidate, Manifest, read_manifest, source_sha256, write_manifest
from laconic.k1.paired_config import (
    PairedReplayConfig,
    PairedReplayConfigError,
    PairedRunProvenance,
    PriceTable,
    UsageMapping,
    read_paired_config,
    write_paired_config,
)
from laconic.k1.paired_report import (
    PairedReportError,
    build_paired_report,
    verify_paired_report,
    write_paired_report,
)
from laconic.k1.paired_runner import (
    PairedReplayAdmissionError,
    PairedReplayCostCapError,
    PairedReplayError,
    PairedReplayRequest,
    PairedReplayResponse,
    PairedResponseTurn,
    PairedWorkload,
    admit_paired_workloads,
    run_paired_replay,
)
from laconic.k1.pricing import BillableResponseUsage, cost_usage, normalize_usage


def _config(tmp_path: Path) -> PairedReplayConfig:
    private = tmp_path / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    return PairedReplayConfig(
        epoch_digest="a" * 64,
        epoch_path=(private / "epoch.json").resolve(),
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


def _workload(tmp_path: Path) -> tuple[PairedReplayConfig, PairedWorkload]:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    source = private / "candidate.jsonl"
    records = [
        {
            "timestamp": "2026-07-30T21:00:00Z",
            "type": "user",
            "message": {"role": "user", "content": "Inspect."},
        },
        {
            "timestamp": "2026-07-30T21:00:01Z",
            "message": {
                "id": "message-1",
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
    candidate = Candidate(
        candidate_id="candidate-a",
        source_path=source.resolve(),
        source_sha256=source_sha256(source),
        provider="claude-code",
        model="claude-sonnet-4-6",
        model_family="claude-4",
        project="acme/service",
        timestamp="2026-07-30T21:00:00Z",
        session_length=1,
        message_count=1,
        has_code=True,
        tool_density=1.0,
        time_period="2026-Q3",
        session_size_band="small",
        selection_stratum="claude-code|claude-4|2026-Q3|small",
        lineage="issue-1",
        eligibility_disposition="confirmatory",
        split="redesign",
    )
    holdout_source = private / "holdout.jsonl"
    holdout_source.write_text(
        source.read_text(encoding="utf-8").replace("Inspect.", "Inspect holdout."),
        encoding="utf-8",
    )
    holdout = replace(
        candidate,
        candidate_id="candidate-holdout",
        source_path=holdout_source.resolve(),
        source_sha256=source_sha256(holdout_source),
        project="acme/holdout",
        lineage="issue-holdout",
        split="holdout",
    )
    manifest = Manifest((candidate, holdout))
    manifest_path = private / "manifest.json"
    epoch_path = private / "epoch.json"
    write_manifest(manifest_path, manifest)
    assert (
        main(
            [
                "k1",
                "epoch",
                "create",
                "--manifest",
                str(manifest_path),
                "--epoch",
                str(epoch_path),
                "--audit",
                str(private / "access-audit.json"),
                "--approved-root",
                str(private),
                "--epoch-id",
                "k1-test-paired-replay",
                "--created-at",
                "2026-07-31T11:00:00Z",
            ]
        )
        == EXIT_OK
    )
    holdout_source.unlink()
    eligibility_path = private / "eligibility.json"
    write_eligibility_ledger(eligibility_path, assess_manifest(epoch_path, manifest_path))
    write_environment_ledger(
        private / "environment.json",
        assess_environments(epoch_path, manifest_path, eligibility_path),
    )
    config = replace(_config(tmp_path), epoch_digest=read_epoch(epoch_path).digest)
    return config, admit_paired_workloads(config)[0]


class _ReplayClient:
    def __init__(self, *, input_tokens: int = 100) -> None:
        self.input_tokens = input_tokens
        self.requests: list[PairedReplayRequest] = []

    def respond(self, request: PairedReplayRequest) -> PairedReplayResponse:
        self.requests.append(request)
        return PairedReplayResponse(
            (
                PairedResponseTurn(
                    response={"arm": request.arm, "fresh": True},
                    native_usage={
                        "input_tokens": self.input_tokens,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 10,
                    },
                    classification="completed",
                ),
            )
        )


def _turn(
    classification: Literal["completed", "induced", "unsupported"] = "completed",
    *,
    unsupported_reason: str | None = None,
) -> PairedResponseTurn:
    return PairedResponseTurn(
        response={"classification": classification},
        native_usage={
            "input_tokens": 100,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 10,
        },
        classification=classification,
        unsupported_reason=unsupported_reason,
    )


class _OutcomeClient:
    def __init__(
        self, raw_turns: tuple[PairedResponseTurn, ...], codec_turns: tuple[PairedResponseTurn, ...]
    ) -> None:
        self.raw_turns = raw_turns
        self.codec_turns = codec_turns
        self.requests: list[PairedReplayRequest] = []

    def respond(self, request: PairedReplayRequest) -> PairedReplayResponse:
        self.requests.append(request)
        turns = self.raw_turns if request.arm == "raw" else self.codec_turns
        return PairedReplayResponse(turns)


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
        "epoch_digest",
        "epoch_path",
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


def test_normalize_usage_requires_every_declared_native_counter(tmp_path: Path) -> None:
    mapping = _config(tmp_path).usage_mapping
    native_usage = {
        "input_tokens": 100,
        "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 10,
        "output_tokens": 50,
    }

    assert normalize_usage(native_usage, mapping) == BillableResponseUsage(100, 20, 10, 50)

    native_usage.pop("cache_creation_input_tokens")
    with pytest.raises(PairedReplayConfigError, match="missing configured field"):
        normalize_usage(native_usage, mapping)
    native_usage["cache_creation_input_tokens"] = True
    with pytest.raises(PairedReplayConfigError, match="non-negative integer"):
        normalize_usage(native_usage, mapping)
    native_usage["new_billable_counter"] = 1
    with pytest.raises(PairedReplayConfigError, match="undeclared fields"):
        normalize_usage(native_usage, mapping)


def test_decimal_pricing_uses_explicit_cache_categories(tmp_path: Path) -> None:
    config = _config(tmp_path)
    usage = BillableResponseUsage(
        input_tokens=1_000_000,
        cache_read_tokens=2_000_000,
        cache_write_tokens=4_000_000,
        output_tokens=8_000_000,
    )

    assert cost_usage(usage, config.pricing) == Decimal("138.6")


def test_paired_runner_reuses_identical_settings_and_private_artifacts(tmp_path: Path) -> None:
    config, workload = _workload(tmp_path)
    client = _ReplayClient()

    receipt = run_paired_replay(config, (workload,), client, run_id="run-1")

    assert [request.arm for request in client.requests] == ["raw", "codec", "raw", "codec"]
    assert {request.settings_digest for request in client.requests} == {config.settings_digest}
    assert receipt.config_digest == config.digest
    assert len(receipt.pairs) == config.repeat_count
    for pair in receipt.pairs:
        assert pair.raw.provenance.config_digest == pair.codec.provenance.config_digest
        assert pair.raw.artifact_path.stat().st_mode & 0o777 == 0o600
        assert pair.codec.artifact_path.stat().st_mode & 0o777 == 0o600
        assert '"fresh":true' in pair.raw.artifact_path.read_text(encoding="utf-8")
    assert all(not hasattr(request.workload, "session") for request in client.requests)


def test_paired_report_persists_receipt_and_rejects_tampered_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, workload = _workload(tmp_path)
    receipt = run_paired_replay(config, (workload,), _ReplayClient(), run_id="run-report")
    epoch = read_epoch(config.epoch_path)
    report_path = config.epoch_path.parent / "paired-report.json"
    report = build_paired_report(config.epoch_path, config.manifest_path, config, receipt)

    write_paired_report(report_path, epoch, report)

    verified = verify_paired_report(report_path, config.epoch_path, config.manifest_path, config)
    assert verified.digest == report.digest
    config_path = config.epoch_path.parent / "paired-config.json"
    write_paired_config(config_path, config)
    assert (
        main(
            [
                "k1",
                "replay",
                "verify-report",
                "--report",
                str(report_path),
                "--config",
                str(config_path),
                "--epoch",
                str(config.epoch_path),
                "--manifest",
                str(config.manifest_path),
            ]
        )
        == EXIT_OK
    )
    assert report_path.stat().st_mode & 0o777 == 0o600
    assert '"response"' not in report_path.read_text(encoding="utf-8")
    receipt.pairs[0].raw.artifact_path.write_text("tampered", encoding="utf-8")

    with pytest.raises(PairedReportError, match="response artifact digest mismatch"):
        verify_paired_report(report_path, config.epoch_path, config.manifest_path, config)
    assert (
        main(
            [
                "k1",
                "replay",
                "verify-report",
                "--report",
                str(report_path),
                "--config",
                str(config_path),
                "--epoch",
                str(config.epoch_path),
                "--manifest",
                str(config.manifest_path),
            ]
        )
        == EXIT_K1_MANIFEST
    )
    assert "response artifact digest mismatch" in capsys.readouterr().err


def test_paired_report_reports_response_artifact_permission_failure(
    tmp_path: Path,
) -> None:
    config, workload = _workload(tmp_path)
    receipt = run_paired_replay(config, (workload,), _ReplayClient(), run_id="run-report")
    epoch = read_epoch(config.epoch_path)
    report_path = config.epoch_path.parent / "paired-report.json"
    report = build_paired_report(config.epoch_path, config.manifest_path, config, receipt)
    write_paired_report(report_path, epoch, report)
    receipt.pairs[0].raw.artifact_path.chmod(0o644)

    with pytest.raises(PairedReportError, match="response artifact must have mode 0600"):
        verify_paired_report(report_path, config.epoch_path, config.manifest_path, config)


def test_paired_report_rejects_nonprivate_response_artifact_directory(
    tmp_path: Path,
) -> None:
    config, workload = _workload(tmp_path)
    receipt = run_paired_replay(config, (workload,), _ReplayClient(), run_id="run-report")
    epoch = read_epoch(config.epoch_path)
    report_path = config.epoch_path.parent / "paired-report.json"
    report = build_paired_report(config.epoch_path, config.manifest_path, config, receipt)
    write_paired_report(report_path, epoch, report)
    receipt.pairs[0].raw.artifact_path.parent.chmod(0o755)

    with pytest.raises(PairedReportError, match="response artifact directory must have mode 0700"):
        verify_paired_report(report_path, config.epoch_path, config.manifest_path, config)


def test_paired_report_rejects_recomputed_tampered_aggregate(tmp_path: Path) -> None:
    config, workload = _workload(tmp_path)
    receipt = run_paired_replay(config, (workload,), _ReplayClient(), run_id="run-report")
    epoch = read_epoch(config.epoch_path)
    report_path = config.epoch_path.parent / "paired-report.json"
    report = build_paired_report(config.epoch_path, config.manifest_path, config, receipt)
    write_paired_report(report_path, epoch, report)

    document = json.loads(report_path.read_text(encoding="utf-8"))
    document["strata"][0]["raw_cost_usd"] = "999"
    payload = {key: value for key, value in document.items() if key != "digest"}
    document["digest"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    report_path.write_text(json.dumps(document), encoding="utf-8")
    report_path.chmod(0o600)

    with pytest.raises(PairedReportError, match="strata do not match"):
        verify_paired_report(report_path, config.epoch_path, config.manifest_path, config)


def test_paired_report_rejects_nonprivate_parent_directory(tmp_path: Path) -> None:
    config, workload = _workload(tmp_path)
    receipt = run_paired_replay(config, (workload,), _ReplayClient(), run_id="run-report")
    epoch = read_epoch(config.epoch_path)
    report_path = config.epoch_path.parent / "paired-report.json"
    report = build_paired_report(config.epoch_path, config.manifest_path, config, receipt)
    write_paired_report(report_path, epoch, report)
    report_path.parent.chmod(0o755)

    with pytest.raises(PairedReportError, match="directory must have mode 0700"):
        verify_paired_report(report_path, config.epoch_path, config.manifest_path, config)


def test_fresh_epoch_workflow_produces_m5_ready_redesign_report(tmp_path: Path) -> None:
    config, workload = _workload(tmp_path)
    holdout = next(
        candidate
        for candidate in read_manifest(config.manifest_path).candidates
        if candidate.split == "holdout"
    )
    assert not holdout.source_path.exists()
    receipt = run_paired_replay(config, (workload,), _ReplayClient(), run_id="run-m5-ready")
    epoch = read_epoch(config.epoch_path)
    report_path = config.epoch_path.parent / "m5-ready-report.json"
    report = build_paired_report(config.epoch_path, config.manifest_path, config, receipt)

    write_paired_report(report_path, epoch, report)

    assert (
        main(
            [
                "k1",
                "epoch",
                "verify",
                "--epoch",
                str(config.epoch_path),
                "--manifest",
                str(config.manifest_path),
            ]
        )
        == EXIT_OK
    )
    verified = verify_paired_report(report_path, config.epoch_path, config.manifest_path, config)
    audit = read_access_audit(epoch.audit_path)
    assert verified.strata[0].completed_pair_count == config.repeat_count
    assert [(record.candidate_id, record.operation, record.split) for record in audit.records] == [
        ("candidate-a", "eligibility_assess", "redesign"),
        ("candidate-a", "eligibility_verify", "redesign"),
        ("candidate-a", "environment_assess", "redesign"),
        ("candidate-a", "eligibility_verify", "redesign"),
        ("candidate-a", "eligibility_verify", "redesign"),
        ("candidate-a", "environment_verify", "redesign"),
        ("candidate-a", "paired_admit", "redesign"),
        ("candidate-a", "eligibility_verify", "redesign"),
        ("candidate-a", "eligibility_verify", "redesign"),
        ("candidate-a", "environment_verify", "redesign"),
        ("candidate-a", "paired_admit", "redesign"),
    ]


def test_paired_runner_records_billed_raw_arm_before_failing_cost_cap(tmp_path: Path) -> None:
    config, workload = _workload(tmp_path)
    config = replace(config, cost_cap_per_pair_usd="0.0001")
    client = _ReplayClient()

    with pytest.raises(PairedReplayCostCapError, match="past the"):
        run_paired_replay(config, (workload,), client, run_id="run-over-cap")

    assert [request.arm for request in client.requests] == ["raw"]
    assert (config.artifact_root / "run-over-cap" / "candidate-a" / "0000-raw.json").is_file()


def test_paired_runner_accounts_for_induced_codec_turns(tmp_path: Path) -> None:
    config, workload = _workload(tmp_path)
    client = _OutcomeClient(
        (_turn(),),
        (_turn(), _turn("induced")),
    )

    receipt = run_paired_replay(config, (workload,), client, run_id="run-induced")

    assert all(
        pair.raw.turn_accounting.completed_turn_count == 1
        and pair.raw.turn_accounting.induced_turn_count == 0
        and pair.codec.turn_accounting.completed_turn_count == 1
        and pair.codec.turn_accounting.induced_turn_count == 1
        and pair.codec.turn_accounting.unsupported_turn_count == 0
        for pair in receipt.pairs
    )
    assert '"classification":"induced"' in receipt.pairs[0].codec.artifact_path.read_text(
        encoding="utf-8"
    )


def test_unsupported_turn_terminates_only_its_pair_and_preserves_artifacts(
    tmp_path: Path,
) -> None:
    config, workload = _workload(tmp_path)
    client = _OutcomeClient(
        (_turn("unsupported", unsupported_reason="snapshot has no matching tool call"),),
        (_turn(),),
    )

    receipt = run_paired_replay(config, (workload,), client, run_id="run-unsupported")

    assert [request.arm for request in client.requests] == ["raw", "raw"]
    assert receipt.pairs == ()
    assert len(receipt.terminated_pairs) == config.repeat_count
    terminated = receipt.terminated_pairs[0]
    assert terminated.raw.turn_accounting.unsupported_turn_count == 1
    assert terminated.raw.artifact_path.is_file()
    assert "snapshot has no matching tool call" in terminated.raw.artifact_path.read_text(
        encoding="utf-8"
    )


def test_runner_rejects_induced_raw_turns_and_nonterminal_unsupported_turns(
    tmp_path: Path,
) -> None:
    with pytest.raises(PairedReplayError, match="must terminate"):
        PairedReplayResponse((_turn("unsupported", unsupported_reason="unsupported"), _turn()))

    config, workload = _workload(tmp_path)
    client = _OutcomeClient((_turn("induced"),), (_turn(),))
    with pytest.raises(PairedReplayError, match="raw arm must not"):
        run_paired_replay(config, (workload,), client, run_id="run-invalid")
    assert [request.arm for request in client.requests] == ["raw"]


def test_paired_admission_rejects_holdout_config_before_native_read(tmp_path: Path) -> None:
    config, _ = _workload(tmp_path)

    with pytest.raises(PairedReplayAdmissionError, match="requires the redesign split"):
        admit_paired_workloads(replace(config, split="holdout"))


def test_paired_config_cli_and_artifact_root_hygiene(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, workload = _workload(tmp_path)
    config_path = tmp_path / "private" / "paired-replay.json"
    write_paired_config(config_path, config)

    assert main(["k1", "replay", "verify-config", "--config", str(config_path)]) == EXIT_OK
    assert "verified K1 paired replay config" in capsys.readouterr().out

    bad_root = replace(config, artifact_root=tmp_path / "private" / "bad-artifacts")
    bad_root.artifact_root.mkdir(mode=0o755)
    bad_root.artifact_root.chmod(0o755)
    client = _ReplayClient()
    with pytest.raises(PairedReplayError, match="mode 0700"):
        run_paired_replay(bad_root, (workload,), client, run_id="run-private")
    assert client.requests == []


def test_runner_rejects_unsafe_or_symlinked_artifact_paths_before_request(
    tmp_path: Path,
) -> None:
    config, workload = _workload(tmp_path)
    client = _ReplayClient()

    with pytest.raises(PairedReplayError, match="simple names"):
        run_paired_replay(config, (workload,), client, run_id="../escape")
    assert client.requests == []

    target = tmp_path / "private" / "outside"
    target.mkdir(mode=0o700)
    (config.artifact_root / "run-link").symlink_to(target, target_is_directory=True)
    with pytest.raises(PairedReplayError, match="non-symlink directory"):
        run_paired_replay(config, (workload,), client, run_id="run-link")
    assert client.requests == []


def test_paired_config_cli_rejects_nonprivate_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    config_path = tmp_path / "private" / "paired-replay.json"
    write_paired_config(config_path, config)
    config_path.chmod(0o644)

    assert main(["k1", "replay", "verify-config", "--config", str(config_path)]) == EXIT_K1_MANIFEST
    assert "mode 0600" in capsys.readouterr().err
