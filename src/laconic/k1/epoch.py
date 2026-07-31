"""Private, content-sealed K1 evidence epochs and access audits."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, cast

from laconic.k1.manifest import Candidate, Manifest, ManifestError, Split, is_sha256, read_manifest
from laconic.k1.split import SplitError, validate_frozen_split

EPOCH_SCHEMA_VERSION: Final = 1
AUDIT_SCHEMA_VERSION: Final = 2


class EpochError(ValueError):
    """Raised when a sealed K1 evidence epoch is malformed or invalid."""


class HoldoutAccessDenied(EpochError):
    """Raised before any pre-M6 holdout source path can be opened."""


@dataclass(frozen=True, slots=True)
class SealedEpoch:
    """The non-content receipt that starts one fresh K1 evidence epoch."""

    epoch_id: str
    created_at: str
    manifest_digest: str
    split_digest: str
    approved_roots: tuple[Path, ...]
    audit_path: Path

    def __post_init__(self) -> None:
        _require_identifier("epoch_id", self.epoch_id)
        _require_timestamp(self.created_at)
        for field_name, value in (
            ("manifest_digest", self.manifest_digest),
            ("split_digest", self.split_digest),
        ):
            if not is_sha256(value):
                raise EpochError(f"{field_name} must be 64 lowercase hex")
        if not self.approved_roots:
            raise EpochError("approved_roots must not be empty")
        normalized_roots = tuple(sorted({_normalize_path(root) for root in self.approved_roots}))
        if len(normalized_roots) != len(self.approved_roots):
            raise EpochError("approved_roots must be unique")
        object.__setattr__(self, "approved_roots", normalized_roots)
        audit_path = _normalize_path(self.audit_path)
        if not _is_within_any(audit_path, normalized_roots):
            raise EpochError("audit_path must be within an approved private root")
        object.__setattr__(self, "audit_path", audit_path)

    @property
    def digest(self) -> str:
        """Return the canonical identity of this sealed epoch."""
        return _digest(self.payload_without_digest())

    def payload_without_digest(self) -> dict[str, object]:
        """Return non-content receipt fields covered by the epoch digest."""
        return {
            "approved_roots": [str(root) for root in self.approved_roots],
            "audit_path": str(self.audit_path),
            "created_at": self.created_at,
            "epoch_id": self.epoch_id,
            "manifest_digest": self.manifest_digest,
            "schema_version": EPOCH_SCHEMA_VERSION,
            "split_digest": self.split_digest,
        }

    def to_document(self) -> dict[str, object]:
        """Return the authenticated private epoch document."""
        return {"digest": self.digest, **self.payload_without_digest()}


@dataclass(frozen=True, slots=True)
class AccessAuditRecord:
    """One allowed, non-content private source access in a sealed epoch."""

    candidate_id: str
    operation: str
    split: Split
    timestamp: str
    previous_digest: str

    def __post_init__(self) -> None:
        _require_identifier("candidate_id", self.candidate_id)
        _require_identifier("operation", self.operation)
        if self.split not in {"redesign", "holdout"}:
            raise EpochError(f"unknown audit split {self.split!r}")
        _require_timestamp(self.timestamp)
        if not is_sha256(self.previous_digest):
            raise EpochError("previous_digest must be 64 lowercase hex")

    @property
    def digest(self) -> str:
        """Return the hash-chain link for this access record."""
        return _digest(self.payload_without_digest())

    def payload_without_digest(self) -> dict[str, object]:
        """Return the canonical non-content record fields."""
        return {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "candidate_id": self.candidate_id,
            "operation": self.operation,
            "previous_digest": self.previous_digest,
            "split": self.split,
            "timestamp": self.timestamp,
        }

    def to_document(self) -> dict[str, object]:
        """Return the canonical record with its chain digest."""
        return {"digest": self.digest, **self.payload_without_digest()}


@dataclass(frozen=True, slots=True)
class AccessAudit:
    """A hash-chained record of allowed K1 source access for one epoch."""

    epoch_digest: str
    records: tuple[AccessAuditRecord, ...]

    def __post_init__(self) -> None:
        if not is_sha256(self.epoch_digest):
            raise EpochError("epoch_digest must be 64 lowercase hex")
        previous_digest = self.epoch_digest
        for record in self.records:
            if record.previous_digest != previous_digest:
                raise EpochError("access audit hash chain is discontinuous")
            previous_digest = record.digest

    @property
    def head_digest(self) -> str:
        """Return the bound audit-chain head, including the empty-audit anchor."""
        return self.records[-1].digest if self.records else self.epoch_digest

    def to_document(self) -> dict[str, object]:
        """Return the private audit document with its current chain head."""
        return {
            "epoch_digest": self.epoch_digest,
            "head_digest": self.head_digest,
            "records": [record.to_document() for record in self.records],
            "schema_version": AUDIT_SCHEMA_VERSION,
        }


def split_digest(manifest: Manifest) -> str:
    """Return a standalone canonical binding for a frozen manifest's assignment."""
    try:
        validate_frozen_split(manifest)
    except SplitError as error:
        raise EpochError(str(error)) from error
    assignments = [
        {"candidate_id": candidate.candidate_id, "split": candidate.split}
        for candidate in sorted(manifest.candidates, key=lambda candidate: candidate.candidate_id)
    ]
    return _digest({"assignments": assignments, "schema_version": EPOCH_SCHEMA_VERSION})


