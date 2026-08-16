"""Private, immutable configuration and provenance contracts for paired replay."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast
from urllib.parse import urlsplit

from tools.paired_replay.epoch import EpochError, SealedEpoch, verify_epoch_manifest
from tools.paired_replay.evidence import JsonScalar
from tools.paired_replay.interaction import (
    InteractionReceiptError,
    read_interaction_receipt,
    verify_interaction_receipt,
)
from tools.paired_replay.manifest import is_sha256

PAIRED_REPLAY_CONFIG_SCHEMA_VERSION = 7

Arm = Literal["raw", "codec"]
Split = Literal["redesign", "holdout"]


class PairedReplayConfigError(ValueError):
    """Raised when a paired replay configuration is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class PriceTable:
    """One explicit provider price table, denominated in USD per million tokens."""

    effective_date: str
    input_per_mtok: str
    cache_read_per_mtok: str
    cache_write_per_mtok: str
    output_per_mtok: str

    def __post_init__(self) -> None:
        try:
            date.fromisoformat(self.effective_date)
        except ValueError as error:
            raise PairedReplayConfigError("price effective_date must be ISO-8601") from error
        _positive_decimal(self.input_per_mtok, "input_per_mtok")
        _non_negative_decimal(self.cache_read_per_mtok, "cache_read_per_mtok")
        _non_negative_decimal(self.cache_write_per_mtok, "cache_write_per_mtok")
        _positive_decimal(self.output_per_mtok, "output_per_mtok")

    def to_payload(self) -> dict[str, str]:
        """Return the canonical price-table serialization."""
        return {
            "cache_read_per_mtok": self.cache_read_per_mtok,
            "cache_write_per_mtok": self.cache_write_per_mtok,
            "effective_date": self.effective_date,
            "input_per_mtok": self.input_per_mtok,
            "output_per_mtok": self.output_per_mtok,
        }


@dataclass(frozen=True, slots=True)
class UsageMapping:
    """Declared native usage fields required to account for a live response."""

    input_field: str
    cache_read_field: str
    cache_write_field: str | None
    output_field: str
    input_includes_cache: bool

    def __post_init__(self) -> None:
        fields = (
            self.input_field,
            self.cache_read_field,
            self.output_field,
        )
        if self.cache_write_field is not None:
            fields += (self.cache_write_field,)
        if any(not field.strip() for field in fields):
            raise PairedReplayConfigError("usage mapping fields must not be empty")
        if len(set(fields)) != len(fields):
            raise PairedReplayConfigError("usage mapping fields must be distinct")
        if not isinstance(self.input_includes_cache, bool):
            raise PairedReplayConfigError("input_includes_cache must be a boolean")

    def to_payload(self) -> dict[str, object]:
        """Return the canonical native-to-billable field mapping."""
        return {
            "cache_read_field": self.cache_read_field,
            "cache_write_field": self.cache_write_field,
            "input_field": self.input_field,
            "input_includes_cache": self.input_includes_cache,
            "output_field": self.output_field,
        }


@dataclass(frozen=True, slots=True)
class ProviderRouting:
    """The provider-routing controls bound into every replay request.

    ``None`` on the config means the provider has no routing surface (e.g. a direct
    single-upstream endpoint). A provider whose contract requires routing must set it.
    """

    only: tuple[str, ...]
    allow_fallbacks: bool
    require_parameters: bool

    def __post_init__(self) -> None:
        if not self.only or any(not provider.strip() for provider in self.only):
            raise PairedReplayConfigError("provider routing must name at least one provider")
        if len(set(self.only)) != len(self.only):
            raise PairedReplayConfigError("provider routing providers must be unique")
        if not isinstance(self.allow_fallbacks, bool) or not isinstance(
            self.require_parameters, bool
        ):
            raise PairedReplayConfigError("provider routing flags must be booleans")

    def to_payload(self) -> dict[str, object]:
        """Return the canonical request-routing serialization."""
        return {
            "allow_fallbacks": self.allow_fallbacks,
            "only": list(self.only),
            "require_parameters": self.require_parameters,
        }


@dataclass(frozen=True, slots=True)
class InteractionReceiptBinding:
    """One chronological receipt that authorizes a configured redesign workload."""

    candidate_id: str
    receipt_path: Path
    receipt_digest: str

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise PairedReplayConfigError("interaction receipt candidate_id must not be empty")
        if not self.receipt_path.is_absolute():
            raise PairedReplayConfigError("interaction receipt path must be absolute")
        object.__setattr__(self, "receipt_path", self.receipt_path.resolve(strict=False))
        if not is_sha256(self.receipt_digest):
            raise PairedReplayConfigError("interaction receipt digest must be 64 lowercase hex")

    def to_payload(self) -> dict[str, str]:
        """Return the digest-bound non-content receipt reference."""
        return {
            "candidate_id": self.candidate_id,
            "receipt_digest": self.receipt_digest,
            "receipt_path": str(self.receipt_path),
        }


