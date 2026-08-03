"""Orchestrates every registered gate into one
:class:`~laconic.gates.protocol.GateSuiteResult`.

human-bug-catch is always available regardless of which automated gates this build
has registered -- it is human-subject and never computed, so there is
nothing for it to depend on.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from laconic.gates import action_equivalence, codec_overhead, net_cost, reasoning_accuracy
from laconic.gates.protocol import GateResult, GateSuiteResult
from laconic.replay.corpus import EmptyCorpusError
from laconic.replay.engine import find_baseline_transcripts

#: Every automated gate this build knows how to measure, in report order.
GATE_MEASURERS: dict[str, Callable[[Sequence[Path]], GateResult]] = {
    "net-cost": net_cost.measure,
    "action-equivalence": action_equivalence.measure,
    "codec-overhead": codec_overhead.measure,
    "reasoning-accuracy": reasoning_accuracy.measure,
}

#: Human bug-catch is manual because it has no automated threshold.
HUMAN_BUG_CATCH_DESCRIPTION = "Human bug-catch rate, rendered view vs raw trace"


class UnknownGateError(ValueError):
    """Raised when ``only`` names a gate this build does not recognize."""


def run_gates(paths: Sequence[Path], *, only: Sequence[str] | None = None) -> GateSuiteResult:
    """Run each selected evaluation criterion in a stable order.

    The default set derives from registered automated criteria, plus the
    explicitly reported manual human-bug-catch assessment. Unknown names fail
    rather than silently skipping an evaluation. The reasoning-accuracy
    response fixture is corpus-wide and therefore requires exactly one path.
    """
    if not find_baseline_transcripts(paths):
        listed = ", ".join(str(path) for path in paths)
        raise EmptyCorpusError(f"no baseline transcripts found under {listed}")
    gate_order = (
        "net-cost",
        "action-equivalence",
        "human-bug-catch",
        "codec-overhead",
        "reasoning-accuracy",
    )
    default_selection = tuple(
        gate for gate in gate_order if gate == "human-bug-catch" or gate in GATE_MEASURERS
    )
    selected = tuple(only) if only is not None else default_selection
    unknown = [
        gate for gate in selected if gate != "human-bug-catch" and gate not in GATE_MEASURERS
    ]
    if unknown:
        raise UnknownGateError(f"unknown gate(s): {', '.join(unknown)}")

    results: list[GateResult] = []
    for gate in gate_order:
        if gate not in selected:
            continue
        if gate == "human-bug-catch":
            results.append(
                GateResult.manual(
                    "human-bug-catch", HUMAN_BUG_CATCH_DESCRIPTION, detail="not evaluated"
                )
            )
            continue
        results.append(GATE_MEASURERS[gate](paths))
    return GateSuiteResult(results=tuple(results))
