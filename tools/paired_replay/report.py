"""Persistent, non-content M4 paired receipts and paired-replay stratum reports."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from tools.paired_replay.config import PairedReplayConfig
from tools.paired_replay.epoch import (
    EpochError,
    SealedEpoch,
    read_access_audit,
    verify_epoch_manifest,
)
from tools.paired_replay.manifest import Manifest, is_sha256, stratum_for
from tools.paired_replay.runner import PairedArmReceipt, PairedRunReceipt

REPORT_SCHEMA_VERSION = 1


class PairedReportError(ValueError):
    """Raised when a private paired receipt report is invalid or cannot be verified."""


@dataclass(frozen=True, slots=True)
class StratumPairedReport:
    """Non-content raw, codec, induced, and unsupported totals for one stratum."""

    stratum: str
    completed_raw_turn_count: int
    completed_codec_turn_count: int
    induced_codec_turn_count: int
    unsupported_raw_turn_count: int
    unsupported_codec_turn_count: int
    raw_cost_usd: Decimal
    codec_cost_usd: Decimal
    completed_pair_count: int
    terminated_pair_count: int
    response_artifact_sha256s: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.stratum:
            raise PairedReportError("stratum must not be empty")
        if any(
            value < 0
            for value in (
                self.completed_raw_turn_count,
                self.completed_codec_turn_count,
                self.induced_codec_turn_count,
                self.unsupported_raw_turn_count,
                self.unsupported_codec_turn_count,
                self.completed_pair_count,
                self.terminated_pair_count,
            )
        ):
            raise PairedReportError("stratum report counts must be non-negative")
        if self.raw_cost_usd < 0 or self.codec_cost_usd < 0:
            raise PairedReportError("stratum report costs must be non-negative")
        if not self.response_artifact_sha256s or any(
            not is_sha256(digest) for digest in self.response_artifact_sha256s
        ):
            raise PairedReportError("stratum report must bind response artifact digests")

    def to_document(self) -> dict[str, object]:
        """Return canonical non-content totals for private diagnostics."""
        return {
            "codec_cost_usd": str(self.codec_cost_usd),
            "completed_codec_turn_count": self.completed_codec_turn_count,
            "completed_pair_count": self.completed_pair_count,
            "completed_raw_turn_count": self.completed_raw_turn_count,
            "induced_codec_turn_count": self.induced_codec_turn_count,
            "raw_cost_usd": str(self.raw_cost_usd),
            "response_artifact_sha256s": list(self.response_artifact_sha256s),
            "stratum": self.stratum,
            "terminated_pair_count": self.terminated_pair_count,
            "unsupported_codec_turn_count": self.unsupported_codec_turn_count,
            "unsupported_raw_turn_count": self.unsupported_raw_turn_count,
        }


@dataclass(frozen=True, slots=True)
class PersistentPairedReport:
    """Private M5 input binding a persistent paired receipt to the sealed epoch."""

    epoch_digest: str
    manifest_digest: str
    split_digest: str
    eligibility_ledger_digest: str
    environment_ledger_digest: str
    config_digest: str
    audit_head_digest: str
    receipt: dict[str, object]
    strata: tuple[StratumPairedReport, ...]

    def __post_init__(self) -> None:
        for field_name, value in (
            ("epoch_digest", self.epoch_digest),
            ("manifest_digest", self.manifest_digest),
            ("split_digest", self.split_digest),
            ("eligibility_ledger_digest", self.eligibility_ledger_digest),
            ("environment_ledger_digest", self.environment_ledger_digest),
            ("config_digest", self.config_digest),
            ("audit_head_digest", self.audit_head_digest),
        ):
            if not is_sha256(value):
                raise PairedReportError(f"{field_name} must be 64 lowercase hex")
        if set(self.receipt) != {
            "config_digest",
            "digest",
            "epoch_digest",
            "pairs",
            "schema_version",
            "terminated_pairs",
            "total_cost_usd",
        }:
            raise PairedReportError("paired receipt has invalid fields")
        if not self.strata:
            raise PairedReportError("paired report requires at least one stratum")
        if tuple(sorted(report.stratum for report in self.strata)) != tuple(
            report.stratum for report in self.strata
        ):
            raise PairedReportError("stratum reports must be sorted")

    @property
    def receipt_digest(self) -> str:
        """Return the persistent paired receipt identity bound by this report."""
        return cast(str, self.receipt["digest"])

    def payload_without_digest(self) -> dict[str, object]:
        """Return the report payload covered by its tamper-evident digest."""
        return {
            "audit_head_digest": self.audit_head_digest,
            "config_digest": self.config_digest,
            "eligibility_ledger_digest": self.eligibility_ledger_digest,
            "environment_ledger_digest": self.environment_ledger_digest,
            "epoch_digest": self.epoch_digest,
            "manifest_digest": self.manifest_digest,
            "receipt": self.receipt,
            "schema_version": REPORT_SCHEMA_VERSION,
            "split_digest": self.split_digest,
            "strata": [report.to_document() for report in self.strata],
        }

    @property
    def digest(self) -> str:
        """Return the canonical report identity."""
        return _digest(self.payload_without_digest())

    def to_document(self) -> dict[str, object]:
        """Return a persistent private report with its tamper-evident digest."""
        return {"digest": self.digest, **self.payload_without_digest()}


def build_paired_report(
    epoch_path: Path,
    manifest_path: Path,
    config: PairedReplayConfig,
    receipt: PairedRunReceipt,
) -> PersistentPairedReport:
    """Bind a live redesign-only paired receipt to its sealed epoch and audit head."""
    try:
        epoch, manifest = verify_epoch_manifest(epoch_path, manifest_path)
        audit = read_access_audit(epoch.audit_path)
    except EpochError as error:
        raise PairedReportError(str(error)) from error
    _verify_config_and_receipt(epoch, manifest, config, receipt)
    receipt_document = receipt.to_document()
    return PersistentPairedReport(
        epoch_digest=epoch.digest,
        manifest_digest=manifest.digest,
        split_digest=epoch.split_digest,
        eligibility_ledger_digest=_receipt_ledger_digest(receipt, "eligibility_ledger_digest"),
        environment_ledger_digest=_receipt_ledger_digest(receipt, "environment_ledger_digest"),
        config_digest=config.digest,
        audit_head_digest=audit.head_digest,
        receipt=receipt_document,
        strata=_stratum_reports(manifest, receipt),
    )


def write_paired_report(path: Path, epoch: SealedEpoch, report: PersistentPairedReport) -> None:
    """Atomically persist an paired-replay report under an approved private root as 0600."""
    _require_approved_private_path(path, epoch)
    _write_private_json(path, report.to_document())


def read_paired_report(path: Path) -> PersistentPairedReport:
    """Read and authenticate one persistent paired report without opening response bodies."""
    document = _read_private_json(path)
    expected_fields = {
        "audit_head_digest",
        "config_digest",
        "digest",
        "eligibility_ledger_digest",
        "environment_ledger_digest",
        "epoch_digest",
        "manifest_digest",
        "receipt",
        "schema_version",
        "split_digest",
        "strata",
    }
    if set(document) != expected_fields:
        raise PairedReportError("paired report has invalid fields")
    if document["schema_version"] != REPORT_SCHEMA_VERSION:
        raise PairedReportError("paired report schema_version is unsupported")
    digest = document["digest"]
    if not isinstance(digest, str) or not is_sha256(digest):
        raise PairedReportError("paired report digest must be 64 lowercase hex")
    raw_strata = document["strata"]
    if not isinstance(raw_strata, list):
        raise PairedReportError("paired report strata must be an array")
    report = PersistentPairedReport(
        epoch_digest=_required_digest(document, "epoch_digest"),
        manifest_digest=_required_digest(document, "manifest_digest"),
        split_digest=_required_digest(document, "split_digest"),
        eligibility_ledger_digest=_required_digest(document, "eligibility_ledger_digest"),
        environment_ledger_digest=_required_digest(document, "environment_ledger_digest"),
        config_digest=_required_digest(document, "config_digest"),
        audit_head_digest=_required_digest(document, "audit_head_digest"),
        receipt=_required_object(document, "receipt"),
        strata=tuple(_stratum_from_document(item) for item in raw_strata),
    )
    if not hmac.compare_digest(digest, report.digest):
        raise PairedReportError("paired report digest mismatch")
    return report


def verify_paired_report(
    path: Path,
    epoch_path: Path,
    manifest_path: Path,
    config: PairedReplayConfig,
) -> PersistentPairedReport:
    """Verify report bindings, current audit head, and every referenced response artifact."""
    report = read_paired_report(path)
    try:
        epoch, manifest = verify_epoch_manifest(epoch_path, manifest_path)
        audit = read_access_audit(epoch.audit_path)
    except EpochError as error:
        raise PairedReportError(str(error)) from error
    _require_approved_private_path(path, epoch)
    if (
        report.epoch_digest != epoch.digest
        or report.manifest_digest != manifest.digest
        or report.split_digest != epoch.split_digest
        or report.config_digest != config.digest
    ):
        raise PairedReportError("paired report no longer matches sealed epoch or config")
    if report.audit_head_digest != audit.head_digest and not any(
        record.digest == report.audit_head_digest for record in audit.records
    ):
        raise PairedReportError("paired report audit head is absent from the current audit chain")
    _verify_receipt_document(report.receipt, report, config, manifest)
    return report


def _verify_config_and_receipt(
    epoch: SealedEpoch,
    manifest: Manifest,
    config: PairedReplayConfig,
    receipt: PairedRunReceipt,
) -> None:
    if config.split != "redesign":
        raise PairedReportError("paired report requires the redesign split before release approval")
    if config.epoch_digest != epoch.digest or receipt.epoch_digest != epoch.digest:
        raise PairedReportError("paired receipt epoch_digest does not match sealed epoch")
    if receipt.config_digest != config.digest:
        raise PairedReportError("paired receipt config_digest does not match config")
    if not receipt.pairs and not receipt.terminated_pairs:
        raise PairedReportError("paired report requires at least one stored pair")
    candidates = {candidate.candidate_id: candidate for candidate in manifest.candidates}
    for arm in _receipt_arms(receipt):
        provenance = arm.provenance
        candidate = candidates.get(provenance.candidate_id)
        if (
            candidate is None
            or candidate.split != "redesign"
            or provenance.config_digest != config.digest
            or provenance.manifest_digest != manifest.digest
            or provenance.candidate_id not in config.candidate_ids
            or provenance.source_sha256 != candidate.source_sha256
        ):
            raise PairedReportError("paired arm provenance does not match report inputs")


def _receipt_ledger_digest(receipt: PairedRunReceipt, field_name: str) -> str:
    values = {cast(str, getattr(arm.provenance, field_name)) for arm in _receipt_arms(receipt)}
    if len(values) != 1:
        raise PairedReportError(f"paired receipt has inconsistent {field_name}")
    return values.pop()


def _receipt_arms(receipt: PairedRunReceipt) -> tuple[PairedArmReceipt, ...]:
    arms = [arm for pair in receipt.pairs for arm in (pair.raw, pair.codec)]
    for pair in receipt.terminated_pairs:
        arms.append(pair.raw)
        if pair.codec is not None:
            arms.append(pair.codec)
    return tuple(arms)


def _stratum_reports(
    manifest: Manifest, receipt: PairedRunReceipt
) -> tuple[StratumPairedReport, ...]:
    candidates = {candidate.candidate_id: candidate for candidate in manifest.candidates}
    arm_groups: dict[str, list[PairedArmReceipt]] = defaultdict(list)
    complete_pairs: defaultdict[str, int] = defaultdict(int)
    terminated_pairs: defaultdict[str, int] = defaultdict(int)
    for pair in receipt.pairs:
        stratum = stratum_for(candidates[pair.raw.provenance.candidate_id])
        arm_groups[stratum].extend((pair.raw, pair.codec))
        complete_pairs[stratum] += 1
    for terminated_pair in receipt.terminated_pairs:
        stratum = stratum_for(candidates[terminated_pair.raw.provenance.candidate_id])
        arm_groups[stratum].append(terminated_pair.raw)
        if terminated_pair.codec is not None:
            arm_groups[stratum].append(terminated_pair.codec)
        terminated_pairs[stratum] += 1
    reports: list[StratumPairedReport] = []
    for stratum, arms in arm_groups.items():
        raw = [arm for arm in arms if arm.provenance.arm == "raw"]
        codec = [arm for arm in arms if arm.provenance.arm == "codec"]
        reports.append(
            StratumPairedReport(
                stratum=stratum,
                completed_raw_turn_count=sum(
                    arm.turn_accounting.completed_turn_count for arm in raw
                ),
                completed_codec_turn_count=sum(
                    arm.turn_accounting.completed_turn_count for arm in codec
                ),
                induced_codec_turn_count=sum(
                    arm.turn_accounting.induced_turn_count for arm in codec
                ),
                unsupported_raw_turn_count=sum(
                    arm.turn_accounting.unsupported_turn_count for arm in raw
                ),
                unsupported_codec_turn_count=sum(
                    arm.turn_accounting.unsupported_turn_count for arm in codec
                ),
                raw_cost_usd=sum((arm.cost_usd for arm in raw), Decimal()),
                codec_cost_usd=sum((arm.cost_usd for arm in codec), Decimal()),
                completed_pair_count=complete_pairs[stratum],
                terminated_pair_count=terminated_pairs[stratum],
                response_artifact_sha256s=tuple(
                    sorted(arm.provenance.response_artifact_sha256 for arm in arms)
                ),
            )
        )
    return tuple(sorted(reports, key=lambda report: report.stratum))


def _document_stratum_reports(
    manifest: Manifest, receipt: dict[str, object]
) -> tuple[StratumPairedReport, ...]:
    candidates = {candidate.candidate_id: candidate for candidate in manifest.candidates}
    arm_groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    complete_pairs: defaultdict[str, int] = defaultdict(int)
    terminated_pairs: defaultdict[str, int] = defaultdict(int)
    for field_name, completed in (("pairs", True), ("terminated_pairs", False)):
        raw_pairs = receipt.get(field_name)
        if not isinstance(raw_pairs, list):
            raise PairedReportError(f"paired receipt {field_name} must be an array")
        for raw_pair in raw_pairs:
            pair = _object(raw_pair, "paired receipt pair")
            raw = _required_object(pair, "raw")
            raw_provenance = _required_object(raw, "provenance")
            candidate_id = _required_text(raw_provenance, "candidate_id")
            candidate = candidates.get(candidate_id)
            if candidate is None:
                raise PairedReportError("paired receipt candidate is absent from manifest")
            stratum = stratum_for(candidate)
            arm_groups[stratum].append(raw)
            codec = pair.get("codec")
            if codec is not None:
                arm_groups[stratum].append(_object(codec, "paired receipt codec"))
            if completed:
                if codec is None:
                    raise PairedReportError("complete paired receipt lacks codec arm")
                complete_pairs[stratum] += 1
            else:
                terminated_pairs[stratum] += 1
    reports: list[StratumPairedReport] = []
    for stratum, arms in arm_groups.items():
        raw_arms = [
            arm
            for arm in arms
            if _required_text(_required_object(arm, "provenance"), "arm") == "raw"
        ]
        codec_arms = [
            arm
            for arm in arms
            if _required_text(_required_object(arm, "provenance"), "arm") == "codec"
        ]
        reports.append(
            StratumPairedReport(
                stratum=stratum,
                completed_raw_turn_count=sum(
                    _document_turn_count(arm, "completed_turn_count") for arm in raw_arms
                ),
                completed_codec_turn_count=sum(
                    _document_turn_count(arm, "completed_turn_count") for arm in codec_arms
                ),
                induced_codec_turn_count=sum(
                    _document_turn_count(arm, "induced_turn_count") for arm in codec_arms
                ),
                unsupported_raw_turn_count=sum(
                    _document_turn_count(arm, "unsupported_turn_count") for arm in raw_arms
                ),
                unsupported_codec_turn_count=sum(
                    _document_turn_count(arm, "unsupported_turn_count") for arm in codec_arms
                ),
                raw_cost_usd=sum(
                    (_required_decimal(arm, "cost_usd") for arm in raw_arms), Decimal()
                ),
                codec_cost_usd=sum(
                    (_required_decimal(arm, "cost_usd") for arm in codec_arms), Decimal()
                ),
                completed_pair_count=complete_pairs[stratum],
                terminated_pair_count=terminated_pairs[stratum],
                response_artifact_sha256s=tuple(
                    sorted(
                        _required_digest(
                            _required_object(arm, "provenance"),
                            "response_artifact_sha256",
                        )
                        for arm in arms
                    )
                ),
            )
        )
    return tuple(sorted(reports, key=lambda report: report.stratum))


def _document_turn_count(arm: dict[str, object], field_name: str) -> int:
    accounting = _required_object(arm, "turn_accounting")
    return _required_count(accounting, field_name)


def _verify_receipt_document(
    receipt: dict[str, object],
    report: PersistentPairedReport,
    config: PairedReplayConfig,
    manifest: Manifest,
) -> None:
    receipt_digest = receipt.get("digest")
    if not isinstance(receipt_digest, str) or not is_sha256(receipt_digest):
        raise PairedReportError("paired receipt digest must be 64 lowercase hex")
    if receipt.get("schema_version") != 1:
        raise PairedReportError("paired receipt schema_version is unsupported")
    if receipt.get("epoch_digest") != report.epoch_digest:
        raise PairedReportError("paired receipt epoch_digest does not match report")
    if receipt.get("config_digest") != config.digest:
        raise PairedReportError("paired receipt config_digest does not match config")
    if receipt_digest != _digest({key: value for key, value in receipt.items() if key != "digest"}):
        raise PairedReportError("paired receipt digest mismatch")
    arms = _receipt_document_arms(receipt)
    candidates = {candidate.candidate_id: candidate for candidate in manifest.candidates}
    eligibility_digests: set[str] = set()
    environment_digests: set[str] = set()
    for arm in arms:
        provenance = _required_object(arm, "provenance")
        candidate_id = _required_text(provenance, "candidate_id")
        candidate = candidates.get(candidate_id)
        if (
            candidate is None
            or candidate.split != "redesign"
            or candidate_id not in config.candidate_ids
            or _required_digest(provenance, "config_digest") != config.digest
            or _required_digest(provenance, "manifest_digest") != manifest.digest
            or _required_digest(provenance, "source_sha256") != candidate.source_sha256
        ):
            raise PairedReportError("paired arm provenance does not match report inputs")
        eligibility_digests.add(_required_digest(provenance, "eligibility_ledger_digest"))
        environment_digests.add(_required_digest(provenance, "environment_ledger_digest"))
        artifact_path = _required_text(arm, "artifact_path")
        artifact_digest = _required_digest(provenance, "response_artifact_sha256")
        _verify_response_artifact(Path(artifact_path), config.artifact_root, artifact_digest)
    if eligibility_digests != {report.eligibility_ledger_digest}:
        raise PairedReportError("paired receipt eligibility ledger digest does not match report")
    if environment_digests != {report.environment_ledger_digest}:
        raise PairedReportError("paired receipt environment ledger digest does not match report")
    expected_strata = _document_stratum_reports(manifest, receipt)
    if tuple(item.to_document() for item in report.strata) != tuple(
        item.to_document() for item in expected_strata
    ):
        raise PairedReportError("paired report strata do not match persistent receipt")
    total_cost = sum((item.raw_cost_usd + item.codec_cost_usd for item in report.strata), Decimal())
    if total_cost != _required_decimal(receipt, "total_cost_usd"):
        raise PairedReportError("paired report costs do not match persistent receipt")


def _receipt_document_arms(receipt: dict[str, object]) -> tuple[dict[str, object], ...]:
    arms: list[dict[str, object]] = []
    for field_name in ("pairs", "terminated_pairs"):
        raw_pairs = receipt.get(field_name)
        if not isinstance(raw_pairs, list):
            raise PairedReportError(f"paired receipt {field_name} must be an array")
        for pair in raw_pairs:
            pair_document = _object(pair, "paired receipt pair")
            if set(pair_document) != {"raw", "codec"}:
                raise PairedReportError("paired receipt pair has invalid fields")
            raw = _required_object(pair_document, "raw")
            _validate_receipt_arm(raw)
            if field_name == "pairs" and _document_turn_count(raw, "unsupported_turn_count"):
                raise PairedReportError("completed paired receipt contains unsupported turns")
            arms.append(raw)
            codec = pair_document["codec"]
            if codec is None:
                if field_name == "pairs":
                    raise PairedReportError("complete paired receipt lacks codec arm")
                continue
            codec_arm = _object(codec, "paired receipt codec")
            _validate_receipt_arm(codec_arm)
            if field_name == "pairs" and _document_turn_count(codec_arm, "unsupported_turn_count"):
                raise PairedReportError("completed paired receipt contains unsupported turns")
            arms.append(codec_arm)
    if not arms:
        raise PairedReportError("paired receipt has no arms")
    return tuple(arms)


def _validate_receipt_arm(arm: dict[str, object]) -> None:
    if set(arm) != {"artifact_path", "cost_usd", "provenance", "turn_accounting", "usage"}:
        raise PairedReportError("paired receipt arm has invalid fields")
    _required_text(arm, "artifact_path")
    _required_decimal(arm, "cost_usd")
    provenance = _required_object(arm, "provenance")
    if set(provenance) != {
        "arm",
        "candidate_id",
        "condition_digest",
        "config_digest",
        "eligibility_ledger_digest",
        "environment_digest",
        "environment_ledger_digest",
        "interaction_receipt_digest",
        "manifest_digest",
        "repeat_index",
        "response_artifact_sha256",
        "run_id",
        "source_sha256",
    }:
        raise PairedReportError("paired receipt provenance has invalid fields")
    if _required_text(provenance, "arm") not in {"raw", "codec"}:
        raise PairedReportError("paired receipt provenance arm is invalid")
    for field_name in (
        "condition_digest",
        "config_digest",
        "eligibility_ledger_digest",
        "environment_digest",
        "environment_ledger_digest",
        "interaction_receipt_digest",
        "manifest_digest",
        "response_artifact_sha256",
        "source_sha256",
    ):
        _required_digest(provenance, field_name)
    _required_text(provenance, "candidate_id")
    _required_text(provenance, "run_id")
    _required_count(provenance, "repeat_index")
    accounting = _required_object(arm, "turn_accounting")
    if set(accounting) != {
        "completed_turn_count",
        "induced_turn_count",
        "unsupported_turn_count",
    }:
        raise PairedReportError("paired receipt turn accounting has invalid fields")
    for field_name in accounting:
        _required_count(accounting, field_name)
    usage = arm["usage"]
    if not isinstance(usage, list):
        raise PairedReportError("paired receipt usage must be an array")
    for entry in usage:
        usage_entry = _object(entry, "paired receipt usage")
        if set(usage_entry) != {
            "cache_read_tokens",
            "cache_write_tokens",
            "input_tokens",
            "output_tokens",
        }:
            raise PairedReportError("paired receipt usage has invalid fields")
        for field_name in usage_entry:
            _required_count(usage_entry, field_name)


def _verify_response_artifact(path: Path, root: Path, expected_digest: str) -> None:
    try:
        resolved_path = path.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
        directory_mode = stat.S_IMODE(resolved_path.parent.stat().st_mode)
        mode = stat.S_IMODE(resolved_path.stat().st_mode)
    except OSError as error:
        raise PairedReportError(f"cannot verify response artifact {path}: {error}") from error
    except ValueError as error:
        raise PairedReportError("response artifact escapes configured artifact root") from error
    if directory_mode != 0o700:
        raise PairedReportError(
            f"response artifact directory must have mode 0700, found {directory_mode:04o}"
        )
    if mode != 0o600:
        raise PairedReportError(f"response artifact must have mode 0600, found {mode:04o}")
    try:
        actual_digest = hashlib.sha256(resolved_path.read_bytes()).hexdigest()
    except OSError as error:
        raise PairedReportError(f"cannot verify response artifact {path}: {error}") from error
    if not hmac.compare_digest(expected_digest, actual_digest):
        raise PairedReportError("response artifact digest mismatch")


def _stratum_from_document(raw: object) -> StratumPairedReport:
    document = _object(raw, "stratum report")
    expected_fields = {
        "codec_cost_usd",
        "completed_codec_turn_count",
        "completed_pair_count",
        "completed_raw_turn_count",
        "induced_codec_turn_count",
        "raw_cost_usd",
        "response_artifact_sha256s",
        "stratum",
        "terminated_pair_count",
        "unsupported_codec_turn_count",
        "unsupported_raw_turn_count",
    }
    if set(document) != expected_fields:
        raise PairedReportError("stratum report has invalid fields")
    artifacts = document["response_artifact_sha256s"]
    if not isinstance(artifacts, list) or not all(isinstance(value, str) for value in artifacts):
        raise PairedReportError("stratum report artifact digests must be an array of strings")
    return StratumPairedReport(
        stratum=_required_text(document, "stratum"),
        completed_raw_turn_count=_required_count(document, "completed_raw_turn_count"),
        completed_codec_turn_count=_required_count(document, "completed_codec_turn_count"),
        induced_codec_turn_count=_required_count(document, "induced_codec_turn_count"),
        unsupported_raw_turn_count=_required_count(document, "unsupported_raw_turn_count"),
        unsupported_codec_turn_count=_required_count(document, "unsupported_codec_turn_count"),
        raw_cost_usd=_required_decimal(document, "raw_cost_usd"),
        codec_cost_usd=_required_decimal(document, "codec_cost_usd"),
        completed_pair_count=_required_count(document, "completed_pair_count"),
        terminated_pair_count=_required_count(document, "terminated_pair_count"),
        response_artifact_sha256s=tuple(artifacts),
    )


def _required_digest(document: dict[str, object], field_name: str) -> str:
    value = _required_text(document, field_name)
    if not is_sha256(value):
        raise PairedReportError(f"{field_name} must be 64 lowercase hex")
    return value


def _required_text(document: dict[str, object], field_name: str) -> str:
    value = document.get(field_name)
    if not isinstance(value, str) or not value:
        raise PairedReportError(f"{field_name} must be a non-empty string")
    return value


def _required_count(document: dict[str, object], field_name: str) -> int:
    value = document.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PairedReportError(f"{field_name} must be a non-negative integer")
    return value


def _required_decimal(document: dict[str, object], field_name: str) -> Decimal:
    value = _required_text(document, field_name)
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise PairedReportError(f"{field_name} must be a non-negative finite decimal") from error
    if not parsed.is_finite() or parsed < 0:
        raise PairedReportError(f"{field_name} must be a non-negative finite decimal")
    return parsed


def _required_object(document: dict[str, object], field_name: str) -> dict[str, object]:
    return _object(document.get(field_name), field_name)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PairedReportError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _require_approved_private_path(path: Path, epoch: SealedEpoch) -> None:
    if not path.is_absolute():
        raise PairedReportError("paired report path must be absolute")
    resolved = path.resolve(strict=False)
    if not any(_is_within(resolved, root) for root in epoch.approved_roots):
        raise PairedReportError("paired report path must be within an approved private root")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _read_private_json(path: Path) -> dict[str, object]:
    try:
        parent_mode = stat.S_IMODE(path.parent.stat().st_mode)
        if parent_mode != 0o700:
            raise PairedReportError(
                f"paired report directory must have mode 0700, found {parent_mode:04o}"
            )
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise PairedReportError("paired report must have mode 0600")
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise PairedReportError(f"cannot read paired report {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise PairedReportError(f"paired report is not valid JSON: {error.msg}") from error
    return _object(document, "paired report")


def _write_private_json(path: Path, document: dict[str, object]) -> None:
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
            stream.write(
                json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                + "\n"
            )
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as error:
        raise PairedReportError(f"cannot write paired report {path}: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        raise PairedReportError(f"cannot stat paired report directory {path}: {error}") from error
    if mode != 0o700:
        raise PairedReportError(f"paired report directory must have mode 0700, found {mode:04o}")


def _digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
