"""Aggregate-only opportunity report, privacy gate, and binding M22 decision."""

from __future__ import annotations

import json
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from laconic.codec.encoders.fallback import FallbackEncoder
from laconic.ledger import Ledger
from laconic.mcp_census.loaders import EXCLUSIONS, HostResult, iter_results
from laconic.mcp_census.manifest import (
    CENSUS_ID,
    MIN_CHARACTER_SHARE_PCT,
    MIN_CHARACTERS,
    MIN_EMISSION_PCT,
    MIN_REDUCTION_PCT,
    MIN_RESULTS,
    MIN_SESSIONS,
    PUBLIC_KEYS,
    CensusManifest,
    canonical_json,
    current_inventory_matches,
)
from laconic.runtime.engine import recovery_envelope

REPORT_SCHEMA_VERSION: Final = 1
PREDICATES: Final = (
    "evidence_floor",
    "materiality",
    "emission",
    "aggregate_reduction",
    "recoverability",
    "validity",
)
LIMITATIONS: Final = (
    "offline_fallback_probe_not_runtime_support",
    "character_reduction_not_token_or_cost_savings",
    "local_private_population_not_generalizable",
)
GO_DISPOSITION: Final = "GO — M22 MCP SUPPORT: authorized for design and implementation"
HOLD_PREFIX: Final = "HOLD — M22 MCP SUPPORT: failed="
POPULATION_KEYS: Final = frozenset(
    {
        "source_files",
        "sessions_seen",
        "candidate_results",
        "candidate_result_chars",
        "mcp_results",
        "eligible_mcp_results",
        "eligible_mcp_chars",
        "distinct_mcp_sessions",
        "exclusions",
        "by_host",
    }
)
PUBLIC_POPULATION_KEYS: Final = frozenset(
    {
        "eligible_mcp_results",
        "distinct_mcp_sessions",
        "mcp_candidate_chars",
        "candidate_result_chars",
    }
)
HOST_KEYS: Final = frozenset(
    {"results", "candidate_results", "candidate_chars", "mcp_results", "eligible_mcp_results"}
)
PROBE_KEYS: Final = frozenset(
    {
        "attempted",
        "emitted",
        "pass_through",
        "raw_chars",
        "visible_chars",
        "characters_avoided",
        "emission_pct",
        "aggregate_reduction_pct",
        "recovery_mismatches",
        "pass_through_reasons",
        "mechanism_engaged",
    }
)
REPORT_KEYS: Final = frozenset(
    {
        "schema_version",
        "census_id",
        "manifest_sha256",
        "source_inventory_sha256",
        "inclusive_start",
        "exclusive_end",
        "population",
        "probe",
        "predicates",
        "disposition",
        "failed_predicates",
        "source_unchanged",
        "privacy_validated",
        "provider_calls",
        "limitations",
    }
)
PASS_THROUGH_KEYS: Final = frozenset({"not_smaller", "recovery_mismatch"})


class OpportunityPrivacyError(ValueError):
    """A serialized census artifact contains a field or value outside its allowlist."""


@dataclass(frozen=True, slots=True)
class OpportunityReport:
    payload: dict[str, Any]
    disposition: dict[str, Any]

    def to_json(self) -> str:
        return canonical_json(self.payload)

    def disposition_json(self) -> str:
        return canonical_json(self.disposition)


@dataclass(slots=True)
class _Population:
    sessions: set[tuple[str, str]]
    mcp_sessions: set[tuple[str, str]]
    candidate_results: int
    candidate_chars: int
    mcp_results: int
    eligible: list[HostResult]
    exclusions: Counter[str]
    by_host: dict[str, Counter[str]]


@dataclass(frozen=True, slots=True)
class _Probe:
    attempted: int
    emitted: int
    pass_through: int
    raw_chars: int
    visible_chars: int
    characters_avoided: int
    recovery_mismatches: int
    pass_through_reasons: dict[str, int]

    @property
    def emission_pct(self) -> float:
        return _pct(self.emitted, self.attempted)

    @property
    def aggregate_reduction_pct(self) -> float:
        return _pct(self.characters_avoided, self.raw_chars)

    @property
    def mechanism_engaged(self) -> bool:
        return self.attempted > 0 and self.emitted + self.pass_through == self.attempted


