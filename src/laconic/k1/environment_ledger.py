"""Private, non-content admission receipts for K1 tool environments."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from laconic.k1.environment import EnvironmentError, SnapshotEnvironment, validate_snapshot
from laconic.k1.epoch import EpochError, record_redesign_access, verify_epoch_manifest
from laconic.k1.evidence import (
    JsonValue,
    NativeEvidenceError,
    ToolCall,
    validate_confirmatory_evidence,
)
from laconic.k1.extractors import extract_native
from laconic.k1.manifest import Candidate, Split, is_sha256

ENVIRONMENT_LEDGER_SCHEMA_VERSION = 3

EnvironmentStatus = Literal["valid", "unsupported", "unavailable"]
EnvironmentMode = Literal["recorded_tool", "snapshot"]
EnvironmentReason = Literal[
    "environment_unavailable",
    "recorded_tool_validated",
    "snapshot_validated",
    "unsupported_tool",
]


class EnvironmentLedgerError(ValueError):
    """Raised when a private K1 environment ledger is malformed or incomplete."""


@dataclass(frozen=True, slots=True)
class EnvironmentRecord:
    """One non-content environment admission decision for a manifest candidate."""

    candidate_id: str
    source_sha256: str
    status: EnvironmentStatus
    mode: EnvironmentMode | None
    environment_digest: str | None
    reason: EnvironmentReason
    snapshot_root: str | None = None

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise EnvironmentLedgerError("candidate_id must not be empty")
        if not is_sha256(self.source_sha256):
            raise EnvironmentLedgerError("source_sha256 must be 64 lowercase hex")
        if self.status not in {"valid", "unsupported", "unavailable"}:
            raise EnvironmentLedgerError(f"unknown environment status {self.status!r}")
        if self.status == "valid":
            if self.mode is None or self.environment_digest is None:
                raise EnvironmentLedgerError("valid environment record requires mode and digest")
            if not is_sha256(self.environment_digest):
                raise EnvironmentLedgerError("environment_digest must be 64 lowercase hex")
            expected_reason: EnvironmentReason = (
                "snapshot_validated" if self.mode == "snapshot" else "recorded_tool_validated"
            )
            if self.reason != expected_reason:
                raise EnvironmentLedgerError("valid environment record reason does not match mode")
            if self.mode == "snapshot":
                if self.snapshot_root is None or not Path(self.snapshot_root).is_absolute():
                    raise EnvironmentLedgerError(
                        "snapshot environment record requires an absolute snapshot_root"
                    )
            elif self.snapshot_root is not None:
                raise EnvironmentLedgerError(
                    "recorded-tool environment record must not contain snapshot_root"
                )
            return
        if self.mode is not None or self.environment_digest is not None:
            raise EnvironmentLedgerError(
                "non-valid environment record must not claim an environment"
            )
        if self.snapshot_root is not None:
            raise EnvironmentLedgerError(
                "non-valid environment record must not contain snapshot_root"
            )
        expected_reason = (
            "unsupported_tool" if self.status == "unsupported" else "environment_unavailable"
        )
        if self.reason != expected_reason:
            raise EnvironmentLedgerError(
                "non-valid environment record reason does not match status"
            )

    def to_payload(self) -> dict[str, object]:
        """Return a canonical receipt without paths, calls, or tool output."""
        return {
            "candidate_id": self.candidate_id,
            "environment_digest": self.environment_digest,
            "mode": self.mode,
            "reason": self.reason,
            "snapshot_root": self.snapshot_root,
            "source_sha256": self.source_sha256,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class EnvironmentLedger:
    """Complete redesign-only environment admission decisions for one sealed manifest."""

    epoch_digest: str
    manifest_digest: str
    split: Split
    records: tuple[EnvironmentRecord, ...]

    def __post_init__(self) -> None:
        if not is_sha256(self.manifest_digest):
            raise EnvironmentLedgerError("manifest_digest must be 64 lowercase hex")
        if not is_sha256(self.epoch_digest):
            raise EnvironmentLedgerError("epoch_digest must be 64 lowercase hex")
        if self.split != "redesign":
            raise EnvironmentLedgerError("environment ledger must be scoped to redesign")
        if not self.records:
            raise EnvironmentLedgerError("environment ledger must contain records")
        if len({record.candidate_id for record in self.records}) != len(self.records):
            raise EnvironmentLedgerError("environment ledger has duplicate candidate_id values")

    @property
    def digest(self) -> str:
        """Return the digest of this canonical non-content receipt."""
        return _digest(self.payload_without_digest())

    def payload_without_digest(self) -> dict[str, object]:
        """Return the ledger fields covered by its digest."""
        return {
            "epoch_digest": self.epoch_digest,
            "manifest_digest": self.manifest_digest,
            "records": [
                record.to_payload()
                for record in sorted(self.records, key=lambda record: record.candidate_id)
            ],
            "schema_version": ENVIRONMENT_LEDGER_SCHEMA_VERSION,
            "split": self.split,
        }

    def to_document(self) -> dict[str, object]:
        """Return the serialized authenticated receipt."""
        return {"digest": self.digest, **self.payload_without_digest()}


def assess_environments(epoch_path: Path, manifest_path: Path) -> EnvironmentLedger:
    """Validate only sealed-redesign environments after audit authorization."""
    try:
        epoch, manifest = verify_epoch_manifest(epoch_path, manifest_path)
        records = tuple(
            _assess_redesign_candidate_environment(epoch_path, manifest_path, candidate)
            for candidate in manifest.candidates
            if candidate.split == "redesign"
        )
    except EpochError as error:
        raise EnvironmentLedgerError(str(error)) from error
    return EnvironmentLedger(epoch.digest, manifest.digest, "redesign", records)


def verify_environment(
    epoch_path: Path, manifest_path: Path, ledger_path: Path
) -> EnvironmentLedger:
    """Revalidate redesign-confirmatory environments after audit authorization."""
    try:
        epoch, manifest = verify_epoch_manifest(epoch_path, manifest_path)
    except EpochError as error:
        raise EnvironmentLedgerError(str(error)) from error
    ledger = read_environment_ledger(ledger_path)
    if ledger.manifest_digest != manifest.digest:
        raise EnvironmentLedgerError("ledger manifest_digest does not match manifest")
    if ledger.epoch_digest != epoch.digest:
        raise EnvironmentLedgerError("ledger epoch_digest does not match sealed epoch")
    if ledger.split != "redesign":
        raise EnvironmentLedgerError("ledger split must be redesign")
    candidates = tuple(
        candidate for candidate in manifest.candidates if candidate.split == "redesign"
    )
    records = {record.candidate_id: record for record in ledger.records}
    candidate_ids = {candidate.candidate_id for candidate in candidates}
    if set(records) != candidate_ids:
        raise EnvironmentLedgerError(
            "ledger must contain exactly one record for every redesign manifest candidate"
        )
    for candidate in candidates:
        record = records[candidate.candidate_id]
        if record.source_sha256 != candidate.source_sha256:
            raise EnvironmentLedgerError(
                f"candidate {candidate.candidate_id}: source_sha256 mismatch"
            )
        if candidate.eligibility_disposition != "confirmatory":
            continue
        if record.status != "valid" or record.mode is None:
            raise EnvironmentLedgerError(
                f"candidate {candidate.candidate_id}: confirmatory candidate lacks "
                "valid environment"
            )
        if record.mode == "recorded_tool":
            try:
                record_redesign_access(
                    epoch_path,
                    manifest_path,
                    candidate.candidate_id,
                    "environment_verify",
                    timestamp=_audit_timestamp(),
                )
            except EpochError as error:
                raise EnvironmentLedgerError(
                    f"candidate {candidate.candidate_id}: audit authorization failed: {error}"
                ) from error
            try:
                actual_digest = _recorded_tool_digest(candidate)
            except (NativeEvidenceError, OSError) as error:
                raise EnvironmentLedgerError(
                    f"candidate {candidate.candidate_id}: cannot revalidate "
                    "recorded tool environment"
                ) from error
            if record.environment_digest != actual_digest:
                raise EnvironmentLedgerError(
                    f"candidate {candidate.candidate_id}: recorded tool environment changed"
                )
            continue
        if record.snapshot_root is None or record.environment_digest is None:
            raise EnvironmentLedgerError(
                f"candidate {candidate.candidate_id}: snapshot receipt is incomplete"
            )
        try:
            validate_snapshot(
                SnapshotEnvironment(Path(record.snapshot_root), record.environment_digest)
            )
        except (EnvironmentError, OSError) as error:
            raise EnvironmentLedgerError(
                f"candidate {candidate.candidate_id}: cannot revalidate snapshot environment"
            ) from error
    return ledger


def write_environment_ledger(path: Path, ledger: EnvironmentLedger) -> None:
    """Atomically write a private environment ledger as 0600 in a 0700 directory."""
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
            stream.write(json.dumps(ledger.to_document(), indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as error:
        raise EnvironmentLedgerError(f"cannot write environment ledger {path}: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_environment_ledger(path: Path) -> EnvironmentLedger:
    """Read and authenticate a private non-content environment ledger."""
    _require_private_path(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise EnvironmentLedgerError(f"cannot read environment ledger {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise EnvironmentLedgerError(
            f"environment ledger is not valid JSON: {error.msg}"
        ) from error
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "digest",
        "epoch_digest",
        "manifest_digest",
        "records",
        "split",
    }:
        raise EnvironmentLedgerError("environment ledger has invalid fields")
    if document["schema_version"] != ENVIRONMENT_LEDGER_SCHEMA_VERSION:
        raise EnvironmentLedgerError(
            f"unsupported environment ledger schema {document['schema_version']!r}"
        )
    digest = document["digest"]
    if not isinstance(digest, str) or not is_sha256(digest):
        raise EnvironmentLedgerError("environment ledger digest must be 64 lowercase hex")
    raw_records = document["records"]
    if not isinstance(raw_records, list):
        raise EnvironmentLedgerError("environment ledger records must be an array")
    ledger = EnvironmentLedger(
        _required_text(document, "epoch_digest"),
        _required_text(document, "manifest_digest"),
        cast(Split, _required_text(document, "split")),
        tuple(_record_from_payload(index, record) for index, record in enumerate(raw_records)),
    )
    if not hmac.compare_digest(digest, ledger.digest):
        raise EnvironmentLedgerError("environment ledger digest mismatch")
    return ledger


def environment_counts(ledger: EnvironmentLedger) -> dict[EnvironmentStatus, int]:
    """Return complete status counts for non-content admission diagnostics."""
    return {
        status: sum(record.status == status for record in ledger.records)
        for status in ("valid", "unsupported", "unavailable")
    }


def _record_from_payload(index: int, payload: object) -> EnvironmentRecord:
    if not isinstance(payload, dict) or set(payload) != {
        "candidate_id",
        "environment_digest",
        "mode",
        "reason",
        "snapshot_root",
        "source_sha256",
        "status",
    }:
        raise EnvironmentLedgerError(f"record {index}: invalid fields")
    status = payload["status"]
    if status not in {"valid", "unsupported", "unavailable"}:
        raise EnvironmentLedgerError(f"record {index}: invalid status {status!r}")
    mode = payload["mode"]
    if mode is not None and mode not in {"recorded_tool", "snapshot"}:
        raise EnvironmentLedgerError(f"record {index}: invalid mode {mode!r}")
    environment_digest = payload["environment_digest"]
    if environment_digest is not None and not isinstance(environment_digest, str):
        raise EnvironmentLedgerError(f"record {index}: environment_digest must be a string or null")
    reason = payload["reason"]
    if reason not in {
        "environment_unavailable",
        "recorded_tool_validated",
        "snapshot_validated",
        "unsupported_tool",
    }:
        raise EnvironmentLedgerError(f"record {index}: invalid reason {reason!r}")
    snapshot_root = payload["snapshot_root"]
    if snapshot_root is not None and not isinstance(snapshot_root, str):
        raise EnvironmentLedgerError(f"record {index}: snapshot_root must be a string or null")
    return EnvironmentRecord(
        _required_text(payload, "candidate_id"),
        _required_text(payload, "source_sha256"),
        cast(EnvironmentStatus, status),
        cast(EnvironmentMode | None, mode),
        environment_digest,
        cast(EnvironmentReason, reason),
        snapshot_root,
    )


def _assess_redesign_candidate_environment(
    epoch_path: Path, manifest_path: Path, candidate: Candidate
) -> EnvironmentRecord:
    record_redesign_access(
        epoch_path,
        manifest_path,
        candidate.candidate_id,
        "environment_assess",
        timestamp=_audit_timestamp(),
    )
    return _assess_candidate_environment(candidate)


def _audit_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _assess_candidate_environment(candidate: Candidate) -> EnvironmentRecord:
    if candidate.eligibility_disposition != "confirmatory":
        return EnvironmentRecord(
            candidate.candidate_id,
            candidate.source_sha256,
            "unavailable",
            None,
            None,
            "environment_unavailable",
        )
    try:
        environment_digest = _recorded_tool_digest(candidate)
    except OSError:
        return EnvironmentRecord(
            candidate.candidate_id,
            candidate.source_sha256,
            "unavailable",
            None,
            None,
            "environment_unavailable",
        )
    except NativeEvidenceError:
        return EnvironmentRecord(
            candidate.candidate_id,
            candidate.source_sha256,
            "unsupported",
            None,
            None,
            "unsupported_tool",
        )
    return EnvironmentRecord(
        candidate.candidate_id,
        candidate.source_sha256,
        "valid",
        "recorded_tool",
        environment_digest,
        "recorded_tool_validated",
    )


def _recorded_tool_digest(candidate: Candidate) -> str:
    session = extract_native(candidate)
    validate_confirmatory_evidence(candidate, session)
    calls: list[ToolCall] = []
    results: dict[str, JsonValue] = {}
    for event in session.events:
        if event.kind == "assistant":
            calls.extend(event.tool_calls)
        elif event.tool_result is not None:
            results[event.tool_result.call_id] = event.tool_result.output
    observations = [
        {"input": call.input, "name": call.name, "output": results[call.call_id]} for call in calls
    ]
    try:
        encoded = json.dumps(
            observations, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except ValueError as error:
        raise NativeEvidenceError("recorded tool payload is not canonical JSON") from error
    return hashlib.sha256(encoded).hexdigest()


def _require_private_path(path: Path) -> None:
    try:
        entry_stat = path.lstat()
    except OSError as error:
        raise EnvironmentLedgerError(f"cannot stat environment ledger {path}: {error}") from error
    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISREG(entry_stat.st_mode):
        raise EnvironmentLedgerError("environment ledger must be a non-symlink regular file")
    mode = entry_stat.st_mode & 0o777
    if mode != 0o600:
        raise EnvironmentLedgerError(f"environment ledger must have mode 0600, found {mode:04o}")
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
        raise EnvironmentLedgerError(
            f"cannot stat environment ledger directory {path}: {error}"
        ) from error
    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
        raise EnvironmentLedgerError("environment ledger directory must be a non-symlink directory")
    mode = entry_stat.st_mode & 0o777
    if mode != 0o700:
        raise EnvironmentLedgerError(
            f"environment ledger directory must have mode 0700, found {mode:04o}"
        )


def _required_text(payload: dict[str, object], field_name: str) -> str:
    value = payload[field_name]
    if not isinstance(value, str) or not value.strip():
        raise EnvironmentLedgerError(f"{field_name} must be a non-empty string")
    return value


def _digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
