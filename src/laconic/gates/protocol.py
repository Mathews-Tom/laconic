"""The gate result shape and target/kill verdict logic shared by every
gate module.

``docs/system-design.md`` §6 M9's acceptance line: "A result below a
target but above its kill condition remains a reported failure but exits
zero; it blocks dependent milestones until reconciliation resolves the
target miss." :func:`evaluate` is the one place that three-way split is
computed, so K1 and K2 (which have a real gap between target and kill)
and K4/K5 (whose kill condition is the same boundary as their target)
are judged by identical logic rather than four hand-rolled comparisons.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from laconic.gates.thresholds import THRESHOLDS


class GateVerdict(StrEnum):
    """One gate's outcome. There is no "unsure" value: every automated
    gate resolves to exactly one of the first three; K3 always reports
    the fourth."""

    PASS = "pass"
    FAILED_TARGET = "failed_target"
    KILL = "kill"
    MANUAL = "manual"

    @property
    def exits_non_zero(self) -> bool:
        """Whether this verdict, alone, must make the gate suite's exit
        code non-zero -- ``docs/system-design.md`` §6 M9: "A kill
        condition exits non-zero. A result below a target but above its
        kill condition remains a reported failure but exits zero."""
        return self is GateVerdict.KILL


def evaluate(gate: str, value: float) -> GateVerdict:
    """Judge ``value`` against ``gate``'s threshold in
    :data:`~laconic.gates.thresholds.THRESHOLDS`.

    Raises :class:`KeyError` for a gate name outside the table -- there is
    no silent default verdict for a threshold this module does not know
    about.
    """
    threshold = THRESHOLDS[gate]
    if threshold.direction == "at_least":
        if value >= threshold.target:
            return GateVerdict.PASS
        if value < threshold.kill:
            return GateVerdict.KILL
        return GateVerdict.FAILED_TARGET
    if value <= threshold.target:
        return GateVerdict.PASS
    if value > threshold.kill:
        return GateVerdict.KILL
    return GateVerdict.FAILED_TARGET


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's measured outcome, self-describing enough to render or
    serialize without a caller re-deriving anything from
    :data:`~laconic.gates.thresholds.THRESHOLDS`."""

    gate: str
    description: str
    value: float | None
    unit: str
    target: float | None
    kill: float | None
    verdict: GateVerdict
    detail: str

    @staticmethod
    def measured(gate: str, value: float, *, detail: str) -> GateResult:
        """Build a :class:`GateResult` for an automated gate, deriving
        ``verdict`` from :func:`evaluate` so a caller cannot construct a
        result whose verdict disagrees with its own value."""
        threshold = THRESHOLDS[gate]
        return GateResult(
            gate=gate,
            description=threshold.description,
            value=value,
            unit=threshold.unit,
            target=threshold.target,
            kill=threshold.kill,
            verdict=evaluate(gate, value),
            detail=detail,
        )

    @staticmethod
    def manual(gate: str, description: str, *, detail: str) -> GateResult:
        """Build a :class:`GateResult` for a gate this build never
        automates (K3) -- reported explicitly rather than omitted."""
        return GateResult(
            gate=gate,
            description=description,
            value=None,
            unit="",
            target=None,
            kill=None,
            verdict=GateVerdict.MANUAL,
            detail=detail,
        )

    def to_json(self) -> dict[str, object]:
        """Serialize every field ``laconic gates --format json`` reports,
        with ``verdict`` as its plain string value rather than the enum."""
        return {
            "gate": self.gate,
            "description": self.description,
            "value": self.value,
            "unit": self.unit,
            "target": self.target,
            "kill": self.kill,
            "verdict": self.verdict.value,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class GateSuiteResult:
    """Every gate this run evaluated, in the order they ran."""

    results: tuple[GateResult, ...]

    @property
    def exit_code(self) -> int:
        """0 unless any result is a kill condition -- a failed-but-not-killed
        target never makes the suite exit non-zero on its own."""
        return 1 if any(result.verdict.exits_non_zero for result in self.results) else 0

    def to_json(self) -> dict[str, object]:
        """Serialize every result, in run order, under a ``"gates"`` key."""
        return {"gates": [result.to_json() for result in self.results]}
