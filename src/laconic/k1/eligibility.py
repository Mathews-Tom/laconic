"""Private, source-linked eligibility ledgers for K1 native evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from laconic.k1.evidence import (
    EvidenceFailureCause,
    NativeEvidenceError,
    NativeSession,
    validate_confirmatory_evidence,
)
from laconic.k1.extractors import extract_native
from laconic.k1.manifest import Candidate, Manifest, ManifestError, is_sha256, read_manifest

LEDGER_SCHEMA_VERSION = 1

LedgerDisposition = Literal["confirmatory", "diagnostic_only", "excluded"]


class EligibilityLedgerError(ValueError):
    """Raised when a private K1 eligibility ledger is malformed or incomplete."""


@dataclass(frozen=True, slots=True)
class EligibilityRecord:
    """One non-content eligibility disposition for a frozen manifest candidate."""

    candidate_id: str
    source_sha256: str
    disposition: LedgerDisposition
    reason: str
    parser: str | None
    model: str | None
    event_count: int | None

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise EligibilityLedgerError("candidate_id must not be empty")
        if not is_sha256(self.source_sha256):
            raise EligibilityLedgerError("source_sha256 must be 64 lowercase hex")
        if self.disposition not in {"confirmatory", "diagnostic_only", "excluded"}:
            raise EligibilityLedgerError(f"unknown disposition {self.disposition!r}")
        if not self.reason.strip():
            raise EligibilityLedgerError("reason must not be empty")
        if self.disposition == "confirmatory":
            if self.parser is None or not self.parser.strip():
                raise EligibilityLedgerError("confirmatory record requires parser")
            if self.model is None or not self.model.strip():
                raise EligibilityLedgerError("confirmatory record requires model")
            if self.event_count is None or self.event_count < 1:
                raise EligibilityLedgerError("confirmatory record requires positive event_count")
        elif any(value is not None for value in (self.parser, self.model, self.event_count)):
            raise EligibilityLedgerError(
                "non-confirmatory record must not claim extracted evidence"
            )

    def to_payload(self) -> dict[str, object]:
        """Return canonical non-content serialization."""
        return {
            "candidate_id": self.candidate_id,
            "disposition": self.disposition,
            "event_count": self.event_count,
            "model": self.model,
            "parser": self.parser,
            "reason": self.reason,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class EligibilityLedger:
    """A complete set of K1 eligibility decisions for one frozen manifest."""

    manifest_digest: str
    records: tuple[EligibilityRecord, ...]

    def __post_init__(self) -> None:
        if not is_sha256(self.manifest_digest):
            raise EligibilityLedgerError("manifest_digest must be 64 lowercase hex")
        if not self.records:
            raise EligibilityLedgerError("eligibility ledger must contain records")
        ids = [record.candidate_id for record in self.records]
        if len(ids) != len(set(ids)):
            raise EligibilityLedgerError("eligibility ledger has duplicate candidate_id values")

    @property
    def digest(self) -> str:
        """Return the digest of the canonical ledger payload."""
        return _digest(self.payload_without_digest())

    def payload_without_digest(self) -> dict[str, object]:
        """Return canonical private ledger content without its digest."""
        return {
            "manifest_digest": self.manifest_digest,
            "records": [
                record.to_payload()
                for record in sorted(self.records, key=lambda item: item.candidate_id)
            ],
            "schema_version": LEDGER_SCHEMA_VERSION,
        }

    def to_document(self) -> dict[str, object]:
        """Return serialized private ledger content with its digest."""
        return {"digest": self.digest, **self.payload_without_digest()}


def assess_manifest(manifest: Manifest) -> EligibilityLedger:
    """Probe every native source and produce one fail-closed disposition each."""
    records = tuple(_assess_candidate(candidate) for candidate in manifest.candidates)
    return EligibilityLedger(manifest.digest, records)


def verify_eligibility(manifest_path: Path, ledger_path: Path) -> EligibilityLedger:
    """Verify ledger completeness and revalidate every confirmatory source."""
    try:
        manifest = read_manifest(manifest_path)
    except ManifestError as error:
        raise EligibilityLedgerError(str(error)) from error
    ledger = read_eligibility_ledger(ledger_path)
    if ledger.manifest_digest != manifest.digest:
        raise EligibilityLedgerError("ledger manifest_digest does not match manifest")
    by_id = {record.candidate_id: record for record in ledger.records}
    manifest_ids = {candidate.candidate_id for candidate in manifest.candidates}
    if set(by_id) != manifest_ids:
        raise EligibilityLedgerError(
            "ledger must contain exactly one record for every manifest candidate"
        )
    for candidate in manifest.candidates:
        record = by_id[candidate.candidate_id]
        if record.source_sha256 != candidate.source_sha256:
            raise EligibilityLedgerError(
                f"candidate {candidate.candidate_id}: source_sha256 mismatch"
            )
        if candidate.eligibility_disposition not in {"unreviewed", record.disposition}:
            raise EligibilityLedgerError(
                f"candidate {candidate.candidate_id}: manifest and ledger dispositions disagree"
            )
        if record.disposition == "confirmatory":
            try:
                session = _confirmatory_session(candidate)
            except (NativeEvidenceError, OSError) as error:
                raise EligibilityLedgerError(
                    f"candidate {candidate.candidate_id}: confirmatory evidence no longer extracts"
                ) from error
            if (
                record.parser != session.parser
                or record.model != session.model
                or record.event_count != len(session.events)
            ):
                raise EligibilityLedgerError(
                    f"candidate {candidate.candidate_id}: recorded confirmatory evidence changed"
                )
    return ledger


def write_eligibility_ledger(path: Path, ledger: EligibilityLedger) -> None:
    """Atomically write a ledger to a 0700 directory as a 0600 file."""
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
        raise EligibilityLedgerError(f"cannot write eligibility ledger {path}: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_eligibility_ledger(path: Path) -> EligibilityLedger:
    """Read and authenticate a private eligibility ledger."""
    _require_private_path(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise EligibilityLedgerError(f"cannot read eligibility ledger {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise EligibilityLedgerError(
            f"eligibility ledger is not valid JSON: {error.msg}"
        ) from error
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "digest",
        "manifest_digest",
        "records",
    }:
        raise EligibilityLedgerError("eligibility ledger has invalid fields")
    if document["schema_version"] != LEDGER_SCHEMA_VERSION:
        raise EligibilityLedgerError(
            f"unsupported eligibility ledger schema {document['schema_version']!r}"
        )
    digest = document["digest"]
    if not isinstance(digest, str) or not is_sha256(digest):
        raise EligibilityLedgerError("eligibility ledger digest must be 64 lowercase hex")
    raw_records = document["records"]
    if not isinstance(raw_records, list):
        raise EligibilityLedgerError("eligibility ledger records must be an array")
    ledger = EligibilityLedger(
        _required_text(document, "manifest_digest"),
        tuple(_record_from_payload(index, item) for index, item in enumerate(raw_records)),
    )
    if not hmac.compare_digest(digest, ledger.digest):
        raise EligibilityLedgerError("eligibility ledger digest mismatch")
    return ledger


def _assess_candidate(candidate: Candidate) -> EligibilityRecord:
    try:
        session = _confirmatory_session(candidate)
    except OSError:
        return EligibilityRecord(
            candidate.candidate_id,
            candidate.source_sha256,
            "excluded",
            "native source is unavailable",
            None,
            None,
            None,
        )
    except NativeEvidenceError as error:
        return EligibilityRecord(
            candidate.candidate_id,
            candidate.source_sha256,
            _failure_disposition(error.cause),
            str(error),
            None,
            None,
            None,
        )
    return EligibilityRecord(
        candidate.candidate_id,
        candidate.source_sha256,
        "confirmatory",
        "native evidence satisfies K1 confirmatory contract",
        session.parser,
        session.model,
        len(session.events),
    )


def _confirmatory_session(candidate: Candidate) -> NativeSession:
    session = extract_native(candidate)
    validate_confirmatory_evidence(candidate, session)
    return session


def _failure_disposition(cause: EvidenceFailureCause) -> LedgerDisposition:
    return "diagnostic_only" if cause == "missing_evidence" else "excluded"


def _record_from_payload(index: int, payload: object) -> EligibilityRecord:
    if not isinstance(payload, dict) or set(payload) != {
        "candidate_id",
        "disposition",
        "event_count",
        "model",
        "parser",
        "reason",
        "source_sha256",
    }:
        raise EligibilityLedgerError(f"record {index}: invalid fields")
    disposition = payload["disposition"]
    if disposition not in {"confirmatory", "diagnostic_only", "excluded"}:
        raise EligibilityLedgerError(f"record {index}: invalid disposition {disposition!r}")
    event_count = payload["event_count"]
    if event_count is not None and (
        not isinstance(event_count, int) or isinstance(event_count, bool)
    ):
        raise EligibilityLedgerError(f"record {index}: event_count must be an integer or null")
    model = payload["model"]
    parser = payload["parser"]
    if model is not None and not isinstance(model, str):
        raise EligibilityLedgerError(f"record {index}: model must be a string or null")
    if parser is not None and not isinstance(parser, str):
        raise EligibilityLedgerError(f"record {index}: parser must be a string or null")
    return EligibilityRecord(
        _required_text(payload, "candidate_id"),
        _required_text(payload, "source_sha256"),
        cast(LedgerDisposition, disposition),
        _required_text(payload, "reason"),
        parser,
        model,
        event_count,
    )


def _require_private_path(path: Path) -> None:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as error:
        raise EligibilityLedgerError(f"cannot stat eligibility ledger {path}: {error}") from error
    if mode != 0o600:
        raise EligibilityLedgerError(f"eligibility ledger must have mode 0600, found {mode:04o}")
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
    mode = path.stat().st_mode & 0o777
    if mode != 0o700:
        raise EligibilityLedgerError(
            f"eligibility ledger directory must have mode 0700, found {mode:04o}"
        )


def _required_text(payload: dict[str, object], field_name: str) -> str:
    value = payload[field_name]
    if not isinstance(value, str) or not value.strip():
        raise EligibilityLedgerError(f"{field_name} must be a non-empty string")
    return value


def _digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
