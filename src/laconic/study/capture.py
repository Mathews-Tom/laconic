"""Response capture for the K3 human-study harness.

``docs/system-design.md`` §4.1: "Primary measure. Defect detection rate."
and "Secondary measures. Time to decision, self-reported confidence, and
the calibration gap between confidence and correctness -- the last of
these is where the placebic-explanation hazard would show up first."
:class:`ResponseRecord` is the one shape every captured answer takes,
whether it comes from a real participant or a simulated dry run.
"""

from __future__ import annotations

from dataclasses import dataclass

from laconic.study.assignment import Condition
from laconic.study.materials import DefectClass


@dataclass(frozen=True, slots=True)
class ResponseRecord:
    """One participant's response to one task in one condition.

    Every material contains exactly one seeded defect (``materials.py``),
    so ``detected`` doubles as the correctness judgement the calibration
    gap measures against: a response is "correct" exactly when it reports
    detecting the defect that is actually present.
    """

    participant_id: int
    task_id: str
    defect_class: DefectClass
    condition: Condition
    detected: bool
    time_to_decision_s: float
    confidence: float

    def __post_init__(self) -> None:
        if self.participant_id < 0:
            raise ValueError(f"participant_id must not be negative: {self.participant_id}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be within [0, 1]: {self.confidence}")
        if self.time_to_decision_s < 0.0:
            raise ValueError(f"time_to_decision_s must not be negative: {self.time_to_decision_s}")

    @property
    def calibration_gap(self) -> float:
        """Signed gap between stated confidence and actual correctness.

        A single trial's gap is a residual, not a verdict on its own --
        the pre-registered analysis (``analysis.py``) aggregates it across
        trials and conditions. Positive means confidence ran ahead of
        correctness on this trial; negative means it ran behind.
        """
        correct = 1.0 if self.detected else 0.0
        return self.confidence - correct


def capture_response(
    *,
    participant_id: int,
    task_id: str,
    defect_class: DefectClass,
    condition: Condition,
    detected: bool,
    time_to_decision_s: float,
    confidence: float,
) -> ResponseRecord:
    """Build and validate one captured response.

    The single entry point every capture path -- a real participant's
    recorded answer or a simulated dry-run response -- goes through, so an
    out-of-range confidence or a negative decision time can never reach the
    analysis stage silently clamped or truncated; it raises here instead.
    """
    return ResponseRecord(
        participant_id=participant_id,
        task_id=task_id,
        defect_class=defect_class,
        condition=condition,
        detected=detected,
        time_to_decision_s=time_to_decision_s,
        confidence=confidence,
    )