@dataclass(frozen=True, slots=True)
class PairedReplayConfig:
    """Every declared setting shared by raw and codec replay arms.

    Secrets do not belong to this value. A provider adapter receives credentials only
    from its process environment when a run is explicitly authorized.
    """

    epoch_digest: str
    epoch_path: Path
    manifest_path: Path
    eligibility_ledger_path: Path
    environment_ledger_path: Path
    artifact_root: Path
    provider: str
    endpoint: str
    api_version: str
    credential_environment: str
    privacy_boundary: str
    model: str
    decoding_parameters: Mapping[str, JsonScalar]
    seed_supported: bool
    seed: str | None
    repeat_count: int
    split: Split
    candidate_ids: tuple[str, ...]
    interaction_receipts: tuple[InteractionReceiptBinding, ...]
    pricing: PriceTable
    pricing_source: str
    usage_mapping: UsageMapping
    cost_cap_per_pair_usd: str
    cost_cap_run_usd: str
    unsupported_policy: Literal["terminate_pair"]
    induced_policy: Literal["include_in_codec_cost"]
    provider_routing: ProviderRouting | None
    containment_mechanism: Literal["provider_allowed_tools", "empirical_single_tool_declaration"]

    def __post_init__(self) -> None:
        for field_name, value in (
            ("provider", self.provider),
            ("endpoint", self.endpoint),
            ("api_version", self.api_version),
            ("credential_environment", self.credential_environment),
            ("privacy_boundary", self.privacy_boundary),
            ("model", self.model),
            ("pricing_source", self.pricing_source),
        ):
            if not value.strip():
                raise PairedReplayConfigError(f"{field_name} must not be empty")
        _validate_endpoint(self.endpoint)
        for field_name, path in (
            ("epoch_path", self.epoch_path),
            ("manifest_path", self.manifest_path),
            ("eligibility_ledger_path", self.eligibility_ledger_path),
            ("environment_ledger_path", self.environment_ledger_path),
            ("artifact_root", self.artifact_root),
        ):
            if not path.is_absolute():
                raise PairedReplayConfigError(f"{field_name} must be absolute")
            object.__setattr__(self, field_name, path.resolve(strict=False))
        if not is_sha256(self.epoch_digest):
            raise PairedReplayConfigError("epoch_digest must be 64 lowercase hex")
        frozen_parameters = _freeze_parameters(self.decoding_parameters)
        object.__setattr__(self, "decoding_parameters", frozen_parameters)
        if self.seed_supported:
            if self.seed is None or not self.seed.strip():
                raise PairedReplayConfigError("seed_supported requires a non-empty seed")
        elif self.seed is not None:
            raise PairedReplayConfigError("seed must be null when seed_supported is false")
        if self.repeat_count < 1:
            raise PairedReplayConfigError("repeat_count must be positive")
        if not self.seed_supported and self.repeat_count < 2:
            raise PairedReplayConfigError(
                "repeat_count must be at least 2 when seed control is unavailable"
            )
        if self.split not in {"redesign", "holdout"}:
            raise PairedReplayConfigError(f"unknown split {self.split!r}")
        if not self.candidate_ids or any(
            not candidate_id.strip() for candidate_id in self.candidate_ids
        ):
            raise PairedReplayConfigError("candidate_ids must contain non-empty identifiers")
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise PairedReplayConfigError("candidate_ids must be unique")
        if {binding.candidate_id for binding in self.interaction_receipts} != set(
            self.candidate_ids
        ) or len(self.interaction_receipts) != len(self.candidate_ids):
            raise PairedReplayConfigError(
                "interaction_receipts must bind each configured candidate exactly once"
            )
        _positive_decimal(self.cost_cap_per_pair_usd, "cost_cap_per_pair_usd")
        _positive_decimal(self.cost_cap_run_usd, "cost_cap_run_usd")
        if Decimal(self.cost_cap_per_pair_usd) > Decimal(self.cost_cap_run_usd):
            raise PairedReplayConfigError("cost_cap_per_pair_usd must not exceed cost_cap_run_usd")
        if self.unsupported_policy != "terminate_pair":
            raise PairedReplayConfigError("unsupported_policy must terminate_pair")
        if self.induced_policy != "include_in_codec_cost":
            raise PairedReplayConfigError("induced_policy must include_in_codec_cost")
        if (
            self.containment_mechanism == "provider_allowed_tools"
            and self.provider_routing is not None
        ):
            raise PairedReplayConfigError(
                "provider_allowed_tools containment must not declare provider routing"
            )
        if (
            self.containment_mechanism == "empirical_single_tool_declaration"
            and self.provider_routing is None
        ):
            raise PairedReplayConfigError(
                "empirical_single_tool_declaration containment requires provider routing"
            )
        if self.provider not in {"openai", "openrouter"}:
            raise PairedReplayConfigError(f"unknown provider {self.provider!r}")
        if self.containment_mechanism not in {
            "provider_allowed_tools",
            "empirical_single_tool_declaration",
        }:
            raise PairedReplayConfigError(
                f"unknown containment_mechanism {self.containment_mechanism!r}"
            )

    @property
    def digest(self) -> str:
        """Return a digest covering every declared shared replay setting."""
        return _digest(self.payload_without_digest())

    @property
    def settings_digest(self) -> str:
        """Return the digest binding every setting shared by the paired arms."""
        return self.digest

    def payload_without_digest(self) -> dict[str, object]:
        """Return canonical configuration content without its integrity digest."""
        return {
            "artifact_root": str(self.artifact_root),
            "candidate_ids": list(self.candidate_ids),
            "cost_cap_per_pair_usd": self.cost_cap_per_pair_usd,
            "cost_cap_run_usd": self.cost_cap_run_usd,
            "decoding_parameters": dict(self.decoding_parameters),
            "epoch_digest": self.epoch_digest,
            "eligibility_ledger_path": str(self.eligibility_ledger_path),
            "epoch_path": str(self.epoch_path),
            "endpoint": self.endpoint,
            "api_version": self.api_version,
            "credential_environment": self.credential_environment,
            "environment_ledger_path": str(self.environment_ledger_path),
            "induced_policy": self.induced_policy,
            "manifest_path": str(self.manifest_path),
            "model": self.model,
            "pricing": self.pricing.to_payload(),
            "privacy_boundary": self.privacy_boundary,
            "provider": self.provider,
            "repeat_count": self.repeat_count,
            "interaction_receipts": [
                binding.to_payload()
                for binding in sorted(self.interaction_receipts, key=lambda item: item.candidate_id)
            ],
            "schema_version": PAIRED_REPLAY_CONFIG_SCHEMA_VERSION,
            "seed": self.seed,
            "seed_supported": self.seed_supported,
            "split": self.split,
            "unsupported_policy": self.unsupported_policy,
            "pricing_source": self.pricing_source,
            "usage_mapping": self.usage_mapping.to_payload(),
            "provider_routing": (
                None if self.provider_routing is None else self.provider_routing.to_payload()
            ),
            "containment_mechanism": self.containment_mechanism,
        }

    def to_document(self) -> dict[str, object]:
        """Return private configuration content with its digest."""
        return {"digest": self.digest, **self.payload_without_digest()}


