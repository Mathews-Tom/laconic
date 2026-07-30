"""Metadata-only, content-addressed candidate manifests for K1."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, cast

SCHEMA_VERSION: Final = 1
SHA256_HEX_LENGTH: Final = 64
_SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
_DISPOSITIONS: Final = frozenset({"unreviewed", "diagnostic_only", "confirmatory", "excluded"})
_SPLITS: Final = frozenset({"unassigned", "redesign", "holdout"})

EligibilityDisposition = Literal["unreviewed", "diagnostic_only", "confirmatory", "excluded"]
Split = Literal["unassigned", "redesign", "holdout"]


class ManifestError(ValueError):
    """Raised when a K1 manifest is malformed or no longer matches its sources."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """One immutable native transcript reference and its selection metadata."""

    candidate_id: str
    source_path: Path
    source_sha256: str
    provider: str
    model: str | None
    model_family: str
    project: str
    timestamp: str
    session_length: int
    message_count: int
    has_code: bool
    tool_density: float
    time_period: str
    session_size_band: str
    selection_stratum: str
    lineage: str
    eligibility_disposition: EligibilityDisposition
    split: Split

    def __post_init__(self) -> None:
        _require_text("candidate_id", self.candidate_id)
        if not self.source_path.is_absolute():
            raise ManifestError(f"candidate {self.candidate_id}: source_path must be absolute")
        if _SHA256_PATTERN.fullmatch(self.source_sha256) is None:
            raise ManifestError(
                f"candidate {self.candidate_id}: source_sha256 must be 64 lowercase hex"
            )
        _require_stratum_component("provider", self.provider)
        if self.model is not None:
            _require_text("model", self.model)
        _require_stratum_component("model_family", self.model_family)
        _require_text("project", self.project)
        _parse_timestamp(self.timestamp)
        if self.session_length < 0:
            raise ManifestError(
                f"candidate {self.candidate_id}: session_length must be non-negative"
            )
        if self.message_count < 0:
            raise ManifestError(
                f"candidate {self.candidate_id}: message_count must be non-negative"
            )
        if not math.isfinite(self.tool_density) or self.tool_density < 0:
            raise ManifestError(
                f"candidate {self.candidate_id}: tool_density must be finite and non-negative"
            )
        _require_stratum_component("time_period", self.time_period)
        _require_stratum_component("session_size_band", self.session_size_band)
        _require_text("lineage", self.lineage)
        if self.eligibility_disposition not in _DISPOSITIONS:
            raise ManifestError(
                f"candidate {self.candidate_id}: unknown eligibility_disposition "
                f"{self.eligibility_disposition!r}"
            )
        if self.split not in _SPLITS:
            raise ManifestError(f"candidate {self.candidate_id}: unknown split {self.split!r}")
        expected_stratum = stratum_for(self)
        if self.selection_stratum != expected_stratum:
            raise ManifestError(
                f"candidate {self.candidate_id}: selection_stratum must equal {expected_stratum!r}"
            )

    def to_payload(self) -> dict[str, object]:
        """Return the declared metadata fields without any transcript content."""
        return {
            "candidate_id": self.candidate_id,
            "eligibility_disposition": self.eligibility_disposition,
            "has_code": self.has_code,
            "lineage": self.lineage,
            "message_count": self.message_count,
            "model": self.model,
            "model_family": self.model_family,
            "project": self.project,
            "provider": self.provider,
            "selection_stratum": self.selection_stratum,
            "session_length": self.session_length,
            "session_size_band": self.session_size_band,
            "source_path": str(self.source_path),
            "source_sha256": self.source_sha256,
            "split": self.split,
            "time_period": self.time_period,
            "timestamp": self.timestamp,
            "tool_density": self.tool_density,
        }


@dataclass(frozen=True, slots=True)
class Manifest:
    """A versioned, canonical collection of K1 candidate metadata."""

    candidates: tuple[Candidate, ...]

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ManifestError("manifest must contain at least one candidate")
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ManifestError("manifest contains duplicate candidate_id values")
        source_hashes = [candidate.source_sha256 for candidate in self.candidates]
        if len(source_hashes) != len(set(source_hashes)):
            raise ManifestError("manifest contains duplicate source_sha256 values")

    def payload_without_digest(self) -> dict[str, object]:
        """Return the canonical payload covered by the manifest digest."""
        candidates = sorted(self.candidates, key=lambda candidate: candidate.candidate_id)
        return {
            "candidates": [candidate.to_payload() for candidate in candidates],
            "schema_version": SCHEMA_VERSION,
        }

    @property
    def digest(self) -> str:
        """Return the SHA-256 digest of the canonical metadata payload."""
        return _digest_payload(self.payload_without_digest())

    def to_document(self) -> dict[str, object]:
        """Return the serialized manifest document, including its digest."""
        return {"digest": self.digest, **self.payload_without_digest()}


def stratum_from_metadata(
    provider: str,
    model_family: str,
    time_period: str,
    session_size_band: str,
) -> str:
    """Return a stable selection stratum from its four declared dimensions."""
    return "|".join((provider, model_family, time_period, session_size_band))


def stratum_for(candidate: Candidate) -> str:
    """Return the declared representational stratum for a candidate."""
    return stratum_from_metadata(
        candidate.provider,
        candidate.model_family,
        candidate.time_period,
        candidate.session_size_band,
    )


def is_sha256(value: str) -> bool:
    """Return whether value is a lowercase SHA-256 hexadecimal digest."""
    return _SHA256_PATTERN.fullmatch(value) is not None


def read_manifest(path: Path) -> Manifest:
    """Read and authenticate a metadata-only manifest without reading sources."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ManifestError(f"cannot read manifest {path}: {error}") from error
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ManifestError(f"manifest {path} is not valid JSON: {error.msg}") from error
    if not isinstance(document, dict):
        raise ManifestError("manifest root must be an object")
    allowed_keys = {"schema_version", "digest", "candidates"}
    _require_exact_keys("manifest", document, allowed_keys)
    schema_version = document["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SCHEMA_VERSION
    ):
        raise ManifestError(f"unsupported manifest schema_version {schema_version!r}")
    digest = document["digest"]
    if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
        raise ManifestError("manifest digest must be 64 lowercase hex")
    candidates_raw = document["candidates"]
    if not isinstance(candidates_raw, list):
        raise ManifestError("manifest candidates must be an array")
    manifest = Manifest(
        tuple(_candidate_from_payload(index, item) for index, item in enumerate(candidates_raw))
    )
    actual_digest = manifest.digest
    if not hmac.compare_digest(digest, actual_digest):
        raise ManifestError(
            f"manifest digest mismatch: expected {digest}, computed {actual_digest}"
        )
    return manifest


