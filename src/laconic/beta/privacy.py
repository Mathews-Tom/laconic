"""Defense-in-depth privacy validator for M18 receipts and reports.

Mirrors `laconic.observe.privacy`: the dataclasses in :mod:`laconic.beta.receipt`
and :mod:`laconic.beta.report` already limit what can be constructed, but a
field added later without updating this allowlist should fail loudly rather
than silently start persisting an unreviewed key. This module inspects only
the exact key set, each field's own type/enum membership, and hash-shaped
strings -- never receipt or report content, since neither carries any.
"""

from __future__ import annotations

from typing import Any

from laconic.beta.receipt import ALLOWED_RECEIPT_KEYS, KNOWN_REASONS
from laconic.beta.report import ALLOWED_REPORT_KEYS, Verdict, VerdictReason
from laconic.beta.scenarios import ALL_SCENARIOS

#: A lowercase SHA-256 hex digest: exactly 64 ``[0-9a-f]`` characters. Every
#: hash field this package serializes (manifest/schema/wheel/repository)
#: must look like this -- never a path, a session id, or free text.
_HEX_64 = frozenset("0123456789abcdef")

#: Report keys this module type-checks individually, grouped by shape. Every
#: allowlisted key not named here must be a plain integer, so a field added
#: to :class:`~laconic.beta.report.AggregateReport` is type-checked the
#: moment it enters the allowlist rather than silently escaping this gate.
_REPORT_HASH_KEYS = frozenset({"manifest_hash", "schema_hash", "candidate_wheel_sha256"})
_REPORT_SCENARIO_LIST_KEYS = frozenset(
    {
        "pre_signoff_scenarios_covered",
        "pre_signoff_scenarios_missing",
        "post_signoff_scenarios_covered",
        "post_signoff_scenarios_missing",
    }
)
_REPORT_NUMBER_KEYS = frozenset({"generated_at"})
_REPORT_OPTIONAL_NUMBER_KEYS = frozenset(
    {"observed_reduction_pct", "latency_p50_ms", "latency_p95_ms"}
)
_REPORT_OTHER_KEYS = frozenset(
    {
        "eligible_omp_version",
        "verdict",
        "verdict_reasons",
        "pass_through_totals",
        "savings_gate_applies",
    }
)
_REPORT_INT_KEYS = ALLOWED_REPORT_KEYS - (
    _REPORT_HASH_KEYS
    | _REPORT_SCENARIO_LIST_KEYS
    | _REPORT_NUMBER_KEYS
    | _REPORT_OPTIONAL_NUMBER_KEYS
    | _REPORT_OTHER_KEYS
)


class PrivacyViolationError(ValueError):
    """Raised when serialized M18 evidence carries a key or value this
    module cannot certify as content-free."""


def _require_hex64(field: str, value: Any) -> None:
    if not isinstance(value, str) or len(value) != 64 or set(value) - _HEX_64:
        raise PrivacyViolationError(f"{field} must be a 64-character lowercase hex digest")


def validate_receipt_json(payload: dict[str, Any]) -> None:
    """Raise :class:`PrivacyViolationError` unless ``payload`` has exactly
    :data:`laconic.beta.receipt.ALLOWED_RECEIPT_KEYS` and every hash/enum
    field is a real, known, content-free value."""
    if not isinstance(payload, dict):
        raise PrivacyViolationError("receipt must be a JSON object")
    extra_keys = set(payload) - ALLOWED_RECEIPT_KEYS
    if extra_keys:
        raise PrivacyViolationError(f"unallowlisted receipt key(s): {sorted(extra_keys)}")
    missing_keys = ALLOWED_RECEIPT_KEYS - set(payload)
    if missing_keys:
        raise PrivacyViolationError(f"missing receipt key(s): {sorted(missing_keys)}")

    _require_hex64("schema_hash", payload["schema_hash"])
    _require_hex64("manifest_hash", payload["manifest_hash"])
    _require_hex64("candidate_wheel_sha256", payload["candidate_wheel_sha256"])
    _require_hex64("repository_id", payload["repository_id"])

    scenarios = payload["scenarios"]
    if not isinstance(scenarios, list) or not all(isinstance(item, str) for item in scenarios):
        raise PrivacyViolationError("scenarios must be a list of strings")
    unknown_scenarios = set(scenarios) - ALL_SCENARIOS
    if unknown_scenarios:
        raise PrivacyViolationError(
            f"scenarios names {len(unknown_scenarios)} value(s) outside the closed M18 vocabulary"
        )

    pass_through_counts = payload["pass_through_counts"]
    if not isinstance(pass_through_counts, list):
        raise PrivacyViolationError("pass_through_counts must be a list")
    for index, entry in enumerate(pass_through_counts):
        if not isinstance(entry, list) or len(entry) != 2 or not isinstance(entry[0], str):
            raise PrivacyViolationError(f"malformed pass_through_counts entry at index {index}")
        if entry[0] not in KNOWN_REASONS:
            raise PrivacyViolationError(
                f"pass_through_counts entry at index {index} is not a known ledger reason"
            )

    if not isinstance(payload["clean_shutdown"], bool):
        raise PrivacyViolationError("clean_shutdown must be a bool")
    if isinstance(payload["slot"], bool) or not isinstance(payload["slot"], int):
        raise PrivacyViolationError("slot must be an int")
    if payload["slot"] < 1:
        raise PrivacyViolationError("slot must be a positive 1-based ordinal")


