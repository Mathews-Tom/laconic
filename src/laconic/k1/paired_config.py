"""Private, immutable configuration and provenance contracts for K1 paired replay."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast
from urllib.parse import urlsplit

from laconic.k1.evidence import JsonScalar
from laconic.k1.manifest import is_sha256

PAIRED_REPLAY_CONFIG_SCHEMA_VERSION = 1

Arm = Literal["raw", "codec"]
Split = Literal["redesign", "holdout"]


class PairedReplayConfigError(ValueError):
    """Raised when a K1 paired replay configuration is incomplete or unsafe."""


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
    cache_write_field: str
    output_field: str

    def __post_init__(self) -> None:
        fields = (
            self.input_field,
            self.cache_read_field,
            self.cache_write_field,
            self.output_field,
        )
        if any(not field.strip() for field in fields):
            raise PairedReplayConfigError("usage mapping fields must not be empty")
        if len(set(fields)) != len(fields):
            raise PairedReplayConfigError("usage mapping fields must be distinct")

    def to_payload(self) -> dict[str, str]:
        """Return the canonical native-to-billable field mapping."""
        return {
            "cache_read_field": self.cache_read_field,
            "cache_write_field": self.cache_write_field,
            "input_field": self.input_field,
            "output_field": self.output_field,
        }


@dataclass(frozen=True, slots=True)
class PairedReplayConfig:
    """Every declared setting shared by raw and codec replay arms.

    Secrets do not belong to this value. A provider adapter receives credentials only
    from its process environment when a run is explicitly authorized.
    """

    manifest_path: Path
    eligibility_ledger_path: Path
    environment_ledger_path: Path
    artifact_root: Path
    provider: str
    endpoint: str
    privacy_boundary: str
    model: str
    decoding_parameters: Mapping[str, JsonScalar]
    seed_supported: bool
    seed: str | None
    repeat_count: int
    split: Split
    candidate_ids: tuple[str, ...]
    pricing: PriceTable
    usage_mapping: UsageMapping
    cost_cap_per_pair_usd: str
    cost_cap_run_usd: str
    unsupported_policy: Literal["terminate_pair"]
    induced_policy: Literal["include_in_codec_cost"]

    def __post_init__(self) -> None:
        for field_name, value in (
            ("provider", self.provider),
            ("endpoint", self.endpoint),
            ("privacy_boundary", self.privacy_boundary),
            ("model", self.model),
        ):
            if not value.strip():
                raise PairedReplayConfigError(f"{field_name} must not be empty")
        _validate_endpoint(self.endpoint)
        for field_name, path in (
            ("manifest_path", self.manifest_path),
            ("eligibility_ledger_path", self.eligibility_ledger_path),
            ("environment_ledger_path", self.environment_ledger_path),
            ("artifact_root", self.artifact_root),
        ):
            if not path.is_absolute():
                raise PairedReplayConfigError(f"{field_name} must be absolute")
            object.__setattr__(self, field_name, path.resolve(strict=False))
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
        _positive_decimal(self.cost_cap_per_pair_usd, "cost_cap_per_pair_usd")
        _positive_decimal(self.cost_cap_run_usd, "cost_cap_run_usd")
        if Decimal(self.cost_cap_per_pair_usd) > Decimal(self.cost_cap_run_usd):
            raise PairedReplayConfigError("cost_cap_per_pair_usd must not exceed cost_cap_run_usd")
        if self.unsupported_policy != "terminate_pair":
            raise PairedReplayConfigError("unsupported_policy must terminate_pair")
        if self.induced_policy != "include_in_codec_cost":
            raise PairedReplayConfigError("induced_policy must include_in_codec_cost")

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
            "eligibility_ledger_path": str(self.eligibility_ledger_path),
            "endpoint": self.endpoint,
            "environment_ledger_path": str(self.environment_ledger_path),
            "induced_policy": self.induced_policy,
            "manifest_path": str(self.manifest_path),
            "model": self.model,
            "pricing": self.pricing.to_payload(),
            "privacy_boundary": self.privacy_boundary,
            "provider": self.provider,
            "repeat_count": self.repeat_count,
            "schema_version": PAIRED_REPLAY_CONFIG_SCHEMA_VERSION,
            "seed": self.seed,
            "seed_supported": self.seed_supported,
            "split": self.split,
            "unsupported_policy": self.unsupported_policy,
            "usage_mapping": self.usage_mapping.to_payload(),
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
            "environment_digest": self.environment_digest,
            "environment_ledger_digest": self.environment_ledger_digest,
            "manifest_digest": self.manifest_digest,
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


def _config_from_document(document: dict[str, object]) -> PairedReplayConfig:
    pricing = _mapping(document["pricing"], "pricing", _PRICE_FIELDS)
    usage_mapping = _mapping(document["usage_mapping"], "usage_mapping", _USAGE_MAPPING_FIELDS)
    candidate_ids = document["candidate_ids"]
    if not isinstance(candidate_ids, list) or any(
        not isinstance(candidate_id, str) for candidate_id in candidate_ids
    ):
        raise PairedReplayConfigError("candidate_ids must be an array of strings")
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
    return PairedReplayConfig(
        manifest_path=_absolute_path(document, "manifest_path"),
        eligibility_ledger_path=_absolute_path(document, "eligibility_ledger_path"),
        environment_ledger_path=_absolute_path(document, "environment_ledger_path"),
        artifact_root=_absolute_path(document, "artifact_root"),
        provider=_required_text(document, "provider"),
        endpoint=_required_text(document, "endpoint"),
        privacy_boundary=_required_text(document, "privacy_boundary"),
        model=_required_text(document, "model"),
        decoding_parameters=cast(dict[str, JsonScalar], decoding_parameters),
        seed_supported=seed_supported,
        seed=seed,
        repeat_count=repeat_count,
        split=split,
        candidate_ids=tuple(candidate_ids),
        pricing=PriceTable(
            _required_text(pricing, "effective_date"),
            _required_text(pricing, "input_per_mtok"),
            _required_text(pricing, "cache_read_per_mtok"),
            _required_text(pricing, "cache_write_per_mtok"),
            _required_text(pricing, "output_per_mtok"),
        ),
        usage_mapping=UsageMapping(
            _required_text(usage_mapping, "input_field"),
            _required_text(usage_mapping, "cache_read_field"),
            _required_text(usage_mapping, "cache_write_field"),
            _required_text(usage_mapping, "output_field"),
        ),
        cost_cap_per_pair_usd=_required_text(document, "cost_cap_per_pair_usd"),
        cost_cap_run_usd=_required_text(document, "cost_cap_run_usd"),
        unsupported_policy=unsupported_policy,
        induced_policy=induced_policy,
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
    {"input_field", "cache_read_field", "cache_write_field", "output_field"}
)


_DOCUMENT_FIELDS = frozenset(
    {
        "artifact_root",
        "candidate_ids",
        "cost_cap_per_pair_usd",
        "cost_cap_run_usd",
        "decoding_parameters",
        "digest",
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
)
