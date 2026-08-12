"""Contemporary raw/codec paired replay over eligibility/environment-admitted private workloads."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol, cast

from tools.paired_replay.config import (
    Arm,
    InteractionReceiptBinding,
    PairedReplayConfig,
    PairedReplayConfigError,
    PairedRunProvenance,
    verify_execution_config,
)
from tools.paired_replay.eligibility import EligibilityLedgerError, verify_eligibility
from tools.paired_replay.environment_ledger import (
    EnvironmentLedgerError,
    EnvironmentRecord,
    verify_environment,
)
from tools.paired_replay.epoch import EpochError, record_redesign_access, verify_epoch_manifest
from tools.paired_replay.evidence import JsonValue, NativeEvidenceError
from tools.paired_replay.extractors import extract_native
from tools.paired_replay.interaction import InteractionRenderer, render_private_interaction
from tools.paired_replay.manifest import Candidate, is_sha256
from tools.paired_replay.pricing import BillableResponseUsage, cost_usage, normalize_usage


class PairedReplayError(RuntimeError):
    """Base class for paired replay admission and execution failures."""


def require_process_credential(credential_environment: str) -> str:
    """Return the sole approved process credential before private source access."""
    credential = os.environ.get(credential_environment)
    if not credential:
        raise PairedReplayError(
            f"required credential environment {credential_environment!r} is unset"
        )
    return credential


class PairedReplayAdmissionError(PairedReplayError):
    """Raised when eligibility/environment receipts cannot admit a declared paired workload."""


class PairedReplayCostCapError(PairedReplayError):
    """Raised after recording the billed arm that exceeds a frozen cost cap."""


TurnClassification = Literal["completed", "induced", "unsupported"]


@dataclass(frozen=True, slots=True)
class PairedWorkload:
    """One private native workload admitted by immutable M2 and M3 receipts."""

    candidate: Candidate
    user_prompts: tuple[str, ...]
    interaction_receipt: InteractionReceiptBinding
    environment: EnvironmentRecord
    manifest_digest: str
    eligibility_ledger_digest: str
    environment_ledger_digest: str

    def __post_init__(self) -> None:
        if not self.user_prompts or any(not prompt.strip() for prompt in self.user_prompts):
            raise PairedReplayAdmissionError("paired workload requires non-empty user prompts")
        if self.environment.candidate_id != self.candidate.candidate_id:
            raise PairedReplayAdmissionError(
                "environment receipt candidate does not match workload"
            )
        if self.environment.source_sha256 != self.candidate.source_sha256:
            raise PairedReplayAdmissionError(
                "environment receipt source hash does not match workload"
            )
        if self.environment.status != "valid" or self.environment.environment_digest is None:
            raise PairedReplayAdmissionError("paired workload requires a valid environment receipt")
        if self.interaction_receipt.candidate_id != self.candidate.candidate_id:
            raise PairedReplayAdmissionError(
                "interaction receipt candidate does not match workload"
            )
        for field_name, value in (
            ("manifest_digest", self.manifest_digest),
            ("eligibility_ledger_digest", self.eligibility_ledger_digest),
            ("environment_ledger_digest", self.environment_ledger_digest),
        ):
            if not is_sha256(value):
                raise PairedReplayAdmissionError(f"{field_name} must be 64 lowercase hex")

    @property
    def digest(self) -> str:
        """Return a non-content workload identity shared by both replay arms."""
        return _digest(
            {
                "candidate_id": self.candidate.candidate_id,
                "environment_digest": self.environment.environment_digest,
                "manifest_digest": self.manifest_digest,
                "source_sha256": self.candidate.source_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class PairedReplayRequest:
    """One arm invocation under the single frozen settings object."""

    run_id: str
    workload: PairedWorkload
    config: PairedReplayConfig
    interaction: InteractionRenderer
    arm: Arm
    repeat_index: int

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise PairedReplayError("run_id must not be empty")
        if self.arm not in {"raw", "codec"}:
            raise PairedReplayError(f"unknown replay arm {self.arm!r}")
        if not 0 <= self.repeat_index < self.config.repeat_count:
            raise PairedReplayError("repeat_index is outside configured repeat_count")
        if self.workload.candidate.candidate_id not in self.config.candidate_ids:
            raise PairedReplayError("request workload is not declared in configuration")
        if self.workload.candidate.split != self.config.split:
            raise PairedReplayError("request workload split does not match configuration")

        if self.interaction.receipt.digest != self.workload.interaction_receipt.receipt_digest:
            raise PairedReplayError("request interaction receipt does not match workload")

    @property
    def settings_digest(self) -> str:
        """Return the declared model/runtime identity shared with the paired arm."""
        return self.config.settings_digest

    @property
    def condition_digest(self) -> str:
        """Return the private arm condition identity without serializing its content."""
        return _digest(
            {
                "arm": self.arm,
                "config_digest": self.config.digest,
                "interaction_receipt_digest": self.interaction.receipt.digest,
            }
        )


@dataclass(frozen=True, slots=True)
class PairedResponseTurn:
    """One contemporary response turn, retained only in a private raw artifact."""

    response: JsonValue | None
    native_usage: Mapping[str, object]
    classification: TurnClassification
    unsupported_reason: str | None = None

    def __post_init__(self) -> None:
        try:
            json.dumps(self.response, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise PairedReplayError("response must be JSON-serializable") from error
        if self.classification not in {"completed", "induced", "unsupported"}:
            raise PairedReplayError(f"unknown turn classification {self.classification!r}")
        if self.classification == "unsupported":
            if self.unsupported_reason is None or not self.unsupported_reason.strip():
                raise PairedReplayError("unsupported turn requires a non-empty reason")
        elif self.unsupported_reason is not None:
            raise PairedReplayError("only unsupported turns may carry a reason")


@dataclass(frozen=True, slots=True)
class PairedReplayResponse:
    """The complete contemporaneous outcome of one arm request."""

    turns: tuple[PairedResponseTurn, ...]

    def __post_init__(self) -> None:
        if not self.turns:
            raise PairedReplayError("paired replay response must contain at least one turn")
        unsupported = [
            index for index, turn in enumerate(self.turns) if turn.classification == "unsupported"
        ]
        if len(unsupported) > 1:
            raise PairedReplayError("paired replay response may contain one unsupported turn")
        if unsupported and unsupported[0] != len(self.turns) - 1:
            raise PairedReplayError("unsupported turn must terminate the replay response")


class PairedReplayClient(Protocol):
    """Provider adapter receiving private workload content only at explicit run time."""

    def respond(self, request: PairedReplayRequest) -> PairedReplayResponse:
        """Regenerate one raw or codec arm without consulting historical assistant output."""
        ...


@dataclass(frozen=True, slots=True)
class PairedTurnAccounting:
    """Explicit outcome totals for one arm; no induced or unsupported turn is hidden."""

    completed_turn_count: int
    induced_turn_count: int
    unsupported_turn_count: int

    def __post_init__(self) -> None:
        if any(
            count < 0
            for count in (
                self.completed_turn_count,
                self.induced_turn_count,
                self.unsupported_turn_count,
            )
        ):
            raise PairedReplayError("turn accounting counts must be non-negative")
        if self.total_turn_count == 0:
            raise PairedReplayError("turn accounting must account for at least one turn")

    @property
    def total_turn_count(self) -> int:
        """Return the exhaustive count of observed turn outcomes."""
        return self.completed_turn_count + self.induced_turn_count + self.unsupported_turn_count

    def to_payload(self) -> dict[str, int]:
        """Return canonical non-content turn accounting."""
        return {
            "completed_turn_count": self.completed_turn_count,
            "induced_turn_count": self.induced_turn_count,
            "unsupported_turn_count": self.unsupported_turn_count,
        }


@dataclass(frozen=True, slots=True)
class PairedArmReceipt:
    """Non-content accounting and provenance for one stored private arm result."""

    provenance: PairedRunProvenance
    artifact_path: Path
    usage: tuple[BillableResponseUsage, ...]
    cost_usd: Decimal
    turn_accounting: PairedTurnAccounting

    def __post_init__(self) -> None:
        if self.cost_usd < 0:
            raise PairedReplayError("arm cost must be non-negative")
        if len(self.usage) != self.turn_accounting.total_turn_count:
            raise PairedReplayError("arm usage must account for every response turn")

    def to_payload(self) -> dict[str, object]:
        """Return a private receipt payload without response content."""
        return {
            "artifact_path": str(self.artifact_path),
            "cost_usd": str(self.cost_usd),
            "provenance": self.provenance.to_payload(),
            "turn_accounting": self.turn_accounting.to_payload(),
            "usage": [
                {
                    "cache_read_tokens": item.cache_read_tokens,
                    "cache_write_tokens": item.cache_write_tokens,
                    "input_tokens": item.input_tokens,
                    "output_tokens": item.output_tokens,
                }
                for item in self.usage
            ],
        }


@dataclass(frozen=True, slots=True)
class PairedTerminatedPairReceipt:
    """One explicit pair-scoped unsupported outcome retained for causal reporting."""

    raw: PairedArmReceipt
    codec: PairedArmReceipt | None

    def __post_init__(self) -> None:
        receipts = (self.raw,) if self.codec is None else (self.raw, self.codec)
        if not any(receipt.turn_accounting.unsupported_turn_count for receipt in receipts):
            raise PairedReplayError("terminated pair must contain an unsupported arm")
        if self.codec is not None and self.codec.provenance.arm != "codec":
            raise PairedReplayError("terminated pair codec receipt must be codec arm")

    def to_payload(self) -> dict[str, object]:
        """Return a private terminated-pair receipt without response content."""
        return {
            "codec": None if self.codec is None else self.codec.to_payload(),
            "raw": self.raw.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class PairedPairReceipt:
    """The raw and codec outcomes for one exact workload repetition."""

    raw: PairedArmReceipt
    codec: PairedArmReceipt

    def __post_init__(self) -> None:
        raw = self.raw.provenance
        codec = self.codec.provenance
        shared = (
            "config_digest",
            "manifest_digest",
            "eligibility_ledger_digest",
            "environment_ledger_digest",
            "candidate_id",
            "source_sha256",
            "environment_digest",
            "run_id",
            "repeat_index",
        )
        if any(getattr(raw, field) != getattr(codec, field) for field in shared):
            raise PairedReplayError("raw and codec receipts do not share one declared workload")
        if any(arm.turn_accounting.unsupported_turn_count for arm in (self.raw, self.codec)):
            raise PairedReplayError("completed pair cannot contain unsupported turns")
        if raw.arm != "raw" or codec.arm != "codec":
            raise PairedReplayError("paired receipt must contain one raw and one codec arm")

    @property
    def cost_usd(self) -> Decimal:
        """Return both arm costs for enforcing the per-pair cap."""
        return self.raw.cost_usd + self.codec.cost_usd

    def to_payload(self) -> dict[str, object]:
        """Return a private paired receipt without response content."""
        return {"codec": self.codec.to_payload(), "raw": self.raw.to_payload()}


@dataclass(frozen=True, slots=True)
class PairedRunReceipt:
    """Complete non-content receipt for a cost-bounded paired runner invocation."""

    epoch_digest: str
    config_digest: str
    pairs: tuple[PairedPairReceipt, ...]
    total_cost_usd: Decimal
    terminated_pairs: tuple[PairedTerminatedPairReceipt, ...] = ()

    def __post_init__(self) -> None:
        for field_name, value in (
            ("epoch_digest", self.epoch_digest),
            ("config_digest", self.config_digest),
        ):
            if not is_sha256(value):
                raise PairedReplayError(f"{field_name} must be 64 lowercase hex")
        if self.total_cost_usd < 0:
            raise PairedReplayError("total_cost_usd must be non-negative")

    def payload_without_digest(self) -> dict[str, object]:
        """Return the exhaustive non-content receipt payload."""
        return {
            "config_digest": self.config_digest,
            "epoch_digest": self.epoch_digest,
            "pairs": [pair.to_payload() for pair in self.pairs],
            "schema_version": 1,
            "terminated_pairs": [pair.to_payload() for pair in self.terminated_pairs],
            "total_cost_usd": str(self.total_cost_usd),
        }

    @property
    def digest(self) -> str:
        """Return the receipt digest binding every response-artifact digest."""
        return _digest(self.payload_without_digest())

    def to_document(self) -> dict[str, object]:
        """Return the persistent private receipt document."""
        return {"digest": self.digest, **self.payload_without_digest()}


def admit_paired_workloads(config: PairedReplayConfig) -> tuple[PairedWorkload, ...]:
    """Revalidate redesign-only eligibility/environment evidence before any contemporary call."""
    if config.split != "redesign":
        raise PairedReplayAdmissionError(
            "paired replay requires the redesign split before release approval"
        )
    try:
        verify_execution_config(config)
        bindings = {binding.candidate_id: binding for binding in config.interaction_receipts}
        epoch, manifest = verify_epoch_manifest(config.epoch_path, config.manifest_path)
        if config.epoch_digest != epoch.digest:
            raise PairedReplayAdmissionError(
                "paired replay config epoch_digest does not match sealed epoch"
            )
        eligibility = verify_eligibility(
            config.epoch_path, config.manifest_path, config.eligibility_ledger_path
        )
        environments = verify_environment(
            config.epoch_path,
            config.manifest_path,
            config.eligibility_ledger_path,
            config.environment_ledger_path,
        )
    except (
        EligibilityLedgerError,
        EnvironmentLedgerError,
        EpochError,
        PairedReplayConfigError,
    ) as error:
        raise PairedReplayAdmissionError(str(error)) from error
    candidates = {candidate.candidate_id: candidate for candidate in manifest.candidates}
    eligibility_records = {record.candidate_id: record for record in eligibility.records}
    environment_records = {record.candidate_id: record for record in environments.records}
    workloads: list[PairedWorkload] = []
    for candidate_id in config.candidate_ids:
        try:
            candidate = candidates[candidate_id]
            eligibility_record = eligibility_records[candidate_id]
            environment = environment_records[candidate_id]
        except KeyError as error:
            raise PairedReplayAdmissionError(
                f"configured candidate {candidate_id!r} is absent from a verified receipt"
            ) from error
        if candidate.split != config.split:
            raise PairedReplayAdmissionError(
                f"candidate {candidate_id!r} does not belong to configured {config.split!r} split"
            )
        if eligibility_record.disposition != "confirmatory":
            raise PairedReplayAdmissionError(
                f"candidate {candidate_id!r} is not confirmatory in the eligibility ledger"
            )
        if environment.status != "valid" or environment.environment_digest is None:
            raise PairedReplayAdmissionError(
                f"candidate {candidate_id!r} lacks a valid environment receipt"
            )
        try:
            record_redesign_access(
                config.epoch_path,
                config.manifest_path,
                candidate.candidate_id,
                "paired_admit",
                timestamp=_audit_timestamp(),
            )
        except EpochError as error:
            raise PairedReplayAdmissionError(
                f"candidate {candidate_id!r} audit authorization failed: {error}"
            ) from error
        binding = bindings.get(candidate_id)
        if binding is None:
            raise PairedReplayAdmissionError(
                f"configured candidate {candidate_id!r} lacks an interaction receipt binding"
            )
        try:
            session = extract_native(candidate)
        except (NativeEvidenceError, OSError) as error:
            raise PairedReplayAdmissionError(
                f"candidate {candidate_id!r} native workload cannot be re-extracted"
            ) from error
        user_prompts = tuple(
            event.text
            for event in session.events
            if event.kind == "user_prompt" and event.text is not None
        )
        workloads.append(
            PairedWorkload(
                candidate,
                user_prompts,
                binding,
                environment,
                manifest.digest,
                eligibility.digest,
                environments.digest,
            )
        )
    return tuple(workloads)


def run_paired_replay(
    config: PairedReplayConfig,
    client: PairedReplayClient,
    *,
    run_id: str,
) -> PairedRunReceipt:
    """Regenerate raw then codec arms after one eligibility/environment source admission."""
    prepare_private_artifact_root(config)
    workloads = admit_paired_workloads(config)
    _safe_path_component(run_id)
    for workload in workloads:
        candidate_id = _safe_path_component(workload.candidate.candidate_id)
        _ensure_private_directory(
            config.artifact_root / run_id / candidate_id,
            root=config.artifact_root,
        )
    total_cost = Decimal(0)
    pairs: list[PairedPairReceipt] = []
    terminated_pairs: list[PairedTerminatedPairReceipt] = []
    for workload in workloads:
        for repeat_index in range(config.repeat_count):
            raw = _run_arm(config, workload, client, run_id, "raw", repeat_index)
            if raw.cost_usd > Decimal(config.cost_cap_per_pair_usd):
                raise PairedReplayCostCapError(
                    f"raw arm for {workload.candidate.candidate_id!r} spent ${raw.cost_usd}, "
                    f"past the ${config.cost_cap_per_pair_usd} pair cap"
                )
            if total_cost + raw.cost_usd > Decimal(config.cost_cap_run_usd):
                raise PairedReplayCostCapError(
                    f"run {run_id!r} spent ${total_cost + raw.cost_usd}, "
                    f"past the ${config.cost_cap_run_usd} run cap"
                )
            if raw.turn_accounting.unsupported_turn_count:
                if config.unsupported_policy != "terminate_pair":
                    raise PairedReplayError("unsupported policy does not terminate pairs")
                terminated_pairs.append(PairedTerminatedPairReceipt(raw, None))
                total_cost += raw.cost_usd
                continue
            codec = _run_arm(config, workload, client, run_id, "codec", repeat_index)
            pair_cost = raw.cost_usd + codec.cost_usd
            if pair_cost > Decimal(config.cost_cap_per_pair_usd):
                raise PairedReplayCostCapError(
                    f"pair for {workload.candidate.candidate_id!r} spent ${pair_cost}, "
                    f"past the ${config.cost_cap_per_pair_usd} pair cap"
                )
            if total_cost + pair_cost > Decimal(config.cost_cap_run_usd):
                raise PairedReplayCostCapError(
                    f"run {run_id!r} spent ${total_cost + pair_cost}, "
                    f"past the ${config.cost_cap_run_usd} run cap"
                )
            if codec.turn_accounting.unsupported_turn_count:
                if config.unsupported_policy != "terminate_pair":
                    raise PairedReplayError("unsupported policy does not terminate pairs")
                terminated_pairs.append(PairedTerminatedPairReceipt(raw, codec))
                total_cost += pair_cost
                continue
            pair = PairedPairReceipt(raw, codec)
            pairs.append(pair)
            total_cost += pair_cost
    return PairedRunReceipt(
        config.epoch_digest,
        config.digest,
        tuple(pairs),
        total_cost,
        tuple(terminated_pairs),
    )


def _run_arm(
    config: PairedReplayConfig,
    workload: PairedWorkload,
    client: PairedReplayClient,
    run_id: str,
    arm: Arm,
    repeat_index: int,
) -> PairedArmReceipt:
    interaction = render_private_interaction(
        Path(workload.interaction_receipt.receipt_path),
        config.epoch_path,
        config.manifest_path,
        config.eligibility_ledger_path,
        config.environment_ledger_path,
    )
    request = PairedReplayRequest(run_id, workload, config, interaction, arm, repeat_index)
    response = client.respond(request)
    artifact_path = _artifact_path(
        config.artifact_root, run_id, workload.candidate.candidate_id, arm, repeat_index
    )
    normalized_usage: list[BillableResponseUsage] = []
    try:
        for turn in response.turns:
            normalized_usage.append(normalize_usage(turn.native_usage, config.usage_mapping))
    except PairedReplayConfigError as error:
        partial_usage = tuple(
            [
                *normalized_usage,
                *(None for _ in response.turns[len(normalized_usage) :]),
            ]
        )
        _write_response_artifact(
            artifact_path,
            request,
            response,
            partial_usage,
            accounting_error=str(error),
        )
        message = (
            "provider response usage cannot be priced; "
            f"retained private response artifact {artifact_path}"
        )
        raise PairedReplayError(message) from error
    billable_usage = tuple(normalized_usage)
    cost = sum(
        (cost_usage(turn_usage, config.pricing) for turn_usage in billable_usage),
        Decimal(0),
    )
    artifact_sha256 = _write_response_artifact(artifact_path, request, response, billable_usage)
    if arm == "raw" and any(turn.classification == "induced" for turn in response.turns):
        raise PairedReplayError("raw arm must not contain induced turns")
    environment_digest = workload.environment.environment_digest
    if environment_digest is None:
        raise PairedReplayAdmissionError("admitted workload lacks environment digest")
    provenance = PairedRunProvenance(
        config.digest,
        workload.manifest_digest,
        workload.eligibility_ledger_digest,
        workload.environment_ledger_digest,
        interaction.receipt.digest,
        request.condition_digest,
        workload.candidate.candidate_id,
        workload.candidate.source_sha256,
        environment_digest,
        run_id,
        arm,
        repeat_index,
        artifact_sha256,
    )
    return PairedArmReceipt(
        provenance,
        artifact_path,
        billable_usage,
        cost,
        _account_turns(response),
    )


def _artifact_path(root: Path, run_id: str, candidate_id: str, arm: Arm, repeat_index: int) -> Path:
    return (
        root
        / _safe_path_component(run_id)
        / _safe_path_component(candidate_id)
        / (f"{repeat_index:04d}-{arm}.json")
    )


def _write_response_artifact(
    path: Path,
    request: PairedReplayRequest,
    response: PairedReplayResponse,
    usage: tuple[BillableResponseUsage | None, ...] | None,
    accounting_error: str | None = None,
) -> str:
    _ensure_private_directory(path.parent, root=request.config.artifact_root)
    document: dict[str, JsonValue] = {
        "arm": request.arm,
        "candidate_id": request.workload.candidate.candidate_id,
        "condition_digest": request.condition_digest,
        "config_digest": request.config.digest,
        "repeat_index": request.repeat_index,
        "run_id": request.run_id,
        "settings_digest": request.settings_digest,
        "interaction_receipt_digest": request.interaction.receipt.digest,
        "turns": cast(JsonValue, _artifact_turns(response, usage)),
        "workload_digest": request.workload.digest,
    }
    if accounting_error is not None:
        document["usage_accounting_error"] = accounting_error
    try:
        encoded = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PairedReplayError("response artifact cannot be canonical JSON") from error
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(encoded)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as error:
        raise PairedReplayError(
            f"cannot write private response artifact {path}: {error}"
        ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return hashlib.sha256(encoded).hexdigest()


def _artifact_turns(
    response: PairedReplayResponse,
    usage: tuple[BillableResponseUsage | None, ...] | None,
) -> list[dict[str, JsonValue]]:
    if usage is None:
        usage = tuple(None for _ in response.turns)
    if len(usage) != len(response.turns):
        raise PairedReplayError("response artifact usage length does not match response turns")
    return [
        {
            "classification": turn.classification,
            "response": turn.response,
            "unsupported_reason": turn.unsupported_reason,
            "usage": (
                None
                if normalized is None
                else {
                    "cache_read_tokens": normalized.cache_read_tokens,
                    "cache_write_tokens": normalized.cache_write_tokens,
                    "input_tokens": normalized.input_tokens,
                    "output_tokens": normalized.output_tokens,
                }
            ),
        }
        for turn, normalized in zip(response.turns, usage, strict=True)
    ]


def _account_turns(response: PairedReplayResponse) -> PairedTurnAccounting:
    return PairedTurnAccounting(
        completed_turn_count=sum(turn.classification == "completed" for turn in response.turns),
        induced_turn_count=sum(turn.classification == "induced" for turn in response.turns),
        unsupported_turn_count=sum(turn.classification == "unsupported" for turn in response.turns),
    )


def _audit_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _safe_path_component(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise PairedReplayError("artifact path components must be non-empty simple names")
    return value


def prepare_private_artifact_root(config: PairedReplayConfig) -> None:
    """Validate the sealed root before source access or provider billing."""
    epoch, _ = verify_epoch_manifest(config.epoch_path, config.manifest_path)
    root = config.artifact_root
    if not any(
        root == approved_root or root.is_relative_to(approved_root)
        for approved_root in epoch.approved_roots
    ):
        raise PairedReplayError("artifact root must be within sealed approved private roots")
    _ensure_private_directory(root)


def _ensure_private_directory(path: Path, *, root: Path | None = None) -> None:
    if root is not None:
        _ensure_private_directory(root)
        try:
            relative = path.relative_to(root)
        except ValueError as error:
            raise PairedReplayError(
                "artifact directory escapes configured artifact root"
            ) from error
        current = root
        for component in relative.parts:
            current /= component
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError as error:
                raise PairedReplayError(
                    f"cannot create artifact directory {current}: {error}"
                ) from error
            _validate_private_directory(current)
        return

    missing: list[Path] = []
    ancestor = path
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise PairedReplayError(
                f"cannot create artifact directory {directory}: {error}"
            ) from error
    _validate_private_directory(path)


def _validate_private_directory(path: Path) -> None:
    try:
        entry_stat = path.lstat()
    except OSError as error:
        raise PairedReplayError(f"cannot stat artifact directory {path}: {error}") from error
    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
        raise PairedReplayError("artifact directory must be a non-symlink directory")
    if entry_stat.st_uid != os.getuid():
        raise PairedReplayError("artifact directory must be owned by the current user")
    mode = entry_stat.st_mode & 0o777
    if mode != 0o700:
        raise PairedReplayError(f"artifact directory must have mode 0700, found {mode:04o}")


def _digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