@dataclass(frozen=True, slots=True)
class PairedRunProvenance:
    """Non-content provenance binding one private response artifact to a replay arm."""

    config_digest: str
    manifest_digest: str
    eligibility_ledger_digest: str
    environment_ledger_digest: str
    interaction_receipt_digest: str
    condition_digest: str
    candidate_id: str
    source_sha256: str
    environment_digest: str
    run_id: str
    arm: Arm
    repeat_index: int
    response_artifact_sha256: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("config_digest", self.config_digest),
            ("manifest_digest", self.manifest_digest),
            ("eligibility_ledger_digest", self.eligibility_ledger_digest),
            ("environment_ledger_digest", self.environment_ledger_digest),
            ("interaction_receipt_digest", self.interaction_receipt_digest),
            ("condition_digest", self.condition_digest),
            ("source_sha256", self.source_sha256),
            ("environment_digest", self.environment_digest),
            ("response_artifact_sha256", self.response_artifact_sha256),
        ):
            if not is_sha256(value):
                raise PairedReplayConfigError(f"{field_name} must be 64 lowercase hex")
        if not self.candidate_id.strip() or not self.run_id.strip():
            raise PairedReplayConfigError("candidate_id and run_id must not be empty")
        if self.arm not in {"raw", "codec"}:
            raise PairedReplayConfigError(f"unknown arm {self.arm!r}")
        if self.repeat_index < 0:
            raise PairedReplayConfigError("repeat_index must be non-negative")

    def to_payload(self) -> dict[str, object]:
        """Return a non-content receipt payload suitable for private aggregation."""
        return {
            "arm": self.arm,
            "candidate_id": self.candidate_id,
            "config_digest": self.config_digest,
            "eligibility_ledger_digest": self.eligibility_ledger_digest,
            "condition_digest": self.condition_digest,
            "environment_digest": self.environment_digest,
            "environment_ledger_digest": self.environment_ledger_digest,
            "manifest_digest": self.manifest_digest,
            "interaction_receipt_digest": self.interaction_receipt_digest,
            "repeat_index": self.repeat_index,
            "response_artifact_sha256": self.response_artifact_sha256,
            "run_id": self.run_id,
            "source_sha256": self.source_sha256,
        }


