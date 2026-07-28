"""K1: session-level net cost reduction on replayed real traces,
including follow-up reads the codec induces.

``docs/overview.md`` §6.3: "session-level **net** cost reduction ... ≥
25%," kill "< 15% → complexity not justified." Computed corpus-wide, not
as a mean of per-session percentages: :func:`measure` sums every
session's baseline and codec-on cost first and takes the ratio of the
totals, so one cheap, high-percentage session cannot outvote an
expensive one -- see ``.docs/DEVELOPMENT_PLAN_HISTORY.md`` H-25.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from laconic.gates.protocol import GateResult
from laconic.replay.engine import find_baseline_transcripts, replay_on


def measure(paths: Sequence[Path]) -> GateResult:
    """Measure K1 across every baseline transcript under ``paths`` that
    has a committed recorded-response fixture.

    Reuses :func:`laconic.replay.engine.replay_on` (``mode="recorded"``)
    directly -- the same CI-safe, no-model-call aggregation M8 shipped --
    so K1 inherits its "no gross-only figure" guarantee: ``NetCostReport``
    has no field this gate could read that is not already net of induced
    reads.
    """
    baselines = find_baseline_transcripts(paths)
    if not baselines:
        return GateResult.measured("K1", 0.0, detail="no baseline transcripts found")
    reports = replay_on(paths)
    total_baseline = sum(report.baseline.cost.total for _, report in reports)
    total_codec_on = sum(report.codec_on.cost.total for _, report in reports)
    if total_baseline <= 0.0:
        return GateResult.measured("K1", 0.0, detail="corpus recorded no billable baseline cost")
    net_pct = 100 * (total_baseline - total_codec_on) / total_baseline
    detail = (
        f"{len(reports)} session(s): baseline ${total_baseline:.4f}, "
        f"codec-on ${total_codec_on:.4f}, net savings ${total_baseline - total_codec_on:.4f}"
    )
    return GateResult.measured("K1", net_pct, detail=detail)