def create_epoch(
    manifest_path: Path,
    epoch_path: Path,
    *,
    audit_path: Path,
    approved_roots: tuple[Path, ...],
    epoch_id: str,
    created_at: str,
) -> SealedEpoch:
    """Seal a frozen manifest without opening any candidate source path."""
    _require_private_file(manifest_path)
    manifest = read_manifest(manifest_path)
    receipt = SealedEpoch(
        epoch_id=epoch_id,
        created_at=created_at,
        manifest_digest=manifest.digest,
        split_digest=split_digest(manifest),
        approved_roots=approved_roots,
        audit_path=audit_path,
    )
    _validate_epoch_locations(manifest, receipt, manifest_path, epoch_path)
    _write_new_epoch(epoch_path, receipt)
    return receipt


def read_epoch(path: Path) -> SealedEpoch:
    """Read and authenticate a private sealed-epoch receipt."""
    document = _read_private_json(path, "sealed epoch")
    expected_fields = {
        "approved_roots",
        "audit_path",
        "created_at",
        "digest",
        "epoch_id",
        "manifest_digest",
        "schema_version",
        "split_digest",
    }
    if set(document) != expected_fields:
        raise EpochError("sealed epoch has invalid fields")
    schema_version = document["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != EPOCH_SCHEMA_VERSION
    ):
        raise EpochError(f"unsupported epoch schema_version {schema_version!r}")
    digest = document["digest"]
    if not isinstance(digest, str) or not is_sha256(digest):
        raise EpochError("sealed epoch digest must be 64 lowercase hex")
    roots = document["approved_roots"]
    if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
        raise EpochError("approved_roots must be an array of paths")
    required_strings = ("epoch_id", "created_at", "manifest_digest", "split_digest", "audit_path")
    if any(not isinstance(document[field], str) for field in required_strings):
        raise EpochError("sealed epoch contains invalid field types")
    epoch_id = cast(str, document["epoch_id"])
    created_at = cast(str, document["created_at"])
    manifest_digest = cast(str, document["manifest_digest"])
    assignment_digest = cast(str, document["split_digest"])
    audit_path = cast(str, document["audit_path"])
    epoch = SealedEpoch(
        epoch_id=epoch_id,
        created_at=created_at,
        manifest_digest=manifest_digest,
        split_digest=assignment_digest,
        approved_roots=tuple(Path(root) for root in roots),
        audit_path=Path(audit_path),
    )
    if not hmac.compare_digest(digest, epoch.digest):
        raise EpochError("sealed epoch digest mismatch")
    return epoch