def write_manifest(path: Path, manifest: Manifest) -> None:
    """Atomically write a canonical manifest document to a private path."""
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        rendered = (
            json.dumps(manifest.to_document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
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
            stream.write(rendered)
        os.replace(temporary, path)
    except OSError as error:
        raise ManifestError(f"cannot write manifest {path}: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def verify_manifest(path: Path, *, require_frozen_split: bool = True) -> Manifest:
    """Verify a manifest's digest, split state, and every referenced source hash."""
    manifest = read_manifest(path)
    if require_frozen_split and any(
        candidate.split == "unassigned" for candidate in manifest.candidates
    ):
        raise ManifestError(
            "manifest has unassigned candidates; freeze its split before verification"
        )
    if require_frozen_split:
        from laconic.k1.split import validate_frozen_split

        validate_frozen_split(manifest)
    for candidate in manifest.candidates:
        try:
            actual_hash = source_sha256(candidate.source_path)
        except OSError as error:
            raise ManifestError(
                f"candidate {candidate.candidate_id}: cannot hash {candidate.source_path}: {error}"
            ) from error
        if not hmac.compare_digest(candidate.source_sha256, actual_hash):
            raise ManifestError(
                f"candidate {candidate.candidate_id}: source hash mismatch for "
                f"{candidate.source_path}"
            )
    return manifest


def source_sha256(path: Path) -> str:
    """Return a file's SHA-256 with bounded memory use."""
    if not path.is_file():
        raise OSError(f"not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_from_payload(index: int, raw: object) -> Candidate:
    if not isinstance(raw, dict):
        raise ManifestError(f"candidate {index}: must be an object")
    allowed_keys = {
        "candidate_id",
        "eligibility_disposition",
        "has_code",
        "lineage",
        "message_count",
        "model",
        "model_family",
        "project",
        "provider",
        "selection_stratum",
        "session_length",
        "session_size_band",
        "source_path",
        "source_sha256",
        "split",
        "time_period",
        "timestamp",
        "tool_density",
    }
    _require_exact_keys(f"candidate {index}", raw, allowed_keys)
    candidate_id = _required_str(raw, "candidate_id", index)
    source_path = Path(_required_str(raw, "source_path", index)).expanduser().resolve()
    source_sha = _required_str(raw, "source_sha256", index)
    provider = _required_str(raw, "provider", index)
    model_raw = raw["model"]
    if model_raw is not None and not isinstance(model_raw, str):
        raise ManifestError(f"candidate {index}: model must be a string or null")
    model = model_raw
    return Candidate(
        candidate_id=candidate_id,
        source_path=source_path,
        source_sha256=source_sha,
        provider=provider,
        model=model,
        model_family=_required_str(raw, "model_family", index),
        project=_required_str(raw, "project", index),
        timestamp=_required_str(raw, "timestamp", index),
        session_length=_required_int(raw, "session_length", index),
        message_count=_required_int(raw, "message_count", index),
        has_code=_required_bool(raw, "has_code", index),
        tool_density=_required_float(raw, "tool_density", index),
        time_period=_required_str(raw, "time_period", index),
        session_size_band=_required_str(raw, "session_size_band", index),
        selection_stratum=_required_str(raw, "selection_stratum", index),
        lineage=_required_str(raw, "lineage", index),
        eligibility_disposition=_required_disposition(raw, index),
        split=_required_split(raw, index),
    )


def _digest_payload(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _parse_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ManifestError(f"timestamp must be ISO-8601: {value!r}") from error
    if parsed.tzinfo is None:
        raise ManifestError(f"timestamp must include a timezone: {value!r}")


def _require_text(field: str, value: str) -> None:
    if not value.strip():
        raise ManifestError(f"{field} must not be empty")


def _require_stratum_component(field: str, value: str) -> None:
    _require_text(field, value)
    if "|" in value:
        raise ManifestError(f"{field} must not contain '|'; it is a stratum delimiter")


def _require_exact_keys(name: str, payload: dict[str, object], allowed: set[str]) -> None:
    keys = set(payload)
    if keys != allowed:
        missing = sorted(allowed - keys)
        unexpected = sorted(keys - allowed)
        detail: list[str] = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if unexpected:
            detail.append(f"unexpected {', '.join(unexpected)}")
        raise ManifestError(f"{name} has invalid fields: {'; '.join(detail)}")


def _required_str(payload: dict[str, object], field: str, index: int) -> str:
    value = payload[field]
    if not isinstance(value, str):
        raise ManifestError(f"candidate {index}: {field} must be a string")
    return value


def _required_int(payload: dict[str, object], field: str, index: int) -> int:
    value = payload[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ManifestError(f"candidate {index}: {field} must be an integer")
    return value


def _required_bool(payload: dict[str, object], field: str, index: int) -> bool:
    value = payload[field]
    if not isinstance(value, bool):
        raise ManifestError(f"candidate {index}: {field} must be a boolean")
    return value


def _required_float(payload: dict[str, object], field: str, index: int) -> float:
    value = payload[field]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ManifestError(f"candidate {index}: {field} must be a number")
    try:
        parsed = float(value)
    except OverflowError as error:
        raise ManifestError(f"candidate {index}: {field} is outside the float range") from error
    if not math.isfinite(parsed):
        raise ManifestError(f"candidate {index}: {field} must be finite")
    return parsed


def _required_disposition(payload: dict[str, object], index: int) -> EligibilityDisposition:
    value = payload["eligibility_disposition"]
    if value not in _DISPOSITIONS:
        raise ManifestError(f"candidate {index}: unknown eligibility_disposition {value!r}")
    return cast(EligibilityDisposition, value)


def _required_split(payload: dict[str, object], index: int) -> Split:
    value = payload["split"]
    if value not in _SPLITS:
        raise ManifestError(f"candidate {index}: unknown split {value!r}")
    return cast(Split, value)