def _pct(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else round(numerator * 100.0 / denominator, 6)


def _collect(manifest: CensusManifest) -> _Population:
    population = _Population(
        sessions=set(),
        mcp_sessions=set(),
        candidate_results=0,
        candidate_chars=0,
        mcp_results=0,
        eligible=[],
        exclusions=Counter(),
        by_host=defaultdict(Counter),
    )
    for result in iter_results(manifest):
        host = population.by_host[result.host]
        host["results"] += 1
        if result.session_id is not None:
            population.sessions.add((result.host, result.session_id))
        if result.eligible:
            assert result.text is not None
            population.candidate_results += 1
            population.candidate_chars += len(result.text)
            host["candidate_results"] += 1
            host["candidate_chars"] += len(result.text)
        if not result.is_mcp:
            if result.tool_name is None:
                population.exclusions["unattributed"] += 1
            continue
        population.mcp_results += 1
        host["mcp_results"] += 1
        if not result.eligible:
            population.exclusions[result.exclusion or "unknown_shape"] += 1
            continue
        assert result.session_id is not None and result.text is not None
        population.eligible.append(result)
        population.mcp_sessions.add((result.host, result.session_id))
        host["eligible_mcp_results"] += 1
    return population


def _probe(population: _Population) -> _Probe:
    grouped: dict[tuple[str, str], list[HostResult]] = defaultdict(list)
    for result in population.eligible:
        assert result.session_id is not None
        grouped[(result.host, result.session_id)].append(result)
    emitted = raw_chars = visible_chars = recovery_mismatches = 0
    reasons: Counter[str] = Counter()
    reference_session = "c" * 36
    with tempfile.TemporaryDirectory(prefix="laconic-mcp-census-") as temporary:
        for ordinal, results in enumerate(grouped.values(), start=1):
            ledger = Ledger(Path(temporary) / f"session-{ordinal}.sqlite", f"session-{ordinal}")
            encoder = FallbackEncoder(ledger)
            try:
                for turn, result in enumerate(results, start=1):
                    assert result.text is not None
                    record = encoder.encode("mcp", result.text, {}, turn=turn)
                    reference = f"{reference_session}/{record.handle}"
                    envelope = recovery_envelope(reference, record.encoded)
                    raw_chars += len(result.text)
                    recovered = ledger.expand(record.handle)
                    if recovered != result.text:
                        recovery_mismatches += 1
                        visible_chars += len(result.text)
                        reasons["recovery_mismatch"] += 1
                    elif len(envelope) < len(result.text):
                        emitted += 1
                        visible_chars += len(envelope)
                    else:
                        visible_chars += len(result.text)
                        reasons["not_smaller"] += 1
            finally:
                ledger.close()
    attempted = len(population.eligible)
    return _Probe(
        attempted=attempted,
        emitted=emitted,
        pass_through=attempted - emitted,
        raw_chars=raw_chars,
        visible_chars=visible_chars,
        characters_avoided=raw_chars - visible_chars,
        recovery_mismatches=recovery_mismatches,
        pass_through_reasons={key: reasons[key] for key in sorted(PASS_THROUGH_KEYS)},
    )


def _predicate_values(
    population: _Population,
    probe: _Probe,
    *,
    source_unchanged: bool,
) -> dict[str, bool]:
    mcp_chars = sum(len(cast(str, result.text)) for result in population.eligible)
    material_share = _pct(mcp_chars, population.candidate_chars)
    return {
        "evidence_floor": len(population.eligible) >= MIN_RESULTS
        and len(population.mcp_sessions) >= MIN_SESSIONS,
        "materiality": material_share >= MIN_CHARACTER_SHARE_PCT or mcp_chars >= MIN_CHARACTERS,
        "emission": probe.emission_pct >= MIN_EMISSION_PCT,
        "aggregate_reduction": probe.aggregate_reduction_pct >= MIN_REDUCTION_PCT,
        "recoverability": probe.recovery_mismatches == 0,
        "validity": source_unchanged and probe.mechanism_engaged,
    }


def _population_payload(manifest: CensusManifest, population: _Population) -> dict[str, Any]:
    return {
        "source_files": len(manifest.files),
        "sessions_seen": len(population.sessions),
        "candidate_results": population.candidate_results,
        "candidate_result_chars": population.candidate_chars,
        "mcp_results": population.mcp_results,
        "eligible_mcp_results": len(population.eligible),
        "eligible_mcp_chars": sum(len(cast(str, result.text)) for result in population.eligible),
        "distinct_mcp_sessions": len(population.mcp_sessions),
        "exclusions": {key: population.exclusions[key] for key in EXCLUSIONS},
        "by_host": {
            host: {key: population.by_host[host][key] for key in sorted(HOST_KEYS)}
            for host in ("claude_code", "omp")
        },
    }


def _probe_payload(probe: _Probe) -> dict[str, Any]:
    return {
        "attempted": probe.attempted,
        "emitted": probe.emitted,
        "pass_through": probe.pass_through,
        "raw_chars": probe.raw_chars,
        "visible_chars": probe.visible_chars,
        "characters_avoided": probe.characters_avoided,
        "emission_pct": probe.emission_pct,
        "aggregate_reduction_pct": probe.aggregate_reduction_pct,
        "recovery_mismatches": probe.recovery_mismatches,
        "pass_through_reasons": probe.pass_through_reasons,
        "mechanism_engaged": probe.mechanism_engaged,
    }


def build_report(manifest: CensusManifest) -> OpportunityReport:
    population = _collect(manifest)
    probe = _probe(population)
    source_unchanged = current_inventory_matches(manifest)
    predicates = _predicate_values(population, probe, source_unchanged=source_unchanged)
    failed = [key for key in PREDICATES if not predicates[key]]
    disposition = GO_DISPOSITION if not failed else HOLD_PREFIX + ",".join(failed)
    population_payload = _population_payload(manifest, population)
    payload: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "census_id": manifest.payload["census_id"],
        "manifest_sha256": manifest.sha256,
        "source_inventory_sha256": manifest.payload["inventory_sha256"],
        "inclusive_start": manifest.payload["inclusive_start"],
        "exclusive_end": manifest.payload["exclusive_end"],
        "population": population_payload,
        "probe": _probe_payload(probe),
        "predicates": predicates,
        "disposition": disposition,
        "failed_predicates": failed,
        "source_unchanged": source_unchanged,
        "privacy_validated": True,
        "provider_calls": 0,
        "limitations": list(LIMITATIONS),
    }
    public = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "census_id": payload["census_id"],
        "manifest_sha256": payload["manifest_sha256"],
        "source_inventory_sha256": payload["source_inventory_sha256"],
        "population": {
            "eligible_mcp_results": population_payload["eligible_mcp_results"],
            "distinct_mcp_sessions": population_payload["distinct_mcp_sessions"],
            "mcp_candidate_chars": population_payload["eligible_mcp_chars"],
            "candidate_result_chars": population_payload["candidate_result_chars"],
        },
        "predicates": predicates,
        "disposition": disposition,
        "failed_predicates": failed,
        "provider_calls": 0,
        "limitations": list(LIMITATIONS),
    }
    validate_report(payload)
    validate_disposition(public)
    return OpportunityReport(payload=payload, disposition=public)


