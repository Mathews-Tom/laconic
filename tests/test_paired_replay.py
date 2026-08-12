"""Tests for paired replay paired contemporary replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest

from tools.paired_replay import cli
from tools.paired_replay.cli import EXIT_OK, EXIT_PRIVATE_ARTIFACT, main
from tools.paired_replay.config import (
    InteractionReceiptBinding,
    PairedReplayConfig,
    PairedReplayConfigError,
    PairedRunProvenance,
    PriceTable,
    UsageMapping,
    build_execution_config,
    create_execution_config,
    read_paired_config,
    verify_execution_config,
    write_paired_config,
)
from tools.paired_replay.eligibility import assess_manifest, write_eligibility_ledger
from tools.paired_replay.environment_ledger import (
    assess_environments,
    write_environment_ledger,
)
from tools.paired_replay.epoch import read_access_audit, read_epoch
from tools.paired_replay.interaction import (
    InteractionReceiptError,
    build_interaction_receipt,
)
from tools.paired_replay.manifest import (
    Candidate,
    Manifest,
    read_manifest,
    source_sha256,
    write_manifest,
)
from tools.paired_replay.pricing import BillableResponseUsage, cost_usage, normalize_usage
from tools.paired_replay.report import (
    PairedReportError,
    _receipt_document_arms,
    build_paired_report,
    verify_paired_report,
    write_paired_report,
)
from tools.paired_replay.runner import (
    PairedPairReceipt,
    PairedReplayAdmissionError,
    PairedReplayCostCapError,
    PairedReplayError,
    PairedReplayRequest,
    PairedReplayResponse,
    PairedResponseTurn,
    PairedRunReceipt,
    PairedWorkload,
    admit_paired_workloads,
    run_paired_replay,
)


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
        provider="openai",
        endpoint="https://api.openai.com/v1/responses",
        api_version="v1",
        credential_environment="OPENAI_API_KEY",
        privacy_boundary="openai-store-false-abuse-monitoring-30-days-prompt-cache-retention-in-memory",
        model="gpt-5.4-mini-2026-03-17",
        decoding_parameters={
            "max_output_tokens": 4096,
            "parallel_tool_calls": True,
            "prompt_cache_retention": "in_memory",
            "reasoning_effort": "none",
            "store": False,
            "stream": False,
            "temperature": 0.0,
        },
        seed_supported=False,
        seed=None,
        repeat_count=2,
        split="redesign",
        candidate_ids=("candidate-a",),
        interaction_receipts=(
            InteractionReceiptBinding(
                "candidate-a", (private / "interaction.json").resolve(), "b" * 64
            ),
        ),
        pricing=PriceTable("2026-08-04", "0.75", "0.075", "0.75", "4.50"),
        pricing_source="https://openai.com/api/pricing/",
        usage_mapping=UsageMapping(
            "usage.input_tokens",
            "usage.input_tokens_details.cached_tokens",
            None,
            "usage.output_tokens",
            True,
        ),
        cost_cap_per_pair_usd="0.20",
        cost_cap_run_usd="0.80",
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
        {
            "timestamp": "2026-07-30T21:00:03Z",
            "type": "user",
            "message": {"role": "user", "content": "Summarize the result."},
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
    interaction_path = private / "interaction.json"
    interaction = build_interaction_receipt(
        epoch_path,
        manifest_path,
        eligibility_path,
        private / "environment.json",
        candidate.candidate_id,
        interaction_path,
    )
    config = replace(
        _config(tmp_path),
        epoch_digest=read_epoch(epoch_path).digest,
        interaction_receipts=(
            InteractionReceiptBinding(candidate.candidate_id, interaction_path, interaction.digest),
        ),
    )
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
                        "usage.input_tokens": self.input_tokens,
                        "usage.input_tokens_details.cached_tokens": 0,
                        "usage.output_tokens": 10,
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
            "usage.input_tokens": 100,
            "usage.input_tokens_details.cached_tokens": 0,
            "usage.output_tokens": 10,
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
        "api_version",
        "artifact_root",
        "candidate_ids",
        "cost_cap_per_pair_usd",
        "cost_cap_run_usd",
        "credential_environment",
        "decoding_parameters",
        "eligibility_ledger_path",
        "endpoint",
        "epoch_digest",
        "epoch_path",
        "environment_ledger_path",
        "induced_policy",
        "interaction_receipts",
        "manifest_path",
        "model",
        "pricing",
        "pricing_source",
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
    cache_semantics_changed = replace(
        config,
        usage_mapping=replace(config.usage_mapping, input_includes_cache=False),
    )
    assert cache_semantics_changed.digest != config.digest
    changed = replace(config, model="anthropic/claude-opus-4.6")
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


def test_execution_config_rejects_unapproved_provider_pins(tmp_path: Path) -> None:
    with pytest.raises(PairedReplayConfigError, match="approved OpenAI contract"):
        verify_execution_config(
            replace(_config(tmp_path), endpoint="https://api.example.test/v1/messages")
        )
    with pytest.raises(PairedReplayConfigError, match="approved OpenAI contract"):
        verify_execution_config(replace(_config(tmp_path), api_version="v2"))
    with pytest.raises(PairedReplayConfigError, match="approved OpenAI contract"):
        verify_execution_config(replace(_config(tmp_path), credential_environment="OTHER_API_KEY"))
    with pytest.raises(PairedReplayConfigError, match="approved OpenAI contract"):
        verify_execution_config(replace(_config(tmp_path), privacy_boundary="unapproved"))
    with pytest.raises(PairedReplayConfigError, match="approved provider contract"):
        verify_execution_config(replace(_config(tmp_path), model="gpt-5.4"))
    with pytest.raises(PairedReplayConfigError, match="does not support a seed"):
        verify_execution_config(replace(_config(tmp_path), seed_supported=True, seed="seed"))
    with pytest.raises(PairedReplayConfigError, match="exactly two seedless repeats"):
        verify_execution_config(replace(_config(tmp_path), repeat_count=3))
    with pytest.raises(PairedReplayConfigError, match="native usage mapping"):
        verify_execution_config(
            replace(
                _config(tmp_path),
                usage_mapping=UsageMapping(
                    "usage.input_tokens",
                    "usage.input_tokens_details.cache_write_tokens",
                    "usage.input_tokens_details.cached_tokens",
                    "usage.output_tokens",
                    True,
                ),
            )
        )
    with pytest.raises(PairedReplayConfigError, match="native usage mapping"):
        verify_execution_config(
            replace(
                _config(tmp_path),
                usage_mapping=UsageMapping(
                    "usage.input_tokens",
                    "usage.input_tokens_details.cached_tokens",
                    "usage.input_tokens_details.cache_write_tokens",
                    "usage.output_tokens",
                    False,
                ),
            )
        )
    with pytest.raises(PairedReplayConfigError, match="cost caps"):
        verify_execution_config(replace(_config(tmp_path), cost_cap_per_pair_usd="0.21"))
    with pytest.raises(PairedReplayConfigError, match="requires the redesign split"):
        verify_execution_config(replace(_config(tmp_path), split="holdout"))


def test_execution_config_revalidates_each_bound_m3e_receipt(tmp_path: Path) -> None:
    config, _ = _workload(tmp_path)
    binding = config.interaction_receipts[0]

    verify_execution_config(config)

    with pytest.raises(PairedReplayConfigError, match="does not match configuration"):
        verify_execution_config(
            replace(
                config,
                interaction_receipts=(replace(binding, receipt_digest="a" * 64),),
            )
        )
    with pytest.raises(PairedReplayConfigError, match="does not match configuration"):
        verify_execution_config(
            replace(
                config,
                candidate_ids=("candidate-b",),
                interaction_receipts=(replace(binding, candidate_id="candidate-b"),),
            )
        )
    with pytest.raises(PairedReplayConfigError, match="does not match configuration"):
        verify_execution_config(replace(config, epoch_digest="a" * 64))


def test_execution_config_builder_requires_verified_receipts(tmp_path: Path) -> None:
    config, _ = _workload(tmp_path)
    binding = config.interaction_receipts[0]

    private_entries_before = tuple(sorted(config.epoch_path.parent.iterdir()))
    created = build_execution_config(
        epoch_path=config.epoch_path,
        manifest_path=config.manifest_path,
        eligibility_ledger_path=config.eligibility_ledger_path,
        environment_ledger_path=config.environment_ledger_path,
        artifact_root=config.artifact_root,
        interaction_receipt_paths=(binding.receipt_path,),
    )

    assert created.epoch_digest == config.epoch_digest
    assert created.candidate_ids == (binding.candidate_id,)
    assert created.interaction_receipts == (binding,)
    assert tuple(sorted(config.epoch_path.parent.iterdir())) == private_entries_before
    verify_execution_config(created)

    audit_path = read_epoch(config.epoch_path).audit_path
    audit_before = read_access_audit(audit_path).head_digest
    with pytest.raises(PairedReplayConfigError, match="interaction receipt paths must be unique"):
        build_execution_config(
            epoch_path=config.epoch_path,
            manifest_path=config.manifest_path,
            eligibility_ledger_path=config.eligibility_ledger_path,
            environment_ledger_path=config.environment_ledger_path,
            artifact_root=config.artifact_root,
            interaction_receipt_paths=(binding.receipt_path, binding.receipt_path),
        )
    assert read_access_audit(audit_path).head_digest == audit_before

    binding.receipt_path.chmod(0o644)
    with pytest.raises(PairedReplayConfigError, match="interaction receipt .* is invalid"):
        build_execution_config(
            epoch_path=config.epoch_path,
            manifest_path=config.manifest_path,
            eligibility_ledger_path=config.eligibility_ledger_path,
            environment_ledger_path=config.environment_ledger_path,
            artifact_root=config.artifact_root,
            interaction_receipt_paths=(binding.receipt_path,),
        )
    assert tuple(sorted(config.epoch_path.parent.iterdir())) == private_entries_before
    with pytest.raises(PairedReplayConfigError, match="at least one interaction receipt"):
        build_execution_config(
            epoch_path=config.epoch_path,
            manifest_path=config.manifest_path,
            eligibility_ledger_path=config.eligibility_ledger_path,
            environment_ledger_path=config.environment_ledger_path,
            artifact_root=config.artifact_root,
            interaction_receipt_paths=(),
        )


def test_execution_config_rejects_config_over_sealed_evidence(tmp_path: Path) -> None:
    config, _ = _workload(tmp_path)
    binding = config.interaction_receipts[0]
    audit_path = read_epoch(config.epoch_path).audit_path
    audit_before = read_access_audit(audit_path).head_digest
    sealed_paths = (
        config.epoch_path,
        config.manifest_path,
        config.eligibility_ledger_path,
        config.environment_ledger_path,
        audit_path,
        binding.receipt_path,
    )

    for config_path in sealed_paths:
        with pytest.raises(
            PairedReplayConfigError,
            match="config_output_path must not overwrite sealed private evidence",
        ):
            create_execution_config(
                epoch_path=config.epoch_path,
                manifest_path=config.manifest_path,
                eligibility_ledger_path=config.eligibility_ledger_path,
                environment_ledger_path=config.environment_ledger_path,
                artifact_root=config.artifact_root,
                interaction_receipt_paths=(binding.receipt_path,),
                config_path=config_path,
            )

    assert read_access_audit(audit_path).head_digest == audit_before


def test_execution_config_rejects_unapproved_paths_before_receipt_access(
    tmp_path: Path,
) -> None:
    config, _ = _workload(tmp_path)
    binding = config.interaction_receipts[0]
    audit_path = read_epoch(config.epoch_path).audit_path
    audit_before = read_access_audit(audit_path).head_digest

    outside_path = tmp_path.resolve()
    with pytest.raises(PairedReplayConfigError, match="eligibility_ledger_path must be within"):
        build_execution_config(
            epoch_path=config.epoch_path,
            manifest_path=config.manifest_path,
            eligibility_ledger_path=outside_path,
            environment_ledger_path=config.environment_ledger_path,
            artifact_root=config.artifact_root,
            interaction_receipt_paths=(binding.receipt_path,),
        )
    with pytest.raises(PairedReplayConfigError, match="environment_ledger_path must be within"):
        build_execution_config(
            epoch_path=config.epoch_path,
            manifest_path=config.manifest_path,
            eligibility_ledger_path=config.eligibility_ledger_path,
            environment_ledger_path=outside_path,
            artifact_root=config.artifact_root,
            interaction_receipt_paths=(binding.receipt_path,),
        )
    with pytest.raises(
        PairedReplayConfigError, match=r"interaction_receipt_path\[0\] must be within"
    ):
        build_execution_config(
            epoch_path=config.epoch_path,
            manifest_path=config.manifest_path,
            eligibility_ledger_path=config.eligibility_ledger_path,
            environment_ledger_path=config.environment_ledger_path,
            artifact_root=config.artifact_root,
            interaction_receipt_paths=(outside_path / "interaction.json",),
        )
    with pytest.raises(PairedReplayConfigError, match="artifact_root must be within"):
        build_execution_config(
            epoch_path=config.epoch_path,
            manifest_path=config.manifest_path,
            eligibility_ledger_path=config.eligibility_ledger_path,
            environment_ledger_path=config.environment_ledger_path,
            artifact_root=outside_path,
            interaction_receipt_paths=(binding.receipt_path,),
        )
    with pytest.raises(PairedReplayConfigError, match="epoch_path must be absolute"):
        build_execution_config(
            epoch_path=Path("epoch.json"),
            manifest_path=config.manifest_path,
            eligibility_ledger_path=config.eligibility_ledger_path,
            environment_ledger_path=config.environment_ledger_path,
            artifact_root=config.artifact_root,
            interaction_receipt_paths=(binding.receipt_path,),
        )
    with pytest.raises(PairedReplayConfigError, match="eligibility_ledger_path must be within"):
        verify_execution_config(replace(config, eligibility_ledger_path=outside_path))
    with pytest.raises(PairedReplayConfigError, match="environment_ledger_path must be within"):
        verify_execution_config(replace(config, environment_ledger_path=outside_path))
    with pytest.raises(
        PairedReplayConfigError, match=r"interaction_receipt_path\[0\] must be within"
    ):
        verify_execution_config(
            replace(
                config,
                interaction_receipts=(replace(binding, receipt_path=outside_path),),
            )
        )
    with pytest.raises(PairedReplayConfigError, match="artifact_root must be within"):
        verify_execution_config(replace(config, artifact_root=outside_path))

    assert read_access_audit(audit_path).head_digest == audit_before


def test_replay_cli_creates_verified_private_config(tmp_path: Path) -> None:
    config, _ = _workload(tmp_path)
    config_path = config.epoch_path.parent / "generated-config.json"
    artifact_root = config.epoch_path.parent / "generated-artifacts"

    assert (
        main(
            [
                "replay",
                "create-config",
                "--epoch",
                str(config.epoch_path),
                "--manifest",
                str(config.manifest_path),
                "--eligibility-ledger",
                str(config.eligibility_ledger_path),
                "--environment-ledger",
                str(config.environment_ledger_path),
                "--interaction-receipt",
                str(config.interaction_receipts[0].receipt_path),
                "--artifact-root",
                str(artifact_root),
                "--config",
                str(config_path),
            ]
        )
        == EXIT_OK
    )

    created = read_paired_config(config_path)
    assert created.artifact_root == artifact_root
    assert main(["replay", "verify-config", "--config", str(config_path)]) == EXIT_OK
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_replay_cli_rejects_config_outside_epoch_roots(tmp_path: Path) -> None:
    config, _ = _workload(tmp_path)
    config_path = tmp_path / "outside-config.json"

    assert (
        main(
            [
                "replay",
                "create-config",
                "--epoch",
                str(config.epoch_path),
                "--manifest",
                str(config.manifest_path),
                "--eligibility-ledger",
                str(config.eligibility_ledger_path),
                "--environment-ledger",
                str(config.environment_ledger_path),
                "--interaction-receipt",
                str(config.interaction_receipts[0].receipt_path),
                "--artifact-root",
                str(config.artifact_root),
                "--config",
                str(config_path),
            ]
        )
        == EXIT_PRIVATE_ARTIFACT
    )
    assert not config_path.exists()


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
        "interaction_receipt_digest": "a" * 64,
        "condition_digest": "a" * 64,
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
        interaction_receipt_digest="a" * 64,
        condition_digest="a" * 64,
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
        "condition_digest": "a" * 64,
        "environment_digest": "a" * 64,
        "environment_ledger_digest": "a" * 64,
        "manifest_digest": "a" * 64,
        "repeat_index": 1,
        "interaction_receipt_digest": "a" * 64,
        "response_artifact_sha256": "b" * 64,
        "run_id": "run-1",
        "source_sha256": "a" * 64,
    }


def test_normalize_usage_treats_omitted_cache_write_as_zero(tmp_path: Path) -> None:
    mapping = _config(tmp_path).usage_mapping
    native_usage = {
        "usage.input_tokens": 100,
        "usage.input_tokens_details.cached_tokens": 20,
        "usage.output_tokens": 50,
    }

    assert normalize_usage(native_usage, mapping) == BillableResponseUsage(80, 20, 0, 50)
    assert cost_usage(normalize_usage(native_usage, mapping), _config(tmp_path).pricing) == Decimal(
        "0.0002865"
    )
    negative_remainder = {**native_usage, "usage.input_tokens": 19}
    with pytest.raises(PairedReplayConfigError, match="smaller than declared cache counters"):
        normalize_usage(negative_remainder, mapping)

    native_usage["usage.input_tokens_details.cache_write_tokens"] = 10
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

    assert cost_usage(usage, config.pricing) == Decimal("39.900")


def test_paired_runner_reuses_identical_settings_and_private_artifacts(tmp_path: Path) -> None:
    config, workload = _workload(tmp_path)
    client = _ReplayClient()

    receipt = run_paired_replay(config, client, run_id="run-1")

    assert [request.arm for request in client.requests] == ["raw", "codec", "raw", "codec"]
    assert {request.settings_digest for request in client.requests} == {config.settings_digest}
    assert receipt.config_digest == config.digest
    assert len(receipt.pairs) == config.repeat_count
    for pair in receipt.pairs:
        assert pair.raw.provenance.config_digest == pair.codec.provenance.config_digest
        assert (
            pair.raw.provenance.interaction_receipt_digest
            == pair.codec.provenance.interaction_receipt_digest
        )
        assert pair.raw.provenance.condition_digest != pair.codec.provenance.condition_digest
        assert pair.raw.artifact_path.stat().st_mode & 0o777 == 0o600
        assert pair.codec.artifact_path.stat().st_mode & 0o777 == 0o600
        assert '"fresh":true' in pair.raw.artifact_path.read_text(encoding="utf-8")
    assert all(not hasattr(request.workload, "session") for request in client.requests)


def test_paired_report_persists_receipt_and_rejects_tampered_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, workload = _workload(tmp_path)
    receipt = run_paired_replay(config, _ReplayClient(), run_id="run-report")
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
        == EXIT_PRIVATE_ARTIFACT
    )
    assert "response artifact digest mismatch" in capsys.readouterr().err


def test_paired_report_reports_response_artifact_permission_failure(
    tmp_path: Path,
) -> None:
    config, workload = _workload(tmp_path)
    receipt = run_paired_replay(config, _ReplayClient(), run_id="run-report")
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
    receipt = run_paired_replay(config, _ReplayClient(), run_id="run-report")
    epoch = read_epoch(config.epoch_path)
    report_path = config.epoch_path.parent / "paired-report.json"
    report = build_paired_report(config.epoch_path, config.manifest_path, config, receipt)
    write_paired_report(report_path, epoch, report)
    receipt.pairs[0].raw.artifact_path.parent.chmod(0o755)

    with pytest.raises(PairedReportError, match="response artifact directory must have mode 0700"):
        verify_paired_report(report_path, config.epoch_path, config.manifest_path, config)


def test_paired_report_rejects_recomputed_tampered_aggregate(tmp_path: Path) -> None:
    config, workload = _workload(tmp_path)
    receipt = run_paired_replay(config, _ReplayClient(), run_id="run-report")
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
    receipt = run_paired_replay(config, _ReplayClient(), run_id="run-report")
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
    admissions_before = sum(
        record.operation == "paired_admit"
        for record in read_access_audit(read_epoch(config.epoch_path).audit_path).records
    )
    receipt = run_paired_replay(config, _ReplayClient(), run_id="run-m5-ready")
    epoch = read_epoch(config.epoch_path)
    report_path = config.epoch_path.parent / "m5-ready-report.json"
    report = build_paired_report(config.epoch_path, config.manifest_path, config, receipt)

    write_paired_report(report_path, epoch, report)

    assert (
        main(
            [
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
    audit_rows = [(record.candidate_id, record.operation, record.split) for record in audit.records]
    assert all(
        candidate_id == "candidate-a" and split == "redesign"
        for candidate_id, _, split in audit_rows
    )
    assert (
        sum(operation == "paired_admit" for _, operation, _ in audit_rows) == admissions_before + 1
    )
    assert [operation for _, operation, _ in audit_rows].count("interaction_render") == (
        2 * config.repeat_count
    )


def test_paired_runner_rejects_nonprivate_artifact_root_before_source_access(
    tmp_path: Path,
) -> None:
    config, workload = _workload(tmp_path)
    config.artifact_root.mkdir(mode=0o755)
    config.artifact_root.chmod(0o755)
    audit_path = read_epoch(config.epoch_path).audit_path
    audit_before = read_access_audit(audit_path).head_digest

    with pytest.raises(PairedReplayError, match="artifact directory must have mode 0700"):
        run_paired_replay(config, _ReplayClient(), run_id="unsafe-root")

    assert read_access_audit(audit_path).head_digest == audit_before


def test_replay_cli_rejects_missing_credential_before_audit_or_source_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _ = _workload(tmp_path)
    monkeypatch.delenv(config.credential_environment, raising=False)
    config_path = config.epoch_path.parent / "paired-config.json"
    report_path = config.epoch_path.parent / "paired-report.json"
    write_paired_config(config_path, config)
    audit_path = read_epoch(config.epoch_path).audit_path
    audit_before = read_access_audit(audit_path).head_digest

    assert (
        main(
            [
                "replay",
                "run",
                "--config",
                str(config_path),
                "--run-id",
                "missing-credential",
                "--report",
                str(report_path),
            ]
        )
        == EXIT_PRIVATE_ARTIFACT
    )

    assert read_access_audit(audit_path).head_digest == audit_before
    assert not config.artifact_root.exists()


def test_replay_cli_returns_manifest_exit_for_interaction_render_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, _ = _workload(tmp_path)
    monkeypatch.setenv(config.credential_environment, "test-credential")
    config_path = config.epoch_path.parent / "paired-config.json"
    write_paired_config(config_path, config)
    monkeypatch.setattr(
        cli,
        "run_paired_replay",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            InteractionReceiptError("tampered receipt")
        ),
    )

    assert (
        main(
            [
                "replay",
                "run",
                "--config",
                str(config_path),
                "--run-id",
                "render-failure",
            ]
        )
        == EXIT_PRIVATE_ARTIFACT
    )
    assert "tampered receipt" in capsys.readouterr().err


def test_replay_cli_writes_default_private_report_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _ = _workload(tmp_path)
    monkeypatch.setenv(config.credential_environment, "test-credential")
    config_path = config.epoch_path.parent / "paired-config.json"
    write_paired_config(config_path, config)
    monkeypatch.setattr(cli, "OpenAIResponsesClient", _ReplayClient)

    assert (
        main(
            [
                "replay",
                "run",
                "--config",
                str(config_path),
                "--run-id",
                "default-report",
            ]
        )
        == EXIT_OK
    )
    assert (config.artifact_root / "default-report" / "paired-report.json").is_file()


def test_paired_runner_records_billed_raw_arm_before_failing_cost_cap(tmp_path: Path) -> None:
    config, _ = _workload(tmp_path)
    client = _ReplayClient(input_tokens=300_000)
    with pytest.raises(PairedReplayCostCapError, match="past the"):
        run_paired_replay(config, client, run_id="run-over-cap")

    assert [request.arm for request in client.requests] == ["raw"]
    assert (config.artifact_root / "run-over-cap" / "candidate-a" / "0000-raw.json").is_file()


def test_paired_runner_accounts_for_induced_codec_turns(tmp_path: Path) -> None:
    config, workload = _workload(tmp_path)
    client = _OutcomeClient(
        (_turn(),),
        (_turn(), _turn("induced")),
    )

    receipt = run_paired_replay(config, client, run_id="run-induced")

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

    receipt = run_paired_replay(config, client, run_id="run-unsupported")

    assert [request.arm for request in client.requests] == ["raw", "raw"]
    assert receipt.pairs == ()
    assert len(receipt.terminated_pairs) == config.repeat_count
    terminated = receipt.terminated_pairs[0]
    assert terminated.raw.turn_accounting.unsupported_turn_count == 1
    assert terminated.raw.artifact_path.is_file()
    assert "snapshot has no matching tool call" in terminated.raw.artifact_path.read_text(
        encoding="utf-8"
    )


def _codec_unsupported_receipt(tmp_path: Path) -> PairedRunReceipt:
    config, workload = _workload(tmp_path)
    return run_paired_replay(
        config,
        _OutcomeClient(
            (_turn(),),
            (_turn("unsupported", unsupported_reason="provider rejected tool call"),),
        ),
        run_id="run-codec-unsupported",
    )


def test_completed_pair_rejects_unsupported_turns(tmp_path: Path) -> None:
    terminated = _codec_unsupported_receipt(tmp_path).terminated_pairs[0]
    assert terminated.codec is not None

    with pytest.raises(PairedReplayError, match="completed pair cannot contain unsupported"):
        PairedPairReceipt(terminated.raw, terminated.codec)


def test_report_receipt_rejects_unsupported_completed_pair(tmp_path: Path) -> None:
    document = _codec_unsupported_receipt(tmp_path).to_document()
    document["pairs"] = document["terminated_pairs"]
    document["terminated_pairs"] = []

    with pytest.raises(PairedReportError, match="completed paired receipt contains unsupported"):
        _receipt_document_arms(document)


def test_runner_rejects_induced_raw_turns_and_nonterminal_unsupported_turns(
    tmp_path: Path,
) -> None:
    with pytest.raises(PairedReplayError, match="must terminate"):
        PairedReplayResponse((_turn("unsupported", unsupported_reason="unsupported"), _turn()))

    config, workload = _workload(tmp_path)
    client = _OutcomeClient((_turn("induced"),), (_turn(),))
    with pytest.raises(PairedReplayError, match="raw arm must not"):
        run_paired_replay(config, client, run_id="run-invalid")
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

    assert main(["replay", "verify-config", "--config", str(config_path)]) == EXIT_OK
    assert "verified paired replay config" in capsys.readouterr().out

    bad_root = replace(config, artifact_root=tmp_path / "private" / "bad-artifacts")
    bad_root.artifact_root.mkdir(mode=0o755)
    bad_root.artifact_root.chmod(0o755)
    client = _ReplayClient()
    with pytest.raises(PairedReplayError, match="mode 0700"):
        run_paired_replay(bad_root, client, run_id="run-private")
    assert client.requests == []


def test_runner_rejects_unsafe_or_symlinked_artifact_paths_before_request(
    tmp_path: Path,
) -> None:
    config, _ = _workload(tmp_path)
    client = _ReplayClient()

    with pytest.raises(PairedReplayError, match="simple names"):
        run_paired_replay(config, client, run_id="../escape")
    assert client.requests == []

    target = tmp_path / "private" / "outside"
    target.mkdir(mode=0o700)
    (config.artifact_root / "run-link").symlink_to(target, target_is_directory=True)
    with pytest.raises(PairedReplayError, match="non-symlink directory"):
        run_paired_replay(config, client, run_id="run-link")
    assert client.requests == []


def test_paired_runner_rejects_artifact_root_outside_sealed_epoch(
    tmp_path: Path,
) -> None:
    config, _ = _workload(tmp_path)
    outside = replace(config, artifact_root=tmp_path / "outside-private-root")
    client = _ReplayClient()

    with pytest.raises(PairedReplayError, match="sealed approved private roots"):
        run_paired_replay(outside, client, run_id="outside-root")

    assert client.requests == []


def test_paired_config_cli_rejects_nonprivate_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    config_path = tmp_path / "private" / "paired-replay.json"
    write_paired_config(config_path, config)
    config_path.chmod(0o644)

    assert main(["replay", "verify-config", "--config", str(config_path)]) == EXIT_PRIVATE_ARTIFACT
    assert "mode 0600" in capsys.readouterr().err
