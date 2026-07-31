"""Contemporary raw/codec paired replay over M2/M3-admitted private workloads."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from laconic.k1.eligibility import EligibilityLedgerError, verify_eligibility
from laconic.k1.environment_ledger import (
    EnvironmentLedgerError,
    EnvironmentRecord,
    verify_environment,
)
from laconic.k1.epoch import EpochError, record_redesign_access, verify_epoch_manifest
from laconic.k1.evidence import JsonValue, NativeEvidenceError
from laconic.k1.extractors import extract_native
from laconic.k1.manifest import Candidate, is_sha256
from laconic.k1.paired_config import Arm, PairedReplayConfig, PairedRunProvenance
from laconic.k1.pricing import BillableResponseUsage, cost_usage, normalize_usage


class PairedReplayError(RuntimeError):
    """Base class for K1 paired replay admission and execution failures."""


class PairedReplayAdmissionError(PairedReplayError):
    """Raised when M2/M3 receipts cannot admit a declared paired workload."""


class PairedReplayCostCapError(PairedReplayError):
    """Raised after recording the billed arm that exceeds a frozen cost cap."""


TurnClassification = Literal["completed", "induced", "unsupported"]


@dataclass(frozen=True, slots=True)
class PairedWorkload:
    """One private native workload admitted by immutable M2 and M3 receipts."""

    candidate: Candidate
    user_prompts: tuple[str, ...]
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

    @property
    def settings_digest(self) -> str:
        """Return the declared model/runtime identity shared with the paired arm."""
        return self.config.settings_digest


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
        if raw.arm != "raw" or codec.arm != "codec":
            raise PairedReplayError("paired receipt must contain one raw and one codec arm")

    @property
    def cost_usd(self) -> Decimal:
        """Return both arm costs for enforcing the per-pair cap."""
        return self.raw.cost_usd + self.codec.cost_usd


@dataclass(frozen=True, slots=True)
class PairedRunReceipt:
    """Complete non-content receipt for a cost-bounded paired runner invocation."""

    config_digest: str
    pairs: tuple[PairedPairReceipt, ...]
    total_cost_usd: Decimal
    terminated_pairs: tuple[PairedTerminatedPairReceipt, ...] = ()

    def __post_init__(self) -> None:
        if not is_sha256(self.config_digest):
            raise PairedReplayError("config_digest must be 64 lowercase hex")
        if self.total_cost_usd < 0:
            raise PairedReplayError("total_cost_usd must be non-negative")


def admit_paired_workloads(config: PairedReplayConfig) -> tuple[PairedWorkload, ...]:
    """Revalidate redesign-only M2/M3 evidence before any contemporary call."""
    if config.split != "redesign":
        raise PairedReplayAdmissionError("paired replay requires the redesign split before M6")
    try:
        epoch, manifest = verify_epoch_manifest(config.epoch_path, config.manifest_path)
        if config.epoch_digest != epoch.digest:
            raise PairedReplayAdmissionError(
                "paired replay config epoch_digest does not match sealed epoch"
            )
        eligibility = verify_eligibility(
            config.epoch_path, config.manifest_path, config.eligibility_ledger_path
        )
        environments = verify_environment(
            config.epoch_path, config.manifest_path, config.environment_ledger_path
        )
    except (EligibilityLedgerError, EnvironmentLedgerError, EpochError) as error:
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
                environment,
                manifest.digest,
                eligibility.digest,
                environments.digest,
            )
        )
    return tuple(workloads)


def run_paired_replay(
    config: PairedReplayConfig,
    workloads: Sequence[PairedWorkload],
    client: PairedReplayClient,
    *,
    run_id: str,
) -> PairedRunReceipt:
    """Regenerate raw then codec arms for every declared workload and repeat.

    The runner writes each billed response before checking the cap because a provider
    call cannot be unbilled. It then raises immediately, preventing any later arm or
    workload from executing under a breached frozen budget.
    """
    admitted_workloads = admit_paired_workloads(config)
    if tuple(workloads) != admitted_workloads:
        raise PairedReplayAdmissionError(
            "workloads must exactly match the configuration's revalidated M1/M2/M3 evidence"
        )
    workloads = admitted_workloads
    _ensure_private_directory(config.artifact_root)
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
            pair = PairedPairReceipt(raw, codec)
            if pair.cost_usd > Decimal(config.cost_cap_per_pair_usd):
                raise PairedReplayCostCapError(
                    f"pair for {workload.candidate.candidate_id!r} spent ${pair.cost_usd}, "
                    f"past the ${config.cost_cap_per_pair_usd} pair cap"
                )
            if total_cost + pair.cost_usd > Decimal(config.cost_cap_run_usd):
                raise PairedReplayCostCapError(
                    f"run {run_id!r} spent ${total_cost + pair.cost_usd}, "
                    f"past the ${config.cost_cap_run_usd} run cap"
                )
            if codec.turn_accounting.unsupported_turn_count:
                if config.unsupported_policy != "terminate_pair":
                    raise PairedReplayError("unsupported policy does not terminate pairs")
                terminated_pairs.append(PairedTerminatedPairReceipt(raw, codec))
                total_cost += pair.cost_usd
                continue
            pairs.append(pair)
            total_cost += pair.cost_usd
    return PairedRunReceipt(
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
    request = PairedReplayRequest(run_id, workload, config, arm, repeat_index)
    response = client.respond(request)
    if arm == "raw" and any(turn.classification == "induced" for turn in response.turns):
        raise PairedReplayError("raw arm must not contain induced turns")
    usage = tuple(
        normalize_usage(turn.native_usage, config.usage_mapping) for turn in response.turns
    )
    cost = sum((cost_usage(turn_usage, config.pricing) for turn_usage in usage), Decimal(0))
    artifact_path = _artifact_path(
        config.artifact_root, run_id, workload.candidate.candidate_id, arm, repeat_index
    )
    artifact_sha256 = _write_response_artifact(artifact_path, request, response, usage)
    environment_digest = workload.environment.environment_digest
    if environment_digest is None:
        raise PairedReplayAdmissionError("admitted workload lacks environment digest")
    provenance = PairedRunProvenance(
        config.digest,
        workload.manifest_digest,
        workload.eligibility_ledger_digest,
        workload.environment_ledger_digest,
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
        usage,
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
    usage: tuple[BillableResponseUsage, ...],
) -> str:
    _ensure_private_directory(path.parent, root=request.config.artifact_root)
    document: dict[str, JsonValue] = {
        "arm": request.arm,
        "candidate_id": request.workload.candidate.candidate_id,
        "config_digest": request.config.digest,
        "repeat_index": request.repeat_index,
        "run_id": request.run_id,
        "settings_digest": request.settings_digest,
        "turns": [
            {
                "classification": turn.classification,
                "response": turn.response,
                "unsupported_reason": turn.unsupported_reason,
                "usage": {
                    "cache_read_tokens": normalized.cache_read_tokens,
                    "cache_write_tokens": normalized.cache_write_tokens,
                    "input_tokens": normalized.input_tokens,
                    "output_tokens": normalized.output_tokens,
                },
            }
            for turn, normalized in zip(response.turns, usage, strict=True)
        ],
        "workload_digest": request.workload.digest,
    }
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
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
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