def _exact_dict(value: object, allowed: frozenset[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != allowed:
        raise OpportunityPrivacyError(f"{field} must contain exactly {sorted(allowed)}")
    return cast(dict[str, Any], value)


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OpportunityPrivacyError(f"{field} must be a non-negative integer")
    return value


def _percentage(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise OpportunityPrivacyError(f"{field} must be numeric")
    result = float(value)
    if not 0.0 <= result <= 100.0:
        raise OpportunityPrivacyError(f"{field} must be between 0 and 100")
    return result


def _validate_common(value: dict[str, Any]) -> None:
    if value["schema_version"] != REPORT_SCHEMA_VERSION:
        raise OpportunityPrivacyError("unsupported report schema")
    if value["census_id"] != CENSUS_ID:
        raise OpportunityPrivacyError("unsupported census identity")
    if value["provider_calls"] != 0:
        raise OpportunityPrivacyError("provider_calls must be zero")
    for key in ("census_id", "manifest_sha256", "source_inventory_sha256", "disposition"):
        if not isinstance(value[key], str) or not value[key]:
            raise OpportunityPrivacyError(f"{key} must be a non-empty string")
    for key in ("manifest_sha256", "source_inventory_sha256"):
        digest = value[key]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise OpportunityPrivacyError(f"{key} must be lowercase SHA-256")
    predicates = _exact_dict(value["predicates"], frozenset(PREDICATES), "predicates")
    if not all(isinstance(predicates[key], bool) for key in PREDICATES):
        raise OpportunityPrivacyError("predicates must be booleans")
    failed = value["failed_predicates"]
    expected = [key for key in PREDICATES if not predicates[key]]
    if failed != expected:
        raise OpportunityPrivacyError("failed_predicates disagrees with predicates")
    expected_disposition = GO_DISPOSITION if not expected else HOLD_PREFIX + ",".join(expected)
    if value["disposition"] != expected_disposition:
        raise OpportunityPrivacyError("disposition disagrees with predicates")
    if value["limitations"] != list(LIMITATIONS):
        raise OpportunityPrivacyError("limitations changed or disappeared")


def validate_disposition(value: object) -> None:
    public = _exact_dict(value, frozenset(PUBLIC_KEYS), "public disposition")
    _validate_common(public)
    population = _exact_dict(public["population"], PUBLIC_POPULATION_KEYS, "population")
    for key in PUBLIC_POPULATION_KEYS:
        _nonnegative_int(population[key], f"population.{key}")


def validate_report(value: object) -> None:
    report = _exact_dict(value, REPORT_KEYS, "opportunity report")
    _validate_common(report)
    for key in ("inclusive_start", "exclusive_end"):
        if not isinstance(report[key], str) or not report[key].endswith("Z"):
            raise OpportunityPrivacyError(f"{key} must be UTC")
    if report["privacy_validated"] is not True:
        raise OpportunityPrivacyError("privacy_validated must be true")
    if not isinstance(report["source_unchanged"], bool):
        raise OpportunityPrivacyError("source_unchanged must be boolean")
    population = _exact_dict(report["population"], POPULATION_KEYS, "population")
    for key in POPULATION_KEYS - {"exclusions", "by_host"}:
        _nonnegative_int(population[key], f"population.{key}")
    exclusions = _exact_dict(population["exclusions"], frozenset(EXCLUSIONS), "exclusions")
    for key in EXCLUSIONS:
        _nonnegative_int(exclusions[key], f"exclusions.{key}")
    hosts = _exact_dict(population["by_host"], frozenset({"omp", "claude_code"}), "by_host")
    for host in hosts:
        counters = _exact_dict(hosts[host], HOST_KEYS, f"by_host.{host}")
        for key in HOST_KEYS:
            _nonnegative_int(counters[key], f"by_host.{host}.{key}")
    probe = _exact_dict(report["probe"], PROBE_KEYS, "probe")
    for key in PROBE_KEYS - {
        "emission_pct",
        "aggregate_reduction_pct",
        "pass_through_reasons",
        "mechanism_engaged",
    }:
        _nonnegative_int(probe[key], f"probe.{key}")
    _percentage(probe["emission_pct"], "probe.emission_pct")
    _percentage(probe["aggregate_reduction_pct"], "probe.aggregate_reduction_pct")
    if not isinstance(probe["mechanism_engaged"], bool):
        raise OpportunityPrivacyError("probe.mechanism_engaged must be boolean")
    reasons = _exact_dict(probe["pass_through_reasons"], PASS_THROUGH_KEYS, "pass_through_reasons")
    for key in PASS_THROUGH_KEYS:
        _nonnegative_int(reasons[key], f"pass_through_reasons.{key}")


def load_report(path: Path) -> OpportunityReport:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OpportunityPrivacyError(f"cannot read opportunity report: {path}") from error
    validate_report(payload)
    report = cast(dict[str, Any], payload)
    public = {key: report[key] for key in PUBLIC_KEYS if key not in {"population"}}
    population = report["population"]
    public["population"] = {
        "eligible_mcp_results": population["eligible_mcp_results"],
        "distinct_mcp_sessions": population["distinct_mcp_sessions"],
        "mcp_candidate_chars": population["eligible_mcp_chars"],
        "candidate_result_chars": population["candidate_result_chars"],
    }
    validate_disposition(public)
    return OpportunityReport(payload=report, disposition=public)