def read_access_audit(path: Path) -> AccessAudit:
    """Read and authenticate a private hash-chained access audit."""
    document = _read_private_json(path, "access audit")
    expected_fields = {"epoch_digest", "head_digest", "records", "schema_version"}
    if set(document) != expected_fields:
        raise EpochError("access audit has invalid fields")
    schema_version = document["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != AUDIT_SCHEMA_VERSION
    ):
        raise EpochError(f"unsupported audit schema_version {schema_version!r}")
    epoch_digest = document["epoch_digest"]
    head_digest = document["head_digest"]
    records = document["records"]
    if not isinstance(epoch_digest, str) or not is_sha256(epoch_digest):
        raise EpochError("access audit epoch_digest must be 64 lowercase hex")
    if not isinstance(head_digest, str) or not is_sha256(head_digest):
        raise EpochError("access audit head_digest must be 64 lowercase hex")
    if not isinstance(records, list):
        raise EpochError("access audit records must be an array")
    audit = AccessAudit(epoch_digest, tuple(_record_from_document(record) for record in records))
    if not hmac.compare_digest(head_digest, audit.head_digest):
        raise EpochError("access audit head_digest mismatch")
    return audit


def verify_epoch(epoch_path: Path, manifest_path: Path) -> SealedEpoch:
    """Verify a sealed epoch without hashing or opening candidate source content."""
    epoch, _ = _verify_epoch(epoch_path, manifest_path)
    return epoch


def verify_epoch_manifest(epoch_path: Path, manifest_path: Path) -> tuple[SealedEpoch, Manifest]:
    """Return the sealed epoch and its single authenticated metadata-only manifest."""
    return _verify_epoch(epoch_path, manifest_path)


def _verify_epoch(epoch_path: Path, manifest_path: Path) -> tuple[SealedEpoch, Manifest]:
    epoch = read_epoch(epoch_path)
    _require_private_file(manifest_path)
    try:
        manifest = read_manifest(manifest_path)
    except ManifestError as error:
        raise EpochError(str(error)) from error
    if not hmac.compare_digest(manifest.digest, epoch.manifest_digest):
        raise EpochError("epoch manifest digest mismatch")
    if not hmac.compare_digest(split_digest(manifest), epoch.split_digest):
        raise EpochError("epoch split digest mismatch")
    _validate_epoch_locations(manifest, epoch, manifest_path, epoch_path)
    audit = read_access_audit(epoch.audit_path)
    if not hmac.compare_digest(audit.epoch_digest, epoch.digest):
        raise EpochError("access audit does not belong to sealed epoch")
    return epoch, manifest


def record_redesign_access(
    epoch_path: Path,
    manifest_path: Path,
    candidate_id: str,
    operation: str,
    *,
    timestamp: str,
) -> AccessAuditRecord:
    """Authorize and append one redesign-only access before opening its source path."""
    epoch, manifest = _verify_epoch(epoch_path, manifest_path)
    candidate = _candidate_by_id(manifest, candidate_id)
    if candidate.split == "holdout":
        raise HoldoutAccessDenied(
            f"candidate {candidate_id!r} is sealed holdout; M6 unlock is required before path open"
        )
    audit = read_access_audit(epoch.audit_path)
    record = AccessAuditRecord(
        candidate_id=candidate.candidate_id,
        operation=operation,
        split=candidate.split,
        timestamp=timestamp,
        previous_digest=audit.head_digest,
    )
    _write_private_json(
        epoch.audit_path,
        AccessAudit(epoch.digest, (*audit.records, record)).to_document(),
    )
    return record


def _candidate_by_id(manifest: Manifest, candidate_id: str) -> Candidate:
    for candidate in manifest.candidates:
        if candidate.candidate_id == candidate_id:
            return candidate
    raise EpochError(f"candidate {candidate_id!r} is absent from sealed manifest")


def _record_from_document(raw: object) -> AccessAuditRecord:
    if not isinstance(raw, dict):
        raise EpochError("access audit record must be an object")
    expected_fields = {
        "candidate_id",
        "digest",
        "operation",
        "previous_digest",
        "split",
        "timestamp",
        "schema_version",
    }
    if set(raw) != expected_fields:
        raise EpochError("access audit record has invalid fields")
    values = ("candidate_id", "operation", "previous_digest", "split", "timestamp", "digest")
    if any(not isinstance(raw[field], str) for field in values):
        raise EpochError("access audit record contains invalid field types")
    record_digest = cast(str, raw["digest"])
    if not is_sha256(record_digest):
        raise EpochError("access audit record digest must be 64 lowercase hex")
    schema_version = raw["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != AUDIT_SCHEMA_VERSION
    ):
        raise EpochError(f"unsupported access audit record schema {schema_version!r}")
    record = AccessAuditRecord(
        candidate_id=raw["candidate_id"],
        operation=raw["operation"],
        split=raw["split"],
        timestamp=raw["timestamp"],
        previous_digest=raw["previous_digest"],
    )
    if not hmac.compare_digest(record_digest, record.digest):
        raise EpochError("access audit record digest mismatch")
    return record


