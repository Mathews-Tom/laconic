"""The deterministic M18 aggregate campaign report.

Consumes only already-derived :class:`~laconic.beta.receipt.SessionReceipt`
evidence and the frozen :class:`~laconic.beta.manifest.CampaignManifest` --
never a raw ledger or raw tool content. :func:`validate_evidence_set` is the
single gate every receipt set must clear before a report may be generated:
it rejects an empty, partial, duplicate, stale, mutated, or privacy-invalid
set outright rather than rendering a report from evidence that cannot be
trusted. `.docs/DEVELOPMENT_PLAN.md` §6 M18 requires no minimum aggregate
savings percentage control the verdict; :data:`AggregateReport.savings_gate_applies`
is always ``False`` so that rule is a checkable fact, not a comment.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from laconic.beta.manifest import CampaignManifest, fingerprint_manifest
from laconic.beta.receipt import (
    ReceiptFormatError,
    SessionReceipt,
    receipt_from_json,
    receipt_schema_fingerprint,
)
from laconic.beta.scenarios import POST_SIGNOFF_SCENARIOS, PRE_SIGNOFF_SCENARIOS

#: Bumped whenever a report field is added, removed, or reinterpreted.
REPORT_SCHEMA_VERSION = 1


class Verdict(StrEnum):
    """The three outcomes a report may reach.

    `.docs/DEVELOPMENT_PLAN.md` §6 M18 human review gate: post-signoff
    scenarios (real-ledger purge) may only be exercised after a human signs
    the gate, so a campaign that otherwise passes every safety and coverage
    criterion but has not yet exercised them reports
    :attr:`HUMAN_REVIEW_REQUIRED`, not :attr:`GO`.
    """

    GO = "go"
    NO_GO = "no_go"
    HUMAN_REVIEW_REQUIRED = "human_review_required"


class VerdictReason(StrEnum):
    """The closed set of reasons a report may cite for its verdict."""

    BELOW_SESSION_MINIMUM = "below_session_minimum"
    BELOW_REPOSITORY_MINIMUM = "below_repository_minimum"
    BELOW_OBSERVATION_MINIMUM = "below_observation_minimum"
    SAFETY_COUNTERS_NONZERO = "safety_counters_nonzero"
    PRE_SIGNOFF_SCENARIOS_INCOMPLETE = "pre_signoff_scenarios_incomplete"
    POST_SIGNOFF_SCENARIOS_INCOMPLETE = "post_signoff_scenarios_incomplete"


class EvidenceRejection(StrEnum):
    """The closed set of reasons a receipt set may be refused outright."""

    EMPTY = "empty"
    PARTIAL = "partial"
    DUPLICATE = "duplicate"
    STALE = "stale"
    MUTATED = "mutated"
    PRIVACY_INVALID = "privacy_invalid"


class EvidenceValidationError(ValueError):
    """Raised when a receipt set cannot be aggregated into a report."""

    def __init__(self, category: EvidenceRejection, message: str) -> None:
        super().__init__(f"{category.value}: {message}")
        self.category = category


#: The exact key set a serialized aggregate report may ever contain.
ALLOWED_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "manifest_hash",
        "schema_hash",
        "candidate_wheel_sha256",
        "eligible_omp_version",
        "min_sessions",
        "min_repositories",
        "min_eligible_observations",
        "generated_at",
        "sessions_total",
        "sessions_completed",
        "repositories_total",
        "decisions_total",
        "eligible_observations_total",
        "emitted_total",
        "pass_through_totals",
        "raw_chars_total",
        "visible_chars_total",
        "characters_avoided_total",
        "observed_reduction_pct",
        "full_expansions_total",
        "span_expansions_total",
        "latency_p50_ms",
        "latency_p95_ms",
        "exact_expansion_failures_total",
        "compressed_tool_errors_total",
        "oversized_envelopes_total",
        "observed_corruption_total",
        "pre_signoff_scenarios_covered",
        "pre_signoff_scenarios_missing",
        "post_signoff_scenarios_covered",
        "post_signoff_scenarios_missing",
        "verdict",
        "verdict_reasons",
        "savings_gate_applies",
    }
)


@dataclass(frozen=True, slots=True)
class AggregateReport:
    """The complete, content-free M18 campaign report."""

    schema_version: int
    manifest_hash: str
    schema_hash: str
    candidate_wheel_sha256: str
    eligible_omp_version: str
    min_sessions: int
    min_repositories: int
    min_eligible_observations: int
    generated_at: float
    sessions_total: int
    sessions_completed: int
    repositories_total: int
    decisions_total: int
    eligible_observations_total: int
    emitted_total: int
    pass_through_totals: tuple[tuple[str, int], ...]
    raw_chars_total: int
    visible_chars_total: int
    characters_avoided_total: int
    observed_reduction_pct: float | None
    full_expansions_total: int
    span_expansions_total: int
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    exact_expansion_failures_total: int
    compressed_tool_errors_total: int
    oversized_envelopes_total: int
    observed_corruption_total: int
    pre_signoff_scenarios_covered: tuple[str, ...]
    pre_signoff_scenarios_missing: tuple[str, ...]
    post_signoff_scenarios_covered: tuple[str, ...]
    post_signoff_scenarios_missing: tuple[str, ...]
    verdict: Verdict
    verdict_reasons: tuple[VerdictReason, ...]
    savings_gate_applies: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "manifest_hash": self.manifest_hash,
            "schema_hash": self.schema_hash,
            "candidate_wheel_sha256": self.candidate_wheel_sha256,
            "eligible_omp_version": self.eligible_omp_version,
            "min_sessions": self.min_sessions,
            "min_repositories": self.min_repositories,
            "min_eligible_observations": self.min_eligible_observations,
            "generated_at": self.generated_at,
            "sessions_total": self.sessions_total,
            "sessions_completed": self.sessions_completed,
            "repositories_total": self.repositories_total,
            "decisions_total": self.decisions_total,
            "eligible_observations_total": self.eligible_observations_total,
            "emitted_total": self.emitted_total,
            "pass_through_totals": [list(item) for item in self.pass_through_totals],
            "raw_chars_total": self.raw_chars_total,
            "visible_chars_total": self.visible_chars_total,
            "characters_avoided_total": self.characters_avoided_total,
            "observed_reduction_pct": self.observed_reduction_pct,
            "full_expansions_total": self.full_expansions_total,
            "span_expansions_total": self.span_expansions_total,
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "exact_expansion_failures_total": self.exact_expansion_failures_total,
            "compressed_tool_errors_total": self.compressed_tool_errors_total,
            "oversized_envelopes_total": self.oversized_envelopes_total,
            "observed_corruption_total": self.observed_corruption_total,
            "pre_signoff_scenarios_covered": list(self.pre_signoff_scenarios_covered),
            "pre_signoff_scenarios_missing": list(self.pre_signoff_scenarios_missing),
            "post_signoff_scenarios_covered": list(self.post_signoff_scenarios_covered),
            "post_signoff_scenarios_missing": list(self.post_signoff_scenarios_missing),
            "verdict": self.verdict.value,
            "verdict_reasons": [reason.value for reason in self.verdict_reasons],
            "savings_gate_applies": self.savings_gate_applies,
        }


def nearest_rank_percentile(values: Sequence[float], pct: float) -> float:
    """Return the nearest-rank ``pct``-th percentile of ``values``.

    Nearest-rank (not interpolated): sort ascending, take the value at
    ``ceil(pct/100 * n)`` (1-based), clamped to ``[1, n]``. `.docs`'s
    refocus design §10.5 requires p50/p95 latency be reported without
    hiding tail latency behind a mean; nearest-rank is the simplest method
    that always names one value actually observed in the sample.
    """
    if not values:
        raise ValueError("nearest_rank_percentile requires at least one value")
    if not (0 < pct <= 100):
        raise ValueError(f"pct must be in (0, 100]: {pct}")
    ordered = sorted(values)
    rank = max(1, min(math.ceil(pct / 100 * len(ordered)), len(ordered)))
    return ordered[rank - 1]


def validate_evidence_set(
    payloads: Sequence[dict[str, Any]], manifest: CampaignManifest
) -> tuple[SessionReceipt, ...]:
    """Validate a candidate receipt set, returning it typed and slot-ordered.

    Raises :class:`EvidenceValidationError` tagged with exactly one of
    :class:`EvidenceRejection`'s six categories for anything that cannot be
    aggregated: an empty set, a gap in the slot sequence (partial), a
    repeated slot (duplicate), a manifest/schema/candidate-wheel mismatch
    against this build's frozen contract (stale), an internally
    inconsistent receipt (mutated), or a key/enum privacy violation.
    """
    # Import locally to avoid a receipt/privacy import cycle: privacy.py
    # imports the allowlists this module and laconic.beta.receipt define.
    from laconic.beta.privacy import PrivacyViolationError, validate_receipt_json

    if not payloads:
        raise EvidenceValidationError(EvidenceRejection.EMPTY, "no receipts supplied")

    receipts: list[SessionReceipt] = []
    for index, payload in enumerate(payloads):
        try:
            validate_receipt_json(payload)
        except PrivacyViolationError as error:
            raise EvidenceValidationError(
                EvidenceRejection.PRIVACY_INVALID, f"receipt[{index}]: {error}"
            ) from error
        try:
            receipts.append(receipt_from_json(payload))
        except ReceiptFormatError as error:
            raise EvidenceValidationError(
                EvidenceRejection.MUTATED, f"receipt[{index}]: {error}"
            ) from error

    expected_manifest_hash = fingerprint_manifest(manifest)
    expected_schema_hash = receipt_schema_fingerprint()
    manifest_hashes = {receipt.manifest_hash for receipt in receipts}
    schema_hashes = {receipt.schema_hash for receipt in receipts}
    wheel_hashes = {receipt.candidate_wheel_sha256 for receipt in receipts}
    if manifest_hashes != {expected_manifest_hash}:
        raise EvidenceValidationError(
            EvidenceRejection.STALE,
            f"receipt manifest_hash does not match the frozen campaign manifest: "
            f"got {sorted(manifest_hashes)}, expected {expected_manifest_hash!r}",
        )
    if schema_hashes != {expected_schema_hash}:
        raise EvidenceValidationError(
            EvidenceRejection.STALE,
            f"receipt schema_hash does not match this build's receipt contract: "
            f"got {sorted(schema_hashes)}, expected {expected_schema_hash!r}",
        )
    if wheel_hashes != {manifest.candidate_wheel_sha256}:
        raise EvidenceValidationError(
            EvidenceRejection.STALE,
            f"receipt candidate_wheel_sha256 does not match the frozen campaign wheel: "
            f"got {sorted(wheel_hashes)}, expected {manifest.candidate_wheel_sha256!r}",
        )

    slots = [receipt.slot for receipt in receipts]
    if len(set(slots)) != len(slots):
        raise EvidenceValidationError(
            EvidenceRejection.DUPLICATE, f"duplicate slot(s) among {sorted(slots)}"
        )
    declared_slots = dict(manifest.slots)
    if set(slots) != set(declared_slots):
        raise EvidenceValidationError(
            EvidenceRejection.PARTIAL,
            f"slots must be exactly the frozen campaign's declared set "
            f"{sorted(declared_slots)}: got {sorted(slots)}",
        )
    for receipt in receipts:
        expected_repository_id = declared_slots[receipt.slot]
        if receipt.repository_id != expected_repository_id:
            raise EvidenceValidationError(
                EvidenceRejection.MUTATED,
                f"receipt for slot {receipt.slot} names repository_id "
                f"{receipt.repository_id!r}, but the frozen manifest binds slot "
                f"{receipt.slot} to {expected_repository_id!r}",
            )

    return tuple(sorted(receipts, key=lambda receipt: receipt.slot))


def generate_report(
    receipts: tuple[SessionReceipt, ...], manifest: CampaignManifest
) -> AggregateReport:
    """Aggregate an already-validated receipt set into one campaign report."""
    if not receipts:
        raise EvidenceValidationError(EvidenceRejection.EMPTY, "no receipts to aggregate")

    sessions_completed = sum(1 for receipt in receipts if receipt.clean_shutdown)
    repositories_total = len({receipt.repository_id for receipt in receipts})
    decisions_total = sum(receipt.decisions_total for receipt in receipts)
    eligible_observations_total = sum(receipt.eligible_observations for receipt in receipts)
    emitted_total = sum(receipt.emitted_count for receipt in receipts)

    pass_through_totals: Counter[str] = Counter()
    for receipt in receipts:
        for reason, count in receipt.pass_through_counts:
            pass_through_totals[reason] += count

    raw_chars_total = sum(receipt.raw_chars for receipt in receipts)
    visible_chars_total = sum(receipt.visible_chars for receipt in receipts)
    characters_avoided_total = raw_chars_total - visible_chars_total
    observed_reduction_pct = (
        round(characters_avoided_total / raw_chars_total * 100, 2) if raw_chars_total > 0 else None
    )

    latencies = [latency for receipt in receipts for latency in receipt.latencies_ms]
    latency_p50_ms = nearest_rank_percentile(latencies, 50) if latencies else None
    latency_p95_ms = nearest_rank_percentile(latencies, 95) if latencies else None

    exact_expansion_failures_total = sum(receipt.exact_expansion_failures for receipt in receipts)
    compressed_tool_errors_total = sum(receipt.compressed_tool_errors for receipt in receipts)
    oversized_envelopes_total = sum(receipt.oversized_envelopes for receipt in receipts)
    observed_corruption_total = sum(receipt.observed_corruption for receipt in receipts)
    safety_failures_total = (
        exact_expansion_failures_total
        + compressed_tool_errors_total
        + oversized_envelopes_total
        + observed_corruption_total
    )

    covered_scenarios: set[str] = set()
    for receipt in receipts:
        covered_scenarios.update(receipt.scenarios)
    pre_signoff_covered = tuple(sorted(covered_scenarios & PRE_SIGNOFF_SCENARIOS))
    pre_signoff_missing = tuple(sorted(PRE_SIGNOFF_SCENARIOS - covered_scenarios))
    post_signoff_covered = tuple(sorted(covered_scenarios & POST_SIGNOFF_SCENARIOS))
    post_signoff_missing = tuple(sorted(POST_SIGNOFF_SCENARIOS - covered_scenarios))

    reasons: list[VerdictReason] = []
    if sessions_completed < manifest.min_sessions:
        reasons.append(VerdictReason.BELOW_SESSION_MINIMUM)
    if repositories_total < manifest.min_repositories:
        reasons.append(VerdictReason.BELOW_REPOSITORY_MINIMUM)
    if eligible_observations_total < manifest.min_eligible_observations:
        reasons.append(VerdictReason.BELOW_OBSERVATION_MINIMUM)
    if safety_failures_total != 0:
        reasons.append(VerdictReason.SAFETY_COUNTERS_NONZERO)
    if pre_signoff_missing:
        reasons.append(VerdictReason.PRE_SIGNOFF_SCENARIOS_INCOMPLETE)

    if reasons:
        verdict = Verdict.NO_GO
    elif post_signoff_missing:
        verdict = Verdict.HUMAN_REVIEW_REQUIRED
        reasons.append(VerdictReason.POST_SIGNOFF_SCENARIOS_INCOMPLETE)
    else:
        verdict = Verdict.GO

    return AggregateReport(
        schema_version=REPORT_SCHEMA_VERSION,
        manifest_hash=fingerprint_manifest(manifest),
        schema_hash=receipt_schema_fingerprint(),
        candidate_wheel_sha256=manifest.candidate_wheel_sha256,
        eligible_omp_version=manifest.eligible_omp_version,
        min_sessions=manifest.min_sessions,
        min_repositories=manifest.min_repositories,
        min_eligible_observations=manifest.min_eligible_observations,
        generated_at=max(receipt.ended_at for receipt in receipts),
        sessions_total=len(receipts),
        sessions_completed=sessions_completed,
        repositories_total=repositories_total,
        decisions_total=decisions_total,
        eligible_observations_total=eligible_observations_total,
        emitted_total=emitted_total,
        pass_through_totals=tuple(sorted(pass_through_totals.items())),
        raw_chars_total=raw_chars_total,
        visible_chars_total=visible_chars_total,
        characters_avoided_total=characters_avoided_total,
        observed_reduction_pct=observed_reduction_pct,
        full_expansions_total=sum(receipt.full_expansions for receipt in receipts),
        span_expansions_total=sum(receipt.span_expansions for receipt in receipts),
        latency_p50_ms=latency_p50_ms,
        latency_p95_ms=latency_p95_ms,
        exact_expansion_failures_total=exact_expansion_failures_total,
        compressed_tool_errors_total=compressed_tool_errors_total,
        oversized_envelopes_total=oversized_envelopes_total,
        observed_corruption_total=observed_corruption_total,
        pre_signoff_scenarios_covered=pre_signoff_covered,
        pre_signoff_scenarios_missing=pre_signoff_missing,
        post_signoff_scenarios_covered=post_signoff_covered,
        post_signoff_scenarios_missing=post_signoff_missing,
        verdict=verdict,
        verdict_reasons=tuple(reasons),
        savings_gate_applies=False,
    )


def _format_optional_float(value: float | None, *, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:.2f}{suffix}"


def _format_list(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else "(none)"


def render_markdown(report: AggregateReport) -> str:
    """Render ``report`` as byte-deterministic Markdown.

    A pure function of ``report``'s own fields -- no wall-clock read, no
    locale-dependent formatting -- so two calls given the same report (and
    therefore the same receipt/manifest inputs) always produce identical
    bytes, which is exactly what `report check` diffs against.
    """
    lines = [
        "# Laconic M18 Runtime Beta Qualification — Aggregate Report",
        "",
        f"- Manifest hash: `{report.manifest_hash}`",
        f"- Receipt schema hash: `{report.schema_hash}`",
        f"- Candidate wheel SHA-256: `{report.candidate_wheel_sha256}`",
        f"- Eligible OMP version: {report.eligible_omp_version}",
        f"- Frozen minimums: {report.min_sessions} sessions, "
        f"{report.min_repositories} repositories, "
        f"{report.min_eligible_observations} eligible observations",
        f"- Generated at (epoch seconds): {report.generated_at:.3f}",
        f"- Verdict: **{report.verdict.value}**",
        "",
        "## Campaign composition",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Sessions total | {report.sessions_total} |",
        f"| Sessions completed (clean shutdown) | {report.sessions_completed} |",
        f"| Distinct repositories | {report.repositories_total} |",
        f"| Recorded decisions | {report.decisions_total} |",
        f"| Eligible observations (compression attempted) | {report.eligible_observations_total} |",
        "",
        "## Decisions and expansions",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Emitted | {report.emitted_total} |",
    ]
    for reason, count in report.pass_through_totals:
        lines.append(f"| Pass-through: {reason} | {count} |")
    lines += [
        f"| Raw characters | {report.raw_chars_total} |",
        f"| Visible characters | {report.visible_chars_total} |",
        f"| Characters avoided | {report.characters_avoided_total} |",
        "| Observed reduction | "
        f"{_format_optional_float(report.observed_reduction_pct, suffix='%')} |",
        f"| Full expansions | {report.full_expansions_total} |",
        f"| Span expansions | {report.span_expansions_total} |",
        f"| Latency p50 (ms, nearest-rank) | {_format_optional_float(report.latency_p50_ms)} |",
        f"| Latency p95 (ms, nearest-rank) | {_format_optional_float(report.latency_p95_ms)} |",
        "",
        "## Safety counters (must be zero for a GO verdict)",
        "",
        "| Counter | Value |",
        "| --- | --- |",
        f"| Exact expansion failures | {report.exact_expansion_failures_total} |",
        f"| Compressed tool errors | {report.compressed_tool_errors_total} |",
        f"| Oversized envelopes | {report.oversized_envelopes_total} |",
        f"| Observed corruption | {report.observed_corruption_total} |",
        "",
        "## Scenario coverage",
        "",
        f"- Pre-signoff covered: {_format_list(report.pre_signoff_scenarios_covered)}",
        f"- Pre-signoff missing: {_format_list(report.pre_signoff_scenarios_missing)}",
        f"- Post-signoff covered: {_format_list(report.post_signoff_scenarios_covered)}",
        f"- Post-signoff missing: {_format_list(report.post_signoff_scenarios_missing)}",
        "",
        "## Verdict",
        "",
        f"**{report.verdict.value}**",
        "",
        f"Reasons: {_format_list(tuple(report.verdict_reasons))}",
        "",
        "No minimum aggregate savings percentage controls this verdict "
        "(`.docs/DEVELOPMENT_PLAN.md` §6 M18; refocus design §9): observed "
        "character reduction above is reported for information only.",
        "",
    ]
    return "\n".join(lines) + "\n"


def render_from_payloads(
    payloads: Sequence[dict[str, Any]], manifest: CampaignManifest
) -> tuple[AggregateReport, str]:
    """Validate ``payloads`` and return the resulting report and its
    deterministic Markdown rendering, in one step."""
    receipts = validate_evidence_set(payloads, manifest)
    report = generate_report(receipts, manifest)
    return report, render_markdown(report)
