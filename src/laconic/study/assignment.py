"""Within-subjects, counterbalanced condition assignment for the human-bug-catch harness.

``docs/system-design.md`` §4.1's Design line: "Within-subjects, counter-
balanced. Each participant reviews agent traces in both conditions --
rendered view and raw trace -- on matched task pairs, with condition order
randomised." :func:`assign_conditions` builds exactly that: every simulated
or real participant sees, for each :class:`~laconic.study.materials.DefectClass`,
one matched-pair variant in the rendered condition and the other in the raw
condition, with the presentation order (which condition they see first)
counterbalanced across the participant pool rather than fixed.

Two independent sources of imbalance are guarded against separately:

- **Order confound.** If every participant saw rendered first, a practice
  or fatigue effect could masquerade as a condition effect. Half the
  participants (as evenly as ``participant_count`` allows) see rendered
  first; the other half see raw first.
- **Task confound.** If variant ``"a"`` always landed in the rendered
  condition, a difference between conditions could really be a difference
  between the two matched tasks. Which variant lands in which condition is
  independently counterbalanced per defect class.

Both are exact by construction (:func:`_balanced_flags` builds an exactly
even split, then shuffles it), not approximate averages over many draws;
``tests/test_study.py``'s ``-k balance`` suite verifies the shuffle itself
carries no positional bias, over many seeds.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from laconic.study.materials import DefectClass, TraceMaterial, materials_for


class Condition(StrEnum):
    """Which of the two study surfaces a participant is shown."""

    RENDERED = "rendered"
    RAW = "raw"


@dataclass(frozen=True, slots=True)
class Trial:
    """One participant's matched-pair viewing for one defect class."""

    participant_id: int
    defect_class: DefectClass
    rendered_task_id: str
    raw_task_id: str
    order_first: Condition
    sequence_index: int

    def __post_init__(self) -> None:
        if self.participant_id < 0:
            raise ValueError(f"participant_id must not be negative: {self.participant_id}")
        if self.sequence_index < 0:
            raise ValueError(f"sequence_index must not be negative: {self.sequence_index}")
        if self.rendered_task_id == self.raw_task_id:
            raise ValueError(
                f"rendered and raw task must differ: both are {self.rendered_task_id!r}"
            )


def _balanced_flags(count: int, rng: random.Random) -> list[bool]:
    """Return an exactly-even-as-possible, order-shuffled list of booleans.

    ``count // 2`` entries are ``True`` and the rest ``False`` -- an odd
    ``count`` rounds the ``False`` side up by one, so no single boolean can
    claim a majority the shuffle would then merely relocate. Shuffling
    (rather than drawing each entry independently) is what makes the split
    exact for every ``count`` and every seed, instead of merely balanced on
    average over many seeds.
    """
    half_true = count // 2
    flags = [True] * half_true + [False] * (count - half_true)
    rng.shuffle(flags)
    return flags


def assign_conditions(
    materials: Sequence[TraceMaterial], *, participant_count: int, seed: int
) -> tuple[Trial, ...]:
    """Assign every participant a counterbalanced trial per defect class.

    Deterministic in ``seed``: the same ``(materials, participant_count,
    seed)`` always produces the same trials, in the same order, which is
    what lets a dry run be reproduced and a balance check be repeated
    exactly. ``participant_count`` must be at least 2 -- a counterbalanced
    design needs both presentation orders represented.
    """
    if participant_count < 2:
        raise ValueError(f"participant_count must be at least 2: {participant_count}")
    rng = random.Random(seed)
    order_first_flags = _balanced_flags(participant_count, rng)
    defect_classes = tuple(DefectClass)
    variant_flags_by_class = {
        defect_class: _balanced_flags(participant_count, rng) for defect_class in defect_classes
    }

    trials: list[Trial] = []
    for participant_id in range(participant_count):
        order_first = Condition.RENDERED if order_first_flags[participant_id] else Condition.RAW
        for sequence_index, defect_class in enumerate(defect_classes):
            variant_a, variant_b = materials_for(materials, defect_class)
            variant_a_is_rendered = variant_flags_by_class[defect_class][participant_id]
            rendered_task_id = variant_a.task_id if variant_a_is_rendered else variant_b.task_id
            raw_task_id = variant_b.task_id if variant_a_is_rendered else variant_a.task_id
            trials.append(
                Trial(
                    participant_id=participant_id,
                    defect_class=defect_class,
                    rendered_task_id=rendered_task_id,
                    raw_task_id=raw_task_id,
                    order_first=order_first,
                    sequence_index=sequence_index,
                )
            )
    return tuple(trials)