def _validate_epoch_locations(
    manifest: Manifest,
    epoch: SealedEpoch,
    manifest_path: Path,
    epoch_path: Path,
) -> None:
    artifact_paths = (
        ("manifest_path", _normalize_path(manifest_path)),
        ("epoch_path", _normalize_path(epoch_path)),
        ("audit_path", epoch.audit_path),
    )
    for label, artifact_path in artifact_paths:
        if not _is_within_any(artifact_path, epoch.approved_roots):
            raise EpochError(f"{label} must be within approved private roots")
    for root in epoch.approved_roots:
        _require_private_directory(root)
    for _, artifact_path in artifact_paths[1:]:
        _require_private_directory(artifact_path.parent)
    for candidate in manifest.candidates:
        candidate_path = _normalize_path(candidate.source_path)
        if not _is_within_any(candidate_path, epoch.approved_roots):
            raise EpochError(
                f"candidate {candidate.candidate_id!r} source_path is outside approved roots"
            )


def _normalize_path(path: Path) -> Path:
    if not path.is_absolute():
        raise EpochError(f"private path must be absolute: {path}")
    return path.resolve(strict=False)


def _is_within_any(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def _write_new_epoch(epoch_path: Path, epoch: SealedEpoch) -> None:
    reserved: list[Path] = []
    paths = (_normalize_path(epoch_path), epoch.audit_path)
    if paths[0] == paths[1]:
        raise EpochError("epoch_path and audit_path must be distinct")
    try:
        for path in paths:
            _reserve_private_file(path)
            reserved.append(path)
        _write_private_json(paths[0], epoch.to_document())
        _write_private_json(paths[1], AccessAudit(epoch.digest, ()).to_document())
    except EpochError:
        for path in reserved:
            path.unlink(missing_ok=True)
        raise


def _reserve_private_file(path: Path) -> None:
    path = _normalize_path(path)
    _require_private_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise EpochError(f"refusing to overwrite existing private artifact {path}") from error
    except OSError as error:
        raise EpochError(f"cannot reserve private artifact {path}: {error}") from error
    else:
        os.close(descriptor)


def _write_private_json(path: Path, document: dict[str, object]) -> None:
    path = _normalize_path(path)
    _require_private_directory(path.parent)
    temporary: Path | None = None
    try:
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
            stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as error:
        raise EpochError(f"cannot write private artifact {path}: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_private_json(path: Path, label: str) -> dict[str, object]:
    _require_private_file(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise EpochError(f"cannot read {label} {path}: {error}") from error
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise EpochError(f"{label} is not valid JSON: {error.msg}") from error
    if not isinstance(document, dict):
        raise EpochError(f"{label} root must be an object")
    return document


def _require_private_file(path: Path) -> None:
    path = _normalize_path(path)
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as error:
        raise EpochError(f"cannot stat private artifact {path}: {error}") from error
    if mode != 0o600:
        raise EpochError(f"private artifact must have mode 0600, found {mode:04o}")
    _require_private_directory(path.parent)


def _require_private_directory(path: Path) -> None:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as error:
        raise EpochError(f"cannot stat private directory {path}: {error}") from error
    if mode != 0o700:
        raise EpochError(f"private directory must have mode 0700, found {mode:04o}")


def _require_identifier(field_name: str, value: str) -> None:
    if not value or value in {".", ".."} or any(character in value for character in "/\\\x00"):
        raise EpochError(f"{field_name} must be a non-empty simple identifier")


def _require_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EpochError(f"timestamp is invalid: {value!r}") from error
    if parsed.tzinfo is None:
        raise EpochError(f"timestamp must include a timezone: {value!r}")


def _digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
