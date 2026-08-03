"""action-equivalence: action equivalence, compressed vs raw observation.

``docs/overview.md`` §6.3: "Action equivalence, compressed vs raw
observation ... ≥ 95%," kill "< 90% → codec is lossy where it matters."
Computed corpus-wide, matching net-cost's weighting rationale
(``.docs/DEVELOPMENT_PLAN_HISTORY.md`` H-25): total equivalent turns over
total compared turns across every session, not a mean of per-session
rates.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from laconic.gates.protocol import GateResult
from laconic.replay.engine import find_baseline_transcripts, iter_turns, load_recorded_response
from laconic.replay.equivalence import compare_session


def measure(paths: Sequence[Path]) -> GateResult:
    """Measure action-equivalence across every baseline transcript under ``paths`` that
    has a committed recorded-response fixture.

    Structural comparison only (:mod:`laconic.replay.equivalence`) --
    this gate never calls a model, matching the milestone's own
    acceptance line that structural equivalence is decided without one.
    """
    baselines = find_baseline_transcripts(paths)
    if not baselines:
        return GateResult.measured(
            "action-equivalence", 100.0, detail="no baseline transcripts found"
        )
    total_compared = 0
    total_equivalent = 0
    for baseline in baselines:
        session = load_recorded_response(baseline)
        baseline_actions = tuple(turn.actions[-1] for turn in iter_turns(baseline) if turn.actions)
        equivalence = compare_session(baseline_actions, session.non_induced_actions)
        total_compared += len(equivalence.comparisons)
        total_equivalent += sum(1 for c in equivalence.comparisons if c.is_equivalent)
    if total_compared == 0:
        return GateResult.measured(
            "action-equivalence", 100.0, detail="no comparable action turns in the corpus"
        )
    rate_pct = 100 * total_equivalent / total_compared
    detail = f"{len(baselines)} session(s), {total_equivalent}/{total_compared} turns equivalent"
    return GateResult.measured("action-equivalence", rate_pct, detail=detail)
