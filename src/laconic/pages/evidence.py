"""Exact-key public evidence for the static Laconic product site.

This contract is intentionally separate from :mod:`laconic.spend.report`.  The
private spend report carries per-session hashes and model identifiers that are
useful locally but are not part of the public Pages surface.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Final

from laconic.spend.join import Composition, codec_activity
from laconic.spend.report import (
    ESTIMATE_BASIS,
    FALLBACK_SHARE_QUOTABLE_MAX_PCT,
    build_report,
)

PAGES_SCHEMA_VERSION: Final = 1
COHORT: Final = "laconic_repository_development"
GENERATOR_VERSION: Final = "m23-pages-v1"
DENOMINATOR_KIND: Final = "matched_sessions_modelled_cost"
ESTIMATE_AVAILABLE: Final = "available"
ESTIMATE_WITHHELD: Final = "withheld"
WITHHELD_FALLBACK: Final = "fallback_price_share_above_25_percent"
WITHHELD_INPUTS: Final = "insufficient_model_inputs"

LIMITATIONS: Final = (
    "observed_characters_are_not_tokens_or_dollars",
    "single_arm_cohort_has_no_counterfactual",
    "modelled_avoided_cost_is_not_measured_savings",
    "token_density_and_cache_rereads_are_unmeasured_assumptions",
    "host_reported_cost_covers_only_hosts_that_report_one",
    "fallback_pricing_withholds_dollars_above_25_percent",
    "local_laconic_repository_cohort_is_not_generalizable",
)

_TOP_LEVEL_KEYS: Final = frozenset(
    {
        "schema_version",
        "snapshot_id",
        "cohort",
        "inclusive_start",
        "exclusive_end",
        "laconic_version",
        "generator_version",
        "generator_commit",
        "manifest_sha256",
        "source_inventory_sha256",
        "population",
        "observed",
        "estimate",
        "limitations",
        "provider_calls",
    }
)
_POPULATION_KEYS: Final = frozenset(
    {"selected_sessions", "root_sessions", "nested_sessions", "priced_sessions", "joined_sessions"}
)
_OBSERVED_KEYS: Final = frozenset(
    {
        "eligible",
        "emitted",
        "pass_through",
        "raw_chars",
        "visible_chars",
        "chars_avoided",
        "full_expansions",
        "span_expansions",
    }
)
_DENOMINATOR_KEYS: Final = frozenset(
    {
        "kind",
        "modelled_usd",
        "modelled_sessions",
        "host_reported_usd",
        "host_reported_sessions",
    }
)
_ESTIMATE_COMMON_KEYS: Final = frozenset(
    {"basis", "status", "denominator", "fallback_priced_cost_share_pct"}
)
_ESTIMATE_AVAILABLE_KEYS: Final = _ESTIMATE_COMMON_KEYS | frozenset(
    {
        "chars_per_token_low",
        "chars_per_token_high",
        "cache_reread_multiplier",
        "reread_credited_low",
        "reread_credited_high",
        "avoided_cost_usd_low",
        "avoided_cost_usd_high",
        "avoided_share_pct_low",
        "avoided_share_pct_high",
    }
)
_ESTIMATE_WITHHELD_KEYS: Final = _ESTIMATE_COMMON_KEYS | frozenset({"reason"})
_HEX_40 = re.compile(r"[0-9a-f]{40}")
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_SNAPSHOT_ID = re.compile(r"laconic-development-[0-9]{8}T[0-9]{6}Z")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


class PagesEvidenceError(ValueError):
    """Raised when public Pages evidence violates its closed contract."""


def _exact_object(name: str, value: Any, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise PagesEvidenceError(f"{name} must contain exactly {sorted(keys)}")
    return value


def _non_negative_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PagesEvidenceError(f"{name} must be a non-negative integer")
    return value


def _non_negative_float(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PagesEvidenceError(f"{name} must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise PagesEvidenceError(f"{name} must be a finite non-negative number")
    return result


def _bounded_percentage(name: str, value: Any) -> float:
    result = _non_negative_float(name, value)
    if result > 100:
        raise PagesEvidenceError(f"{name} must be at most 100")
    return result


def _closed_string(name: str, value: Any, expected: str) -> str:
    if value != expected:
        raise PagesEvidenceError(f"{name} must equal {expected!r}")
    return expected


def _pattern(name: str, value: Any, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise PagesEvidenceError(f"{name} has an invalid format")
    return value


def _validate_population(value: Any) -> None:
    population = _exact_object("population", value, _POPULATION_KEYS)
    counts = {key: _non_negative_int(f"population.{key}", population[key]) for key in population}
    if counts["root_sessions"] + counts["nested_sessions"] != counts["selected_sessions"]:
        raise PagesEvidenceError("population root and nested counts do not equal selected sessions")
    if counts["priced_sessions"] > counts["selected_sessions"]:
        raise PagesEvidenceError("priced sessions exceed selected sessions")
    if counts["joined_sessions"] > counts["selected_sessions"]:
        raise PagesEvidenceError("joined sessions exceed selected sessions")
    if counts["joined_sessions"] == 0:
        raise PagesEvidenceError("public evidence requires joined runtime decisions")


def _validate_observed(value: Any) -> None:
    observed = _exact_object("observed", value, _OBSERVED_KEYS)
    counts = {key: _non_negative_int(f"observed.{key}", observed[key]) for key in observed}
    if counts["emitted"] > counts["eligible"]:
        raise PagesEvidenceError("emitted decisions exceed eligible decisions")
    if counts["pass_through"] != counts["eligible"] - counts["emitted"]:
        raise PagesEvidenceError("pass-through arithmetic is inconsistent")
    if counts["visible_chars"] > counts["raw_chars"]:
        raise PagesEvidenceError("visible characters exceed raw characters")
    if counts["chars_avoided"] != counts["raw_chars"] - counts["visible_chars"]:
        raise PagesEvidenceError("avoided-character arithmetic is inconsistent")


def _validate_denominator(value: Any) -> None:
    denominator = _exact_object("estimate.denominator", value, _DENOMINATOR_KEYS)
    _closed_string("estimate.denominator.kind", denominator["kind"], DENOMINATOR_KIND)
    _non_negative_float("estimate.denominator.modelled_usd", denominator["modelled_usd"])
    modelled_sessions = _non_negative_int(
        "estimate.denominator.modelled_sessions", denominator["modelled_sessions"]
    )
    _non_negative_float("estimate.denominator.host_reported_usd", denominator["host_reported_usd"])
    host_sessions = _non_negative_int(
        "estimate.denominator.host_reported_sessions", denominator["host_reported_sessions"]
    )
    if host_sessions > modelled_sessions:
        raise PagesEvidenceError("host-reported sessions exceed modelled sessions")


def _validate_estimate(value: Any) -> None:
    if not isinstance(value, dict):
        raise PagesEvidenceError("estimate must be an object")
    status = value.get("status")
    keys = _ESTIMATE_AVAILABLE_KEYS if status == ESTIMATE_AVAILABLE else _ESTIMATE_WITHHELD_KEYS
    estimate = _exact_object("estimate", value, keys)
    _closed_string("estimate.basis", estimate["basis"], ESTIMATE_BASIS)
    _validate_denominator(estimate["denominator"])
    fallback_share = _bounded_percentage(
        "estimate.fallback_priced_cost_share_pct",
        estimate["fallback_priced_cost_share_pct"],
    )
    if status == ESTIMATE_WITHHELD:
        reason = estimate["reason"]
        if reason not in {WITHHELD_FALLBACK, WITHHELD_INPUTS}:
            raise PagesEvidenceError("estimate withholding reason is not approved")
        if reason == WITHHELD_FALLBACK and fallback_share <= FALLBACK_SHARE_QUOTABLE_MAX_PCT:
            raise PagesEvidenceError("fallback withholding requires a share above the gate")
        return
    _closed_string("estimate.status", status, ESTIMATE_AVAILABLE)
    if fallback_share > FALLBACK_SHARE_QUOTABLE_MAX_PCT:
        raise PagesEvidenceError("available estimate exceeds the fallback-price gate")
    low_chars = _non_negative_float("estimate.chars_per_token_low", estimate["chars_per_token_low"])
    high_chars = _non_negative_float(
        "estimate.chars_per_token_high", estimate["chars_per_token_high"]
    )
    low_cost = _non_negative_float(
        "estimate.avoided_cost_usd_low", estimate["avoided_cost_usd_low"]
    )
    high_cost = _non_negative_float(
        "estimate.avoided_cost_usd_high", estimate["avoided_cost_usd_high"]
    )
    low_share = _non_negative_float(
        "estimate.avoided_share_pct_low", estimate["avoided_share_pct_low"]
    )
    high_share = _non_negative_float(
        "estimate.avoided_share_pct_high", estimate["avoided_share_pct_high"]
    )
    reread_low = _non_negative_float(
        "estimate.reread_credited_low", estimate["reread_credited_low"]
    )
    reread_high = _non_negative_float(
        "estimate.reread_credited_high", estimate["reread_credited_high"]
    )
    _non_negative_float("estimate.cache_reread_multiplier", estimate["cache_reread_multiplier"])
    if (
        low_chars > high_chars
        or low_cost > high_cost
        or low_share > high_share
        or reread_low > reread_high
    ):
        raise PagesEvidenceError("estimate band is inverted")


def validate_pages_evidence(payload: Any) -> None:
    """Raise unless ``payload`` is the complete, privacy-safe Pages schema."""

    document = _exact_object("pages evidence", payload, _TOP_LEVEL_KEYS)
    if document["schema_version"] != PAGES_SCHEMA_VERSION:
        raise PagesEvidenceError("unsupported Pages evidence schema")
    _pattern("snapshot_id", document["snapshot_id"], _SNAPSHOT_ID)
    _closed_string("cohort", document["cohort"], COHORT)
    _pattern("inclusive_start", document["inclusive_start"], _TIMESTAMP)
    _pattern("exclusive_end", document["exclusive_end"], _TIMESTAMP)
    _pattern("laconic_version", document["laconic_version"], _VERSION)
    _closed_string("generator_version", document["generator_version"], GENERATOR_VERSION)
    _pattern("generator_commit", document["generator_commit"], _HEX_40)
    _pattern("manifest_sha256", document["manifest_sha256"], _HEX_64)
    _pattern("source_inventory_sha256", document["source_inventory_sha256"], _HEX_64)
    _validate_population(document["population"])
    _validate_observed(document["observed"])
    _validate_estimate(document["estimate"])
    if document["limitations"] != list(LIMITATIONS):
        raise PagesEvidenceError("limitations must equal the complete ordered vocabulary")
    if document["provider_calls"] != 0:
        raise PagesEvidenceError("provider_calls must equal zero")


@dataclass(frozen=True, slots=True)
class PagesEvidence:
    """One validated public snapshot and its immutable canonical serialization."""

    _payload: dict[str, Any]

    def __post_init__(self) -> None:
        validate_pages_evidence(self._payload)
        normalized = json.loads(json.dumps(self._payload, sort_keys=True, allow_nan=False))
        validate_pages_evidence(normalized)
        object.__setattr__(self, "_payload", normalized)

    @property
    def payload(self) -> dict[str, Any]:
        """Return a detached copy so validation cannot be invalidated later."""

        loaded: Any = json.loads(self.to_json())
        if not isinstance(loaded, dict):
            raise PagesEvidenceError("canonical Pages evidence stopped being an object")
        return loaded

    def to_json(self) -> str:
        return json.dumps(self._payload, indent=2, sort_keys=True, allow_nan=False) + "\n"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


def build_pages_evidence(
    composition: Composition,
    *,
    snapshot_id: str,
    inclusive_start: str,
    exclusive_end: str,
    laconic_version: str,
    generator_commit: str,
    manifest_sha256: str,
    source_inventory_sha256: str,
) -> PagesEvidence:
    """Project selected private aggregates into the closed public contract."""

    report = build_report(composition).payload
    activity = codec_activity(composition)
    matched = composition.matched
    estimate_source = report["estimate"]
    fallback_share = round(100.0 * composition.fallback_priced_cost_share(matched), 6)
    denominator = {
        "kind": DENOMINATOR_KIND,
        "modelled_usd": float(report["matched_cost"]["total"]),
        "modelled_sessions": len(matched),
        "host_reported_usd": float(report["matched_host_cost_usd"]),
        "host_reported_sessions": sum(
            1 for session in matched if session.turns and session.reports_host_cost
        ),
    }
    if estimate_source is None:
        estimate: dict[str, Any] = {
            "basis": ESTIMATE_BASIS,
            "status": ESTIMATE_WITHHELD,
            "reason": WITHHELD_INPUTS,
            "denominator": denominator,
            "fallback_priced_cost_share_pct": round(fallback_share, 6),
        }
    elif fallback_share > FALLBACK_SHARE_QUOTABLE_MAX_PCT:
        estimate = {
            "basis": ESTIMATE_BASIS,
            "status": ESTIMATE_WITHHELD,
            "reason": WITHHELD_FALLBACK,
            "denominator": denominator,
            "fallback_priced_cost_share_pct": round(fallback_share, 6),
        }
    else:
        estimate = {
            "basis": ESTIMATE_BASIS,
            "status": ESTIMATE_AVAILABLE,
            "denominator": denominator,
            "fallback_priced_cost_share_pct": round(fallback_share, 6),
            "chars_per_token_low": estimate_source["chars_per_token_low"],
            "chars_per_token_high": estimate_source["chars_per_token_high"],
            "cache_reread_multiplier": estimate_source["cache_reread_multiplier"],
            "reread_credited_low": estimate_source["reread_credited_low"],
            "reread_credited_high": estimate_source["reread_credited_high"],
            "avoided_cost_usd_low": estimate_source["avoided_cost_usd_low"],
            "avoided_cost_usd_high": estimate_source["avoided_cost_usd_high"],
            "avoided_share_pct_low": estimate_source["avoided_share_pct_low"],
            "avoided_share_pct_high": estimate_source["avoided_share_pct_high"],
        }
    sessions = composition.sessions
    payload: dict[str, Any] = {
        "schema_version": PAGES_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "cohort": COHORT,
        "inclusive_start": inclusive_start,
        "exclusive_end": exclusive_end,
        "laconic_version": laconic_version,
        "generator_version": GENERATOR_VERSION,
        "generator_commit": generator_commit,
        "manifest_sha256": manifest_sha256,
        "source_inventory_sha256": source_inventory_sha256,
        "population": {
            "selected_sessions": len(sessions),
            "root_sessions": sum(1 for session in sessions if not session.nested),
            "nested_sessions": sum(1 for session in sessions if session.nested),
            "priced_sessions": len(composition.priced),
            "joined_sessions": len(matched),
        },
        "observed": {
            "eligible": activity.eligible,
            "emitted": activity.emitted,
            "pass_through": activity.pass_through,
            "raw_chars": activity.raw_chars,
            "visible_chars": activity.visible_chars,
            "chars_avoided": activity.chars_avoided,
            "full_expansions": activity.full_expansions,
            "span_expansions": activity.span_expansions,
        },
        "estimate": estimate,
        "limitations": list(LIMITATIONS),
        "provider_calls": 0,
    }
    return PagesEvidence(payload)
