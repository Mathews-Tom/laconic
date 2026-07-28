"""Orchestrates every registered gate into one
:class:`~laconic.gates.protocol.GateSuiteResult`.

K3 is always available regardless of which automated gates this build
has registered -- it is human-subject and never computed, so there is
nothing for it to depend on.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from laconic.gates import k1, k2, k4, k5
from laconic.gates.protocol import GateResult, GateSuiteResult
from laconic.replay.corpus import EmptyCorpusError
from laconic.replay.engine import find_baseline_transcripts

#: Every automated gate this build knows how to measure, in report order.
GATE_MEASURERS: dict[str, Callable[[Sequence[Path]], GateResult]] = {
    "K1": k1.measure,
    "K2": k2.measure,
    "K4": k4.measure,
    "K5": k5.measure,
}

#: K3's description, since it carries no entry in
#: :data:`~laconic.gates.thresholds.THRESHOLDS` for :func:`GateResult.manual`
#: to read.
K3_DESCRIPTION = "Human bug-catch rate, rendered view vs raw trace"


class UnknownGateError(ValueError):
    """Raised when ``only`` names a gate this build does not recognize."""


def run_gates(paths: Sequence[Path], *, only: Sequence[str] | None = None) -> GateSuiteResult:
    """Run every gate in ``only`` (default: every gate this build has
    registered in :data:`GATE_MEASURERS`, plus K3), in a stable order,
    and return one suite result.

    The default set is derived from what is actually registered, not a
    hardcoded K1-K5 list -- a build that has not yet added K4/K5 (an
    earlier PR in this milestone's own stack) must still be able to run
    its default gate set without every caller needing to know which
    gates exist yet.

    Raises :class:`UnknownGateError` for a name in ``only`` this build
    does not recognize -- silently skipping a mistyped gate name would
    make a CI run that never actually checked K2 look identical to one
    that did.
    Raises :class:`~laconic.replay.corpus.EmptyCorpusError` if ``paths``
    contains no baseline transcript at all -- matching
    :func:`laconic.replay.corpus.scan_corpus`'s own posture that a gate
    reporting PASS on zero evidence (a typo'd or empty corpus path) is
    worse than no gate at all; a per-gate PASS-shaped early return would
    otherwise be indistinguishable from a genuinely measured pass.

    K1, K2, and K4 accept any number of ``paths``; K5's benchmark items
    and response fixture are corpus-wide rather than per-baseline, so it
    accepts exactly one (:func:`laconic.gates.k5.responses_path_for`
    raises :class:`~laconic.gates.k5.K5FixtureError` for anything else).
    """
    if not find_baseline_transcripts(paths):
        listed = ", ".join(str(path) for path in paths)
        raise EmptyCorpusError(f"no baseline transcripts found under {listed}")
    gate_order = ("K1", "K2", "K3", "K4", "K5")
    default_selection = tuple(gate for gate in gate_order if gate == "K3" or gate in GATE_MEASURERS)
    selected = tuple(only) if only is not None else default_selection
    unknown = [gate for gate in selected if gate != "K3" and gate not in GATE_MEASURERS]
    if unknown:
        raise UnknownGateError(f"unknown gate(s): {', '.join(unknown)}")

    results: list[GateResult] = []
    for gate in gate_order:
        if gate not in selected:
            continue
        if gate == "K3":
            results.append(GateResult.manual("K3", K3_DESCRIPTION, detail="not evaluated"))
            continue
        results.append(GATE_MEASURERS[gate](paths))
    return GateSuiteResult(results=tuple(results))
