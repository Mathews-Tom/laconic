"""Protocol-shaped, body-free reporting for K1 Stage C batches."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from laconic.k1corpus.stage_b import ManifestSet
from laconic.k1corpus.stage_c import LoadedStageCManifest, PairedSessionMetrics, StageCLedger

#: The seven minimum sections in protocol § Analysis and reporting, in order.
PROTOCOL_REPORT_FIELDS = (
    "corpus_composition",
    "cost_totals",
    "k1",
    "k2",
    "k4",
    "k5",
    "privacy_data_governance_limitations",
    "representative_scope",
)

K1_TARGET_PCT = 25.0
K1_KILL_THRESHOLD_PCT = 15.0


@dataclass(frozen=True, slots=True)
class StageCReport:
    """The complete, body-free corpus-level protocol report."""

    corpus_composition: Mapping[str, object]
    cost_totals: Mapping[str, float | int | None]
    k1: Mapping[str, float | str | None]
    k2: Mapping[str, float | str | None]
    k4: Mapping[str, float | str | None]
    k5: Mapping[str, float | str | None]
    privacy_data_governance_limitations: tuple[str, ...]
    representative_scope: str

    def to_json(self) -> dict[str, object]:
        """Render exactly the protocol's required report sections."""
        return {
            "corpus_composition": self.corpus_composition,
            "cost_totals": self.cost_totals,
            "k1": self.k1,
            "k2": self.k2,
            "k4": self.k4,
            "k5": self.k5,
            "privacy_data_governance_limitations": list(self.privacy_data_governance_limitations),
            "representative_scope": self.representative_scope,
        }


def generate_stage_c_report(
    manifest: LoadedStageCManifest,
    *,
    selected_set: ManifestSet,
    ledger: StageCLedger,
) -> StageCReport:
    """Generate a protocol report without reading transcript bodies.

    A confirmatory K1 value is emitted only when every selected manifest entry
    has one completed, metric-bearing paired artifact. Any missing or partial
    pair is reported as invalid evidence rather than silently excluded.
    """
    completed = ledger.completed
    selected_ids = {entry.session_id for entry in manifest.entries}
    selected_completed = {
        session_id: completion
        for session_id, completion in completed.items()
        if session_id in selected_ids
    }
    missing_ids = selected_ids - set(selected_completed)
    missing_metrics = sum(completion.metrics is None for completion in selected_completed.values())
    metrics = tuple(
        completion.metrics
        for completion in selected_completed.values()
        if completion.metrics is not None
    )
    complete_paired = (
        not missing_ids
        and len(selected_completed) == len(manifest.entries)
        and len(metrics) == len(manifest.entries)
    )
    corpus_composition = {
        "set": selected_set.value,
        "selected_sessions": len(manifest.entries),
        "completed_sessions": len(selected_completed),
        "missing_or_partial_sessions": len(missing_ids) + missing_metrics,
        "retailogists_excluded_sessions": len(manifest.excluded_retailogists),
        "retailogists_excluded_lineages": len(
            {entry.project_lineage_id for entry in manifest.excluded_retailogists}
        ),
    }
    cost_totals = _cost_totals(metrics if complete_paired else ())
    k1 = _k1(metrics, selected_set=selected_set, complete_paired=complete_paired)
    k2 = _k2(metrics, complete_paired=complete_paired)
    unavailable = {
        "value": None,
        "availability": "not_collected",
        "reason": "Stage C paired replay does not supply this metric's required inputs.",
    }
    return StageCReport(
        corpus_composition=corpus_composition,
        cost_totals=cost_totals,
        k1=k1,
        k2=k2,
        k4=unavailable.copy(),
        k5=unavailable.copy(),
        privacy_data_governance_limitations=(
            "Raw session material remains local under the approved source-root boundary.",
            (
                "Only the approved paired-evidence method may send replay observations "
                "and prior actions to the configured client."
            ),
            (
                "Derived manifests, replay artifacts, ledger, and audit remain in the "
                "mode-restricted private Stage C root and are not committed."
            ),
            (
                "This report and its audit contain no transcript bodies, prompts, titles, "
                "paths, credentials, or tool-result excerpts."
            ),
        ),
        representative_scope=(
            "Representative only of the declared self-owned corpus; it makes no claim "
            "about external or client-work corpora."
        ),
    )


def _cost_totals(metrics: tuple[PairedSessionMetrics, ...]) -> dict[str, float | int | None]:
    if not metrics:
        return {
            "baseline_cost_usd": None,
            "codec_on_cost_usd": None,
            "induced_turn_count": None,
            "induced_turn_cost_usd": None,
            "net_cost_savings_usd": None,
        }
    baseline = sum(metric.baseline_cost_usd for metric in metrics)
    codec_on = sum(metric.codec_on_cost_usd for metric in metrics)
    return {
        "baseline_cost_usd": baseline,
        "codec_on_cost_usd": codec_on,
        "induced_turn_count": sum(metric.induced_turn_count for metric in metrics),
        "induced_turn_cost_usd": sum(metric.induced_turn_cost_usd for metric in metrics),
        "net_cost_savings_usd": round(baseline - codec_on, 12),
    }


def _k1(
    metrics: tuple[PairedSessionMetrics, ...], *, selected_set: ManifestSet, complete_paired: bool
) -> dict[str, float | str | None]:
    if selected_set is not ManifestSet.CONFIRMATORY:
        return {
            "value_pct": None,
            "target_pct": K1_TARGET_PCT,
            "kill_threshold_pct": K1_KILL_THRESHOLD_PCT,
            "disposition": "not_applicable_design_set",
        }
    if not complete_paired:
        return {
            "value_pct": None,
            "target_pct": K1_TARGET_PCT,
            "kill_threshold_pct": K1_KILL_THRESHOLD_PCT,
            "disposition": "invalid_partial_paired_evidence",
        }
    baseline = sum(metric.baseline_cost_usd for metric in metrics)
    codec_on = sum(metric.codec_on_cost_usd for metric in metrics)
    value = 0.0 if baseline <= 0 else 100 * (baseline - codec_on) / baseline
    disposition: Literal["target_met", "reconciliation_required", "product_path_no_go"]
    if value >= K1_TARGET_PCT:
        disposition = "target_met"
    elif value >= K1_KILL_THRESHOLD_PCT:
        disposition = "reconciliation_required"
    else:
        disposition = "product_path_no_go"
    return {
        "value_pct": round(value, 12),
        "target_pct": K1_TARGET_PCT,
        "kill_threshold_pct": K1_KILL_THRESHOLD_PCT,
        "disposition": disposition,
    }


def _k2(
    metrics: tuple[PairedSessionMetrics, ...], *, complete_paired: bool
) -> dict[str, float | str | None]:
    if not complete_paired:
        return {
            "value_pct": None,
            "availability": "invalid_partial_paired_evidence",
        }
    compared = sum(metric.compared_turn_count for metric in metrics)
    value = (
        100.0
        if compared == 0
        else 100 * sum(metric.equivalent_turn_count for metric in metrics) / compared
    )
    return {"value_pct": value, "availability": "measured"}