def validate_report_json(payload: dict[str, Any]) -> None:
    """Raise :class:`PrivacyViolationError` unless ``payload`` has exactly
    :data:`laconic.beta.report.ALLOWED_REPORT_KEYS` and every enum field is a
    real, known member."""
    if not isinstance(payload, dict):
        raise PrivacyViolationError("report must be a JSON object")
    extra_keys = set(payload) - ALLOWED_REPORT_KEYS
    if extra_keys:
        raise PrivacyViolationError(f"unallowlisted report key(s): {sorted(extra_keys)}")
    missing_keys = ALLOWED_REPORT_KEYS - set(payload)
    if missing_keys:
        raise PrivacyViolationError(f"missing report key(s): {sorted(missing_keys)}")

    _require_hex64("manifest_hash", payload["manifest_hash"])
    _require_hex64("schema_hash", payload["schema_hash"])
    _require_hex64("candidate_wheel_sha256", payload["candidate_wheel_sha256"])

    if not isinstance(payload["eligible_omp_version"], str) or not payload["eligible_omp_version"]:
        raise PrivacyViolationError("eligible_omp_version must be a non-empty string")
    for key in sorted(_REPORT_INT_KEYS):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise PrivacyViolationError(f"{key} must be an int")
    for key in ("min_sessions", "min_repositories", "min_eligible_observations"):
        if payload[key] < 1:
            raise PrivacyViolationError(f"{key} must be a positive int")
    for key in sorted(_REPORT_NUMBER_KEYS):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise PrivacyViolationError(f"{key} must be a number")
    for key in sorted(_REPORT_OPTIONAL_NUMBER_KEYS):
        value = payload[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int | float)):
            raise PrivacyViolationError(f"{key} must be a number or null")

    if payload["verdict"] not in {member.value for member in Verdict}:
        raise PrivacyViolationError("verdict is not a known verdict")

    for key in (
        "pre_signoff_scenarios_covered",
        "pre_signoff_scenarios_missing",
        "post_signoff_scenarios_covered",
        "post_signoff_scenarios_missing",
    ):
        values = payload[key]
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise PrivacyViolationError(f"{key} must be a list of strings")
        unknown = set(values) - ALL_SCENARIOS
        if unknown:
            raise PrivacyViolationError(f"{key} names {len(unknown)} unknown scenario(s)")

    pass_through_totals = payload["pass_through_totals"]
    if not isinstance(pass_through_totals, list):
        raise PrivacyViolationError("pass_through_totals must be a list")
    for index, entry in enumerate(pass_through_totals):
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not isinstance(entry[0], str)
            or isinstance(entry[1], bool)
            or not isinstance(entry[1], int)
        ):
            raise PrivacyViolationError(f"malformed pass_through_totals entry at index {index}")
        if entry[0] not in KNOWN_REASONS:
            raise PrivacyViolationError(
                f"pass_through_totals entry at index {index} is not a known ledger reason"
            )

    verdict_reasons = payload["verdict_reasons"]
    if not isinstance(verdict_reasons, list) or not all(
        isinstance(item, str) for item in verdict_reasons
    ):
        raise PrivacyViolationError("verdict_reasons must be a list of strings")
    known_verdict_reasons = {member.value for member in VerdictReason}
    unknown_reasons = set(verdict_reasons) - known_verdict_reasons
    if unknown_reasons:
        raise PrivacyViolationError(
            f"verdict_reasons names {len(unknown_reasons)} value(s) outside the closed "
            "reason vocabulary"
        )

    if not isinstance(payload["savings_gate_applies"], bool):
        raise PrivacyViolationError("savings_gate_applies must be a bool")
    if payload["savings_gate_applies"] is not False:
        raise PrivacyViolationError(
            "savings_gate_applies must be False: no minimum savings threshold "
            "may gate the M18 verdict"
        )