def write_paired_config(path: Path, config: PairedReplayConfig) -> None:
    """Atomically write a digest-checked private configuration as mode 0600."""
    temporary: Path | None = None
    try:
        _ensure_private_directory(path.parent)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(json.dumps(config.to_document(), indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as error:
        raise PairedReplayConfigError(
            f"cannot write paired replay config {path}: {error}"
        ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_paired_config(path: Path) -> PairedReplayConfig:
    """Read, validate, and integrity-check a private paired replay configuration."""
    _require_private_path(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise PairedReplayConfigError(
            f"cannot read paired replay config {path}: {error}"
        ) from error
    except json.JSONDecodeError as error:
        raise PairedReplayConfigError(
            f"paired replay config is not valid JSON: {error.msg}"
        ) from error
    if not isinstance(document, dict) or set(document) != _DOCUMENT_FIELDS:
        raise PairedReplayConfigError("paired replay config has invalid fields")
    if document["schema_version"] != PAIRED_REPLAY_CONFIG_SCHEMA_VERSION:
        raise PairedReplayConfigError(
            f"unsupported paired replay config schema {document['schema_version']!r}"
        )
    digest = document["digest"]
    if not isinstance(digest, str) or not is_sha256(digest):
        raise PairedReplayConfigError("paired replay config digest must be 64 lowercase hex")
    config = _config_from_document(document)
    if not hmac.compare_digest(digest, config.digest):
        raise PairedReplayConfigError("paired replay config digest mismatch")
    return config


def build_execution_config(
    *,
    epoch_path: Path,
    manifest_path: Path,
    eligibility_ledger_path: Path,
    environment_ledger_path: Path,
    artifact_root: Path,
    interaction_receipt_paths: Sequence[Path],
    provider: Literal["openai", "openrouter"] = "openai",
) -> PairedReplayConfig:
    """Build the approved provider configuration from verified private receipts."""
    return _build_execution_config(
        epoch_path=epoch_path,
        manifest_path=manifest_path,
        eligibility_ledger_path=eligibility_ledger_path,
        environment_ledger_path=environment_ledger_path,
        artifact_root=artifact_root,
        interaction_receipt_paths=interaction_receipt_paths,
        config_output_path=None,
        provider=provider,
    )


def _build_execution_config(
    *,
    epoch_path: Path,
    manifest_path: Path,
    eligibility_ledger_path: Path,
    environment_ledger_path: Path,
    artifact_root: Path,
    interaction_receipt_paths: Sequence[Path],
    config_output_path: Path | None,
    provider: Literal["openai", "openrouter"] = "openai",
) -> PairedReplayConfig:
    """Build the one approved provider configuration from verified private receipts."""
    if not interaction_receipt_paths:
        raise PairedReplayConfigError("at least one interaction receipt is required")
    epoch_path = _normalized_absolute_path(epoch_path, "epoch_path")
    manifest_path = _normalized_absolute_path(manifest_path, "manifest_path")
    eligibility_ledger_path = _normalized_absolute_path(
        eligibility_ledger_path, "eligibility_ledger_path"
    )
    environment_ledger_path = _normalized_absolute_path(
        environment_ledger_path, "environment_ledger_path"
    )
    artifact_root = _normalized_absolute_path(artifact_root, "artifact_root")
    interaction_receipt_paths = tuple(
        _normalized_absolute_path(path, "interaction_receipt_path")
        for path in interaction_receipt_paths
    )
    if config_output_path is not None:
        config_output_path = _normalized_absolute_path(config_output_path, "config_output_path")
    try:
        epoch, _ = verify_epoch_manifest(epoch_path, manifest_path)
    except EpochError as error:
        raise PairedReplayConfigError(str(error)) from error
    _require_approved_private_path(eligibility_ledger_path, epoch, "eligibility_ledger_path")
    _require_approved_private_path(environment_ledger_path, epoch, "environment_ledger_path")
    _require_approved_private_path(artifact_root, epoch, "artifact_root")
    for index, receipt_path in enumerate(interaction_receipt_paths):
        _require_approved_private_path(receipt_path, epoch, f"interaction_receipt_path[{index}]")
    if len(set(interaction_receipt_paths)) != len(interaction_receipt_paths):
        raise PairedReplayConfigError("interaction receipt paths must be unique")
    if config_output_path is not None:
        _require_approved_private_path(config_output_path, epoch, "config_output_path")
        _require_distinct_config_output_path(
            config_output_path,
            epoch_path,
            manifest_path,
            eligibility_ledger_path,
            environment_ledger_path,
            epoch.audit_path,
            interaction_receipt_paths,
        )
    receipt_headers = _read_unique_receipt_headers(interaction_receipt_paths)
    bindings: list[InteractionReceiptBinding] = []
    for receipt_path, header in zip(interaction_receipt_paths, receipt_headers, strict=True):
        try:
            receipt = verify_interaction_receipt(
                receipt_path,
                epoch_path,
                manifest_path,
                eligibility_ledger_path,
                environment_ledger_path,
            )
        except InteractionReceiptError as error:
            raise PairedReplayConfigError(
                f"interaction receipt {receipt_path} is invalid: {error}"
            ) from error
        if receipt.digest != header[1] or receipt.candidate_id != header[0]:
            raise PairedReplayConfigError(
                f"interaction receipt {receipt_path} changed during verification"
            )
        bindings.append(
            InteractionReceiptBinding(
                receipt.candidate_id,
                receipt_path,
                receipt.digest,
            )
        )
    bindings.sort(key=lambda binding: binding.candidate_id)
    contract = _OPENAI_CONTRACT if provider == "openai" else _OPENROUTER_CONTRACT
    return PairedReplayConfig(
        epoch_digest=epoch.digest,
        epoch_path=epoch_path,
        manifest_path=manifest_path,
        eligibility_ledger_path=eligibility_ledger_path,
        environment_ledger_path=environment_ledger_path,
        artifact_root=artifact_root,
        provider=contract.provider,
        endpoint=contract.endpoint,
        api_version=contract.api_version,
        credential_environment=contract.credential_environment,
        privacy_boundary=contract.privacy_boundary,
        model=contract.model,
        decoding_parameters=contract.decoding_parameters,
        seed_supported=contract.seed_supported,
        seed=contract.seed,
        repeat_count=contract.repeat_count,
        split=contract.split,
        candidate_ids=tuple(binding.candidate_id for binding in bindings),
        interaction_receipts=tuple(bindings),
        pricing=contract.pricing,
        pricing_source=contract.pricing_source,
        usage_mapping=contract.usage_mapping,
        cost_cap_per_pair_usd="0.20",
        cost_cap_run_usd="0.80",
        unsupported_policy="terminate_pair",
        induced_policy="include_in_codec_cost",
        provider_routing=contract.provider_routing,
        containment_mechanism=contract.containment_mechanism,
    )


def create_execution_config(
    *,
    epoch_path: Path,
    manifest_path: Path,
    eligibility_ledger_path: Path,
    environment_ledger_path: Path,
    artifact_root: Path,
    interaction_receipt_paths: Sequence[Path],
    config_path: Path,
    provider: Literal["openai", "openrouter"] = "openai",
) -> PairedReplayConfig:
    """Create a mode-0600 approved configuration from verified private receipts."""
    config_path = _normalized_absolute_path(config_path, "config_path")
    config = _build_execution_config(
        epoch_path=epoch_path,
        manifest_path=manifest_path,
        eligibility_ledger_path=eligibility_ledger_path,
        environment_ledger_path=environment_ledger_path,
        artifact_root=artifact_root,
        interaction_receipt_paths=interaction_receipt_paths,
        config_output_path=config_path,
        provider=provider,
    )
    write_paired_config(config_path, config)
    return config


def verify_provider_contract(config: PairedReplayConfig) -> None:
    """Verify the approved direct provider pin without reading a chronological receipt."""
    if config.provider == "openai":
        _validate_openai_contract(config)
    elif config.provider == "openrouter":
        _validate_openrouter_contract(config)
    else:
        raise PairedReplayConfigError(f"unknown provider {config.provider!r}")


def verify_execution_config(config: PairedReplayConfig) -> None:
    """Verify the one approved provider contract and every chronological receipt binding."""
    verify_provider_contract(config)
    try:
        epoch, _ = verify_epoch_manifest(config.epoch_path, config.manifest_path)
    except EpochError as error:
        raise PairedReplayConfigError(str(error)) from error
    _require_approved_private_path(config.eligibility_ledger_path, epoch, "eligibility_ledger_path")
    _require_approved_private_path(config.environment_ledger_path, epoch, "environment_ledger_path")
    _require_approved_private_path(config.artifact_root, epoch, "artifact_root")
    for index, binding in enumerate(config.interaction_receipts):
        _require_approved_private_path(
            binding.receipt_path, epoch, f"interaction_receipt_path[{index}]"
        )
    for binding in config.interaction_receipts:
        try:
            receipt = verify_interaction_receipt(
                binding.receipt_path,
                config.epoch_path,
                config.manifest_path,
                config.eligibility_ledger_path,
                config.environment_ledger_path,
            )
        except InteractionReceiptError as error:
            raise PairedReplayConfigError(
                f"interaction receipt for {binding.candidate_id!r} is invalid: {error}"
            ) from error
        if (
            receipt.digest != binding.receipt_digest
            or receipt.candidate_id != binding.candidate_id
            or receipt.epoch_digest != config.epoch_digest
        ):
            raise PairedReplayConfigError(
                f"interaction receipt for {binding.candidate_id!r} does not match configuration"
            )


def _validate_openai_contract(config: PairedReplayConfig) -> None:
    if (
        config.provider != "openai"
        or config.endpoint != "https://api.openai.com/v1/responses"
        or config.api_version != "v1"
        or config.credential_environment != "OPENAI_API_KEY"
        or config.privacy_boundary
        != "openai-store-false-abuse-monitoring-30-days-prompt-cache-retention-in-memory"
    ):
        raise PairedReplayConfigError("configuration does not match the approved OpenAI contract")
    if config.model != "gpt-5.4-mini-2026-03-17":
        raise PairedReplayConfigError("OpenAI model does not match the approved provider contract")
    if config.split != "redesign":
        raise PairedReplayConfigError("OpenAI replay requires the redesign split")
    if config.seed_supported or config.seed is not None:
        raise PairedReplayConfigError("OpenAI replay does not support a seed")
    if config.repeat_count != 2:
        raise PairedReplayConfigError("OpenAI replay requires exactly two seedless repeats")
    if config.decoding_parameters != {
        "max_output_tokens": 4096,
        "parallel_tool_calls": True,
        "prompt_cache_retention": "in_memory",
        "reasoning_effort": "none",
        "store": False,
        "stream": False,
        "temperature": 0.0,
    }:
        raise PairedReplayConfigError(
            "OpenAI decoding parameters do not match the approved provider contract"
        )
    if (
        config.pricing.to_payload()
        != {
            "effective_date": "2026-08-04",
            "input_per_mtok": "0.75",
            "cache_read_per_mtok": "0.075",
            "cache_write_per_mtok": "0.75",
            "output_per_mtok": "4.50",
        }
        or config.pricing_source != "https://openai.com/api/pricing/"
    ):
        raise PairedReplayConfigError("OpenAI pricing contract is unapproved")
    if config.usage_mapping.to_payload() != {
        "input_field": "usage.input_tokens",
        "input_includes_cache": True,
        "cache_read_field": "usage.input_tokens_details.cached_tokens",
        "cache_write_field": None,
        "output_field": "usage.output_tokens",
    }:
        raise PairedReplayConfigError("OpenAI native usage mapping is unapproved")
    if Decimal(config.cost_cap_per_pair_usd) != Decimal("0.20") or Decimal(
        config.cost_cap_run_usd
    ) != Decimal("0.80"):
        raise PairedReplayConfigError("OpenAI cost caps do not match the approved contract")
    if config.containment_mechanism != "provider_allowed_tools":
        raise PairedReplayConfigError("OpenAI contract requires provider_allowed_tools containment")


def _validate_openrouter_contract(config: PairedReplayConfig) -> None:
    if (
        config.provider != "openrouter"
        or config.endpoint != "https://openrouter.ai/api/v1/chat/completions"
        or config.api_version != "v1"
        or config.credential_environment != "OPENROUTER_API_KEY"
        or config.privacy_boundary
        != "openrouter-prompt-logging-disabled-anthropic-commercial-retention-30-days"
    ):
        raise PairedReplayConfigError(
            "configuration does not match the approved OpenRouter contract"
        )
    if config.model != "anthropic/claude-haiku-4.5":
        raise PairedReplayConfigError(
            "OpenRouter model does not match the approved provider contract"
        )
    if config.provider_routing is None or config.provider_routing.to_payload() != {
        "allow_fallbacks": False,
        "only": ["anthropic"],
        "require_parameters": True,
    }:
        raise PairedReplayConfigError("OpenRouter routing contract is unapproved")
    if config.split != "redesign":
        raise PairedReplayConfigError("OpenRouter replay requires the redesign split")
    if config.seed_supported or config.seed is not None:
        raise PairedReplayConfigError("OpenRouter replay does not support a seed")
    if config.repeat_count != 2:
        raise PairedReplayConfigError("OpenRouter replay requires exactly two seedless repeats")
    if config.decoding_parameters != {"max_tokens": 4096, "temperature": 0.0}:
        raise PairedReplayConfigError(
            "OpenRouter decoding parameters do not match the approved provider contract"
        )
    if (
        config.pricing.to_payload()
        != {
            "effective_date": "2026-08-01",
            "input_per_mtok": "1",
            "cache_read_per_mtok": "0.10",
            "cache_write_per_mtok": "1.25",
            "output_per_mtok": "5",
        }
        or config.pricing_source
        != "https://openrouter.ai/api/v1/models/anthropic/claude-haiku-4.5/endpoints"
    ):
        raise PairedReplayConfigError("OpenRouter pricing contract is unapproved")
    if config.usage_mapping.to_payload() != {
        "input_field": "usage.prompt_tokens",
        "input_includes_cache": True,
        "cache_read_field": "usage.prompt_tokens_details.cached_tokens",
        "cache_write_field": "usage.prompt_tokens_details.cache_write_tokens",
        "output_field": "usage.completion_tokens",
    }:
        raise PairedReplayConfigError("OpenRouter native usage mapping is unapproved")
    if Decimal(config.cost_cap_per_pair_usd) != Decimal("0.20") or Decimal(
        config.cost_cap_run_usd
    ) != Decimal("0.80"):
        raise PairedReplayConfigError("OpenRouter cost caps do not match the approved contract")
    if config.containment_mechanism != "empirical_single_tool_declaration":
        raise PairedReplayConfigError(
            "OpenRouter contract requires empirical_single_tool_declaration containment"
        )


def _config_from_document(document: dict[str, object]) -> PairedReplayConfig:
    pricing = _mapping(document["pricing"], "pricing", _PRICE_FIELDS)
    usage_mapping = _mapping(document["usage_mapping"], "usage_mapping", _USAGE_MAPPING_FIELDS)
    candidate_ids = document["candidate_ids"]
    if not isinstance(candidate_ids, list) or any(
        not isinstance(candidate_id, str) for candidate_id in candidate_ids
    ):
        raise PairedReplayConfigError("candidate_ids must be an array of strings")
    interaction_receipts = _interaction_receipts(document["interaction_receipts"])
    decoding_parameters = document["decoding_parameters"]
    if not isinstance(decoding_parameters, dict):
        raise PairedReplayConfigError("decoding_parameters must be an object")
    _canonical_json(decoding_parameters, "decoding_parameters")
    seed_supported = document["seed_supported"]
    if not isinstance(seed_supported, bool):
        raise PairedReplayConfigError("seed_supported must be a boolean")
    seed = document["seed"]
    if seed is not None and not isinstance(seed, str):
        raise PairedReplayConfigError("seed must be a string or null")
    repeat_count = document["repeat_count"]
    if not isinstance(repeat_count, int) or isinstance(repeat_count, bool):
        raise PairedReplayConfigError("repeat_count must be an integer")
    split = document["split"]
    if split not in {"redesign", "holdout"}:
        raise PairedReplayConfigError(f"split is invalid: {split!r}")
    unsupported_policy = document["unsupported_policy"]
    induced_policy = document["induced_policy"]
    if unsupported_policy != "terminate_pair" or induced_policy != "include_in_codec_cost":
        raise PairedReplayConfigError("paired replay policies are invalid")
    cache_write_field = usage_mapping["cache_write_field"]
    if cache_write_field is not None and (
        not isinstance(cache_write_field, str) or not cache_write_field.strip()
    ):
        raise PairedReplayConfigError("cache_write_field must be a non-empty string or null")
    provider_routing_value = document["provider_routing"]
    provider_routing = (
        None if provider_routing_value is None else _provider_routing(provider_routing_value)
    )
    containment_mechanism = document["containment_mechanism"]
    if containment_mechanism not in {
        "provider_allowed_tools",
        "empirical_single_tool_declaration",
    }:
        raise PairedReplayConfigError(
            f"containment_mechanism is invalid: {containment_mechanism!r}"
        )
    return PairedReplayConfig(
        epoch_digest=_required_text(document, "epoch_digest"),
        epoch_path=_absolute_path(document, "epoch_path"),
        manifest_path=_absolute_path(document, "manifest_path"),
        eligibility_ledger_path=_absolute_path(document, "eligibility_ledger_path"),
        environment_ledger_path=_absolute_path(document, "environment_ledger_path"),
        artifact_root=_absolute_path(document, "artifact_root"),
        provider=_required_text(document, "provider"),
        endpoint=_required_text(document, "endpoint"),
        api_version=_required_text(document, "api_version"),
        credential_environment=_required_text(document, "credential_environment"),
        privacy_boundary=_required_text(document, "privacy_boundary"),
        model=_required_text(document, "model"),
        decoding_parameters=cast(dict[str, JsonScalar], decoding_parameters),
        seed_supported=seed_supported,
        seed=seed,
        repeat_count=repeat_count,
        split=split,
        candidate_ids=tuple(candidate_ids),
        interaction_receipts=interaction_receipts,
        pricing=PriceTable(
            _required_text(pricing, "effective_date"),
            _required_text(pricing, "input_per_mtok"),
            _required_text(pricing, "cache_read_per_mtok"),
            _required_text(pricing, "cache_write_per_mtok"),
            _required_text(pricing, "output_per_mtok"),
        ),
        pricing_source=_required_text(document, "pricing_source"),
        usage_mapping=UsageMapping(
            _required_text(usage_mapping, "input_field"),
            _required_text(usage_mapping, "cache_read_field"),
            cache_write_field,
            _required_text(usage_mapping, "output_field"),
            _required_bool(usage_mapping, "input_includes_cache"),
        ),
        cost_cap_per_pair_usd=_required_text(document, "cost_cap_per_pair_usd"),
        cost_cap_run_usd=_required_text(document, "cost_cap_run_usd"),
        unsupported_policy=unsupported_policy,
        induced_policy=induced_policy,
        provider_routing=provider_routing,
        containment_mechanism=containment_mechanism,
    )


def _normalized_absolute_path(path: Path, field_name: str) -> Path:
    if not path.is_absolute():
        raise PairedReplayConfigError(f"{field_name} must be absolute")
    return path.resolve(strict=False)


def _require_approved_private_path(path: Path, epoch: SealedEpoch, field_name: str) -> None:
    if not any(path.is_relative_to(approved_root) for approved_root in epoch.approved_roots):
        raise PairedReplayConfigError(f"{field_name} must be within sealed approved private roots")


def _require_distinct_config_output_path(
    config_path: Path,
    epoch_path: Path,
    manifest_path: Path,
    eligibility_ledger_path: Path,
    environment_ledger_path: Path,
    audit_path: Path,
    interaction_receipt_paths: Sequence[Path],
) -> None:
    sealed_paths = (
        epoch_path,
        manifest_path,
        eligibility_ledger_path,
        environment_ledger_path,
        audit_path,
        *interaction_receipt_paths,
    )
    if config_path in sealed_paths:
        raise PairedReplayConfigError(
            "config_output_path must not overwrite sealed private evidence"
        )


def _read_unique_receipt_headers(
    receipt_paths: Sequence[Path],
) -> tuple[tuple[str, str], ...]:
    headers: list[tuple[str, str]] = []
    candidate_ids: set[str] = set()
    for receipt_path in receipt_paths:
        try:
            receipt = read_interaction_receipt(receipt_path)
        except InteractionReceiptError as error:
            raise PairedReplayConfigError(
                f"interaction receipt {receipt_path} is invalid: {error}"
            ) from error
        if receipt.candidate_id in candidate_ids:
            raise PairedReplayConfigError("interaction receipt candidate_ids must be unique")
        candidate_ids.add(receipt.candidate_id)
        headers.append((receipt.candidate_id, receipt.digest))
    return tuple(headers)


def _interaction_receipts(value: object) -> tuple[InteractionReceiptBinding, ...]:
    if not isinstance(value, list):
        raise PairedReplayConfigError("interaction_receipts must be an array")
    bindings: list[InteractionReceiptBinding] = []
    for item in value:
        binding = _mapping(item, "interaction_receipt", _INTERACTION_RECEIPT_FIELDS)
        bindings.append(
            InteractionReceiptBinding(
                _required_text(binding, "candidate_id"),
                _absolute_path(binding, "receipt_path"),
                _required_text(binding, "receipt_digest"),
            )
        )
    return tuple(bindings)


def _provider_routing(value: object) -> ProviderRouting:
    routing = _mapping(value, "provider_routing", _PROVIDER_ROUTING_FIELDS)
    only = routing["only"]
    if not isinstance(only, list) or any(not isinstance(item, str) for item in only):
        raise PairedReplayConfigError("provider_routing.only must be an array of strings")
    return ProviderRouting(
        tuple(only),
        _required_bool(routing, "allow_fallbacks"),
        _required_bool(routing, "require_parameters"),
    )


def _non_negative_decimal(value: str, field_name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise PairedReplayConfigError(f"{field_name} must be a decimal string") from error
    if not parsed.is_finite() or parsed < 0:
        raise PairedReplayConfigError(f"{field_name} must be a non-negative finite decimal")
    return parsed


def _positive_decimal(value: str, field_name: str) -> Decimal:
    parsed = _non_negative_decimal(value, field_name)
    if parsed == 0:
        raise PairedReplayConfigError(f"{field_name} must be positive")
    return parsed


@dataclass(frozen=True, slots=True)
class _ProviderContractFields:
    """The provider-specific portion of one approved paired-replay execution contract."""

    provider: str
    endpoint: str
    api_version: str
    credential_environment: str
    privacy_boundary: str
    model: str
    decoding_parameters: Mapping[str, JsonScalar]
    seed_supported: bool
    seed: str | None
    repeat_count: int
    split: Split
    pricing: PriceTable
    pricing_source: str
    usage_mapping: UsageMapping
    provider_routing: ProviderRouting | None
    containment_mechanism: Literal["provider_allowed_tools", "empirical_single_tool_declaration"]


_OPENAI_CONTRACT = _ProviderContractFields(
    provider="openai",
    endpoint="https://api.openai.com/v1/responses",
    api_version="v1",
    credential_environment="OPENAI_API_KEY",
    privacy_boundary=(
        "openai-store-false-abuse-monitoring-30-days-prompt-cache-retention-in-memory"
    ),
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
    pricing=PriceTable("2026-08-04", "0.75", "0.075", "0.75", "4.50"),
    pricing_source="https://openai.com/api/pricing/",
    usage_mapping=UsageMapping(
        input_field="usage.input_tokens",
        cache_read_field="usage.input_tokens_details.cached_tokens",
        cache_write_field=None,
        output_field="usage.output_tokens",
        input_includes_cache=True,
    ),
    provider_routing=None,
    containment_mechanism="provider_allowed_tools",
)

_OPENROUTER_CONTRACT = _ProviderContractFields(
    provider="openrouter",
    endpoint="https://openrouter.ai/api/v1/chat/completions",
    api_version="v1",
    credential_environment="OPENROUTER_API_KEY",
    privacy_boundary=("openrouter-prompt-logging-disabled-anthropic-commercial-retention-30-days"),
    model="anthropic/claude-haiku-4.5",
    decoding_parameters={
        "max_tokens": 4096,
        "temperature": 0.0,
    },
    seed_supported=False,
    seed=None,
    repeat_count=2,
    split="redesign",
    pricing=PriceTable("2026-08-01", "1", "0.10", "1.25", "5"),
    pricing_source="https://openrouter.ai/api/v1/models/anthropic/claude-haiku-4.5/endpoints",
    usage_mapping=UsageMapping(
        input_field="usage.prompt_tokens",
        cache_read_field="usage.prompt_tokens_details.cached_tokens",
        cache_write_field="usage.prompt_tokens_details.cache_write_tokens",
        output_field="usage.completion_tokens",
        input_includes_cache=True,
    ),
    provider_routing=ProviderRouting(
        only=("anthropic",),
        allow_fallbacks=False,
        require_parameters=True,
    ),
    containment_mechanism="empirical_single_tool_declaration",
)


def _canonical_json(value: object, field_name: str) -> None:
    try:
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise PairedReplayConfigError(f"{field_name} must be canonical JSON") from error


def _freeze_parameters(parameters: Mapping[str, JsonScalar]) -> Mapping[str, JsonScalar]:
    if not parameters:
        raise PairedReplayConfigError("decoding_parameters must declare every setting")
    frozen: dict[str, JsonScalar] = {}
    for key, value in parameters.items():
        if not isinstance(key, str) or not key.strip():
            raise PairedReplayConfigError("decoding parameter keys must be non-empty strings")
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            raise PairedReplayConfigError("decoding parameters must contain only JSON scalars")
        frozen[key] = value
    _canonical_json(frozen, "decoding_parameters")
    return MappingProxyType(frozen)


def _validate_endpoint(endpoint: str) -> None:
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as error:
        raise PairedReplayConfigError("endpoint must be a valid HTTPS URL") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise PairedReplayConfigError(
            "endpoint must be an HTTPS URL without credentials, query, or fragment"
        )


def _mapping(value: object, field_name: str, expected_fields: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise PairedReplayConfigError(f"{field_name} has invalid fields")
    return value


def _absolute_path(document: dict[str, object], field_name: str) -> Path:
    value = _required_text(document, field_name)
    path = Path(value)
    if not path.is_absolute():
        raise PairedReplayConfigError(f"{field_name} must be absolute")
    return path.resolve(strict=False)


def _required_text(document: dict[str, object], field_name: str) -> str:
    value = document.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise PairedReplayConfigError(f"{field_name} must be a non-empty string")
    return value


def _required_bool(document: Mapping[str, object], field_name: str) -> bool:
    value = document.get(field_name)
    if not isinstance(value, bool):
        raise PairedReplayConfigError(f"{field_name} must be a boolean")
    return value


def _require_private_path(path: Path) -> None:
    try:
        entry_stat = path.lstat()
    except OSError as error:
        raise PairedReplayConfigError(
            f"cannot stat paired replay config {path}: {error}"
        ) from error
    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISREG(entry_stat.st_mode):
        raise PairedReplayConfigError("paired replay config must be a non-symlink regular file")
    if entry_stat.st_uid != os.getuid():
        raise PairedReplayConfigError("paired replay config must be owned by the current user")
    mode = entry_stat.st_mode & 0o777
    if mode != 0o600:
        raise PairedReplayConfigError(f"paired replay config must have mode 0600, found {mode:04o}")
    _ensure_private_directory(path.parent)


def _ensure_private_directory(path: Path) -> None:
    missing: list[Path] = []
    ancestor = path
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
    try:
        entry_stat = path.lstat()
    except OSError as error:
        raise PairedReplayConfigError(
            f"cannot stat paired replay config directory {path}: {error}"
        ) from error
    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
        raise PairedReplayConfigError(
            "paired replay config directory must be a non-symlink directory"
        )
    if entry_stat.st_uid != os.getuid():
        raise PairedReplayConfigError(
            "paired replay config directory must be owned by the current user"
        )
    mode = entry_stat.st_mode & 0o777
    if mode != 0o700:
        raise PairedReplayConfigError(
            f"paired replay config directory must have mode 0700, found {mode:04o}"
        )


def _digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_PRICE_FIELDS = frozenset(
    {
        "effective_date",
        "input_per_mtok",
        "cache_read_per_mtok",
        "cache_write_per_mtok",
        "output_per_mtok",
    }
)
_USAGE_MAPPING_FIELDS = frozenset(
    {
        "input_field",
        "input_includes_cache",
        "cache_read_field",
        "cache_write_field",
        "output_field",
    }
)
_PROVIDER_ROUTING_FIELDS = frozenset({"allow_fallbacks", "only", "require_parameters"})


_DOCUMENT_FIELDS = frozenset(
    {
        "artifact_root",
        "candidate_ids",
        "cost_cap_per_pair_usd",
        "cost_cap_run_usd",
        "decoding_parameters",
        "digest",
        "eligibility_ledger_path",
        "epoch_path",
        "epoch_digest",
        "endpoint",
        "api_version",
        "credential_environment",
        "environment_ledger_path",
        "induced_policy",
        "manifest_path",
        "model",
        "pricing",
        "privacy_boundary",
        "provider",
        "interaction_receipts",
        "repeat_count",
        "schema_version",
        "seed",
        "seed_supported",
        "split",
        "unsupported_policy",
        "usage_mapping",
        "pricing_source",
        "provider_routing",
        "containment_mechanism",
    }
)


_INTERACTION_RECEIPT_FIELDS = frozenset({"candidate_id", "receipt_digest", "receipt_path"})
