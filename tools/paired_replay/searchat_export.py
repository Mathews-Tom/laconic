"""Optional adapter from a metadata-only Searchat export to a paired replay manifest.

The adapter accepts a deliberate, allowlisted export file. It never imports Searchat,
opens its database, or reads transcript content; callers create the export outside Laconic.
"""

from __future__ import annotations

import hmac
import json
import math
from pathlib import Path
from typing import cast

from tools.paired_replay.manifest import (
    Candidate,
    EligibilityDisposition,
    Manifest,
    ManifestError,
    is_sha256,
    source_sha256,
    stratum_from_metadata,
    write_manifest,
)
from tools.paired_replay.split import SplitPolicy, freeze_split

_EXPORT_SCHEMA_VERSION = 1
_DISPOSITIONS = frozenset({"unreviewed", "diagnostic_only", "confirmatory", "excluded"})


class SearchatExportError(ManifestError):
    """Raised when an external Searchat metadata export is unsuitable for paired replay."""


def produce_manifest(
    export_path: Path,
    manifest_path: Path,
    *,
    policy: SplitPolicy = SplitPolicy(),
) -> Manifest:
    """Build and write a frozen manifest from a private Searchat metadata export."""
    candidates = tuple(
        _candidate_from_export(index, row) for index, row in enumerate(_read_export(export_path))
    )
    frozen = freeze_split(Manifest(candidates), policy)
    write_manifest(manifest_path, frozen)
    return frozen


def _read_export(path: Path) -> list[object]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SearchatExportError(
            f"cannot read Searchat metadata export {path}: {error}"
        ) from error
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SearchatExportError(
            f"Searchat metadata export is not valid JSON: {error.msg}"
        ) from error
    if not isinstance(document, dict):
        raise SearchatExportError("Searchat metadata export root must be an object")
    _require_exact_keys("Searchat metadata export", document, {"schema_version", "records"})
    schema_version = document["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != _EXPORT_SCHEMA_VERSION
    ):
        raise SearchatExportError(
            f"unsupported Searchat metadata export schema_version {schema_version!r}"
        )
    records = document["records"]
    if not isinstance(records, list):
        raise SearchatExportError("Searchat metadata export records must be an array")
    return records


def _candidate_from_export(index: int, raw: object) -> Candidate:
    if not isinstance(raw, dict):
        raise SearchatExportError(f"Searchat export record {index} must be an object")
    fields = {
        "conversation_id",
        "eligibility_disposition",
        "file_hash",
        "file_path",
        "has_code",
        "lineage",
        "message_count",
        "model",
        "model_family",
        "project_id",
        "provider",
        "session_length",
        "session_size_band",
        "time_period",
        "timestamp",
        "tool_density",
    }
    _require_exact_keys(f"Searchat export record {index}", raw, fields)
    source_path = Path(_string(raw, "file_path", index)).expanduser().resolve()
    exported_hash = _string(raw, "file_hash", index)
    if not is_sha256(exported_hash):
        raise SearchatExportError(
            f"Searchat export record {index}: file_hash must be 64 lowercase hex"
        )
    try:
        actual_hash = source_sha256(source_path)
    except OSError as error:
        raise SearchatExportError(
            f"Searchat export record {index}: cannot hash native source {source_path}: {error}"
        ) from error
    if not hmac.compare_digest(exported_hash, actual_hash):
        raise SearchatExportError(
            f"Searchat export record {index}: file_hash does not match native source {source_path}"
        )
    provider = _string(raw, "provider", index)
    model_family = _string(raw, "model_family", index)
    time_period = _string(raw, "time_period", index)
    session_size_band = _string(raw, "session_size_band", index)
    return Candidate(
        candidate_id=_string(raw, "conversation_id", index),
        source_path=source_path,
        source_sha256=actual_hash,
        provider=provider,
        model=_optional_string(raw, "model", index),
        model_family=model_family,
        project=_string(raw, "project_id", index),
        timestamp=_string(raw, "timestamp", index),
        session_length=_integer(raw, "session_length", index),
        message_count=_integer(raw, "message_count", index),
        has_code=_boolean(raw, "has_code", index),
        tool_density=_number(raw, "tool_density", index),
        time_period=time_period,
        session_size_band=session_size_band,
        selection_stratum=stratum_from_metadata(
            provider,
            model_family,
            time_period,
            session_size_band,
        ),
        lineage=_string(raw, "lineage", index),
        eligibility_disposition=_disposition(raw, index),
        split="unassigned",
    )


def _require_exact_keys(name: str, payload: dict[str, object], allowed: set[str]) -> None:
    keys = set(payload)
    if keys == allowed:
        return
    missing = sorted(allowed - keys)
    unexpected = sorted(keys - allowed)
    detail: list[str] = []
    if missing:
        detail.append(f"missing {', '.join(missing)}")
    if unexpected:
        detail.append(f"unexpected {', '.join(unexpected)}")
    raise SearchatExportError(f"{name} has invalid fields: {'; '.join(detail)}")


def _string(payload: dict[str, object], field: str, index: int) -> str:
    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        raise SearchatExportError(
            f"Searchat export record {index}: {field} must be a non-empty string"
        )
    return value


def _optional_string(payload: dict[str, object], field: str, index: int) -> str | None:
    value = payload[field]
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SearchatExportError(
            f"Searchat export record {index}: {field} must be a string or null"
        )
    return value


def _integer(payload: dict[str, object], field: str, index: int) -> int:
    value = payload[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise SearchatExportError(f"Searchat export record {index}: {field} must be an integer")
    return value


def _boolean(payload: dict[str, object], field: str, index: int) -> bool:
    value = payload[field]
    if not isinstance(value, bool):
        raise SearchatExportError(f"Searchat export record {index}: {field} must be a boolean")
    return value


def _number(payload: dict[str, object], field: str, index: int) -> float:
    value = payload[field]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SearchatExportError(f"Searchat export record {index}: {field} must be a number")
    try:
        parsed = float(value)
    except OverflowError as error:
        raise SearchatExportError(
            f"Searchat export record {index}: {field} is outside the float range"
        ) from error
    if not math.isfinite(parsed):
        raise SearchatExportError(f"Searchat export record {index}: {field} must be finite")
    return parsed


def _disposition(payload: dict[str, object], index: int) -> EligibilityDisposition:
    value = payload["eligibility_disposition"]
    if value not in _DISPOSITIONS:
        raise SearchatExportError(
            f"Searchat export record {index}: invalid eligibility_disposition {value!r}"
        )
    return cast(EligibilityDisposition, value)
