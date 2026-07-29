"""Full K3 harness dry run with simulated participant responses.

``DEVELOPMENT_PLAN.md`` §6 M14's acceptance line: "A dry run with simulated
responses produces an analysis-ready dataset." This module wires materials,
condition assignment, response capture, and the pre-registered analysis into
one seeded, reproducible run; ``laconic study dry-run --seed N --out FILE``
is the CLI surface over it (``laconic.cli``). No real participant is ever
involved -- every response here is drawn from a seeded random-number
generator, never from a person, per this milestone's own constraint that no
real participant may be run as part of it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from laconic.replay.corpus import JsonValue
from laconic.study.analysis import AnalysisResult, PairedMeasure, analyze
from laconic.study.assignment import Condition, Trial, assign_conditions
from laconic.study.capture import ResponseRecord, capture_response
from laconic.study.materials import build_materials

#: Default simulated participant count. Chosen to comfortably clear
#: :data:`~laconic.study.analysis.MINIMUM_PARTICIPANTS`, the pre-registered
#: floor the equivalence analysis requires -- not a claim that this many
#: simulated participants will land inside the margin. Whether a given
#: seed's simulated data is judged equivalent depends on the realized
#: sampling noise, exactly as it would for real participant data; a dry
#: run's job is to exercise the pipeline end to end, not to stage a
#: predetermined outcome.
DEFAULT_PARTICIPANT_COUNT = 24

#: Base simulated detection probability every response starts from, before
#: a per-condition adjustment. Not a claim about a real K3 outcome -- a dry
#: run's only job is to exercise the full pipeline end to end with a
#: plausible-shaped dataset.
_BASE_DETECTION_PROBABILITY = 0.80

#: Simulated rendered-condition detection nudge: the *input* parameter to
#: the simulator, not a guarantee about the *realized* equivalence verdict.
#: A -2pp population-level nudge is consistent with the margin, but with a
#: finite simulated sample the measured difference and its confidence
#: interval can still land outside it, the same way real sampling noise
#: could move a genuine small effect past the margin in either direction.
_RENDERED_DETECTION_DELTA = -0.02


@dataclass(frozen=True, slots=True)
class DryRunResult:
    """Everything one dry run produced: inputs, raw responses, and analysis."""

    seed: int
    participant_count: int
    trials: tuple[Trial, ...]
    responses: tuple[ResponseRecord, ...]
    analysis: AnalysisResult


def _simulate_response(
    rng: random.Random, trial: Trial, condition: Condition, task_id: str
) -> ResponseRecord:
    probability = _BASE_DETECTION_PROBABILITY
    if condition is Condition.RENDERED:
        probability += _RENDERED_DETECTION_DELTA
    detected = rng.random() < probability
    confidence = min(1.0, max(0.0, rng.gauss(0.75 if detected else 0.4, 0.12)))
    time_to_decision_s = max(1.0, rng.gauss(20.0, 6.0))
    return capture_response(
        participant_id=trial.participant_id,
        task_id=task_id,
        defect_class=trial.defect_class,
        condition=condition,
        detected=detected,
        time_to_decision_s=time_to_decision_s,
        confidence=confidence,
    )


def run(*, seed: int, participant_count: int = DEFAULT_PARTICIPANT_COUNT) -> DryRunResult:
    """Run the full harness with simulated responses, seeded and reproducible.

    The same ``(seed, participant_count)`` always reproduces the same
    trials, responses, and analysis -- required for a dry run to be a
    trustworthy demonstration of the pipeline rather than a one-off sample.
    Each trial's two responses are simulated in ``trial.order_first`` order
    -- the condition the counterbalanced design says this participant sees
    first is drawn from the RNG first -- so the dataset's own generation
    order matches the presentation order it records, not an arbitrary
    rendered-then-raw sequence.
    """
    materials = build_materials()
    trials = assign_conditions(materials, participant_count=participant_count, seed=seed)
    rng = random.Random(seed)
    responses: list[ResponseRecord] = []
    for trial in trials:
        conditions = (
            (Condition.RENDERED, trial.rendered_task_id),
            (Condition.RAW, trial.raw_task_id),
        )
        if trial.order_first is Condition.RAW:
            conditions = (conditions[1], conditions[0])
        for condition, task_id in conditions:
            responses.append(_simulate_response(rng, trial, condition, task_id))
    return DryRunResult(
        seed=seed,
        participant_count=participant_count,
        trials=trials,
        responses=tuple(responses),
        analysis=analyze(responses),
    )


def _paired_measure_json(measure: PairedMeasure) -> dict[str, JsonValue]:
    return {"label": measure.label, "mean_diff": measure.mean_diff, "n": measure.n}


def to_json(result: DryRunResult) -> dict[str, JsonValue]:
    """Serialize a dry run into an analysis-ready dataset document.

    ``dataset`` is the per-response record set the pre-registered analysis
    consumes; ``analysis`` is that analysis's own output, included so the
    written file is proof the script actually ran against this dataset, not
    only a place to store raw responses for a later, separate run. Each
    dataset row also carries its trial's ``order_first`` and
    ``sequence_index`` -- the counterbalancing PR-2 computes is otherwise
    invisible in this output, and a later order-effect check needs it to
    tell which condition a participant actually saw first for that pair.
    """
    trial_by_key = {(trial.participant_id, trial.defect_class): trial for trial in result.trials}
    dataset: list[JsonValue] = [
        {
            "participant_id": response.participant_id,
            "task_id": response.task_id,
            "defect_class": response.defect_class.value,
            "condition": response.condition.value,
            "detected": response.detected,
            "time_to_decision_s": response.time_to_decision_s,
            "confidence": response.confidence,
            "calibration_gap": response.calibration_gap,
            "order_first": trial_by_key[
                (response.participant_id, response.defect_class)
            ].order_first.value,
            "sequence_index": trial_by_key[
                (response.participant_id, response.defect_class)
            ].sequence_index,
        }
        for response in result.responses
    ]
    detection = result.analysis.detection
    return {
        "seed": result.seed,
        "participant_count": result.participant_count,
        "dataset": dataset,
        "analysis": {
            "n_participants": result.analysis.n_participants,
            "n_pairs": result.analysis.n_pairs,
            "detection": {
                "rendered_rate": detection.rendered_rate,
                "raw_rate": detection.raw_rate,
                "diff_pp": detection.diff_pp,
                "margin_pp": detection.margin_pp,
                "ci_low_pp": detection.ci_low_pp,
                "ci_high_pp": detection.ci_high_pp,
                "equivalent": detection.equivalent,
            },
            "time_to_decision_s": _paired_measure_json(result.analysis.time_to_decision),
            "confidence": _paired_measure_json(result.analysis.confidence),
            "calibration_gap": _paired_measure_json(result.analysis.calibration_gap),
        },
    }
