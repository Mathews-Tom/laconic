"""Tests for the K3 human-study harness."""

from __future__ import annotations

import statistics

import pytest

from laconic.ledger import Ledger, ObservationKind
from laconic.study.analysis import (
    EQUIVALENCE_MARGIN_PP,
    MINIMUM_PARTICIPANTS,
    InsufficientDataError,
    analyze,
)
from laconic.study.assignment import Condition, assign_conditions
from laconic.study.capture import ResponseRecord, capture_response
from laconic.study.materials import DefectClass, build_materials, materials_for


def test_materials_cover_all_four_defect_classes() -> None:
    materials = build_materials()
    assert {material.defect_class for material in materials} == set(DefectClass)


def test_materials_each_defect_class_has_exactly_two_matched_variants() -> None:
    materials = build_materials()
    for defect_class in DefectClass:
        matched = [m for m in materials if m.defect_class == defect_class]
        assert {m.variant for m in matched} == {"a", "b"}


def test_materials_task_ids_are_unique() -> None:
    materials = build_materials()
    task_ids = [material.task_id for material in materials]
    assert len(task_ids) == len(set(task_ids)) == 8


def test_materials_matched_pair_id_links_variants_of_the_same_class() -> None:
    materials = build_materials()
    variant_a, variant_b = materials_for(materials, DefectClass.BOUNDARY_CONDITION)
    assert variant_a.matched_pair_id == variant_b.matched_pair_id == "incorrect_boundary_condition"
    assert variant_a.task_id != variant_b.task_id


def test_materials_for_raises_on_missing_pair() -> None:
    with pytest.raises(ValueError, match="expected exactly 2"):
        materials_for((), DefectClass.SWALLOWED_EXCEPTION)


def test_materials_raw_text_shows_the_seeded_defect() -> None:
    materials = build_materials()
    by_task = {material.task_id: material for material in materials}
    assert "DISCOUNT_TABLE[code]" in by_task["unhandled-error-a"].raw_text
    assert "CUSTOMERS[customer_id]" in by_task["unhandled-error-b"].raw_text
    assert "range(1, len(levels))" in by_task["boundary-a"].raw_text
    assert "range(1, len(purchases))" in by_task["boundary-b"].raw_text
    assert "except Exception:\n        pass" in by_task["swallowed-a"].raw_text
    assert "logger.debug" in by_task["swallowed-b"].raw_text
    assert "def normalize_email(email):\n    return email.lower()" in (
        by_task["wrong-target-a"].raw_text
    )
    assert "def compute_order_discount(order):\n    return round(order.subtotal * 0.9)" in (
        by_task["wrong-target-b"].raw_text
    )


def test_materials_rendered_text_never_repeats_the_raw_defect_line() -> None:
    """The rendered condition asserts only structural facts and narration --
    never the literal source line -- proving the two conditions genuinely
    differ in what content they expose, not merely in formatting.
    """
    materials = build_materials()
    for material in materials:
        assert "DISCOUNT_TABLE[code]" not in material.rendered_text
        assert "except Exception:" not in material.rendered_text


def test_materials_rendered_text_includes_a_visually_distinct_narration_block() -> None:
    materials = build_materials()
    for material in materials:
        assert "--- generated narration" in material.rendered_text
        assert "--- end generated narration ---" in material.rendered_text
        for handle in material.handles:
            assert f"[{handle}]" in material.rendered_text


def test_materials_rendered_text_carries_a_handle_per_turn() -> None:
    materials = build_materials()
    for material in materials:
        assert len(material.handles) == len(set(material.handles))
        for handle in material.handles:
            assert f"[{handle}]" in material.rendered_text


def test_materials_handles_match_what_a_real_ledger_would_mint() -> None:
    """Regression for a per-task global handle index that diverged from
    :meth:`~laconic.ledger.Ledger.register`'s per-kind ordinal scheme --
    the rendered condition exists specifically to show participants what
    ``laconic view`` actually renders, so a handle scheme the product
    never produces would be a defect in the measurement instrument itself.
    Independently reconstructs each material's expected handle sequence by
    feeding a real :class:`~laconic.ledger.Ledger` observations of the
    same kinds, in the same order, and confirms it mints the identical
    handles.
    """
    kind_by_prefix = {
        "F": ObservationKind.FILE,
        "B": ObservationKind.COMMAND,
        "X": ObservationKind.OTHER,
    }
    materials = build_materials()
    for material in materials:
        kinds = [kind_by_prefix[handle[0]] for handle in material.handles]
        with Ledger(":memory:", f"handle-check-{material.task_id}") as ledger:
            minted = tuple(
                ledger.register(
                    kind, f"subject-{index}", f"raw-{index}", "encoded", turn=index
                ).handle
                for index, kind in enumerate(kinds)
            )
        assert minted == material.handles


def test_materials_are_deterministic_across_builds() -> None:
    first = {m.task_id: (m.rendered_text, m.raw_text, m.handles) for m in build_materials()}
    second = {m.task_id: (m.rendered_text, m.raw_text, m.handles) for m in build_materials()}
    assert first == second


def test_balance_trial_count_is_participants_times_defect_classes() -> None:
    materials = build_materials()
    trials = assign_conditions(materials, participant_count=10, seed=0)
    assert len(trials) == 10 * len(DefectClass)


def test_balance_each_participant_gets_one_trial_per_defect_class() -> None:
    materials = build_materials()
    trials = assign_conditions(materials, participant_count=6, seed=1)
    for participant_id in range(6):
        classes = {t.defect_class for t in trials if t.participant_id == participant_id}
        assert classes == set(DefectClass)


def test_balance_sequence_index_spans_every_defect_class_without_gaps() -> None:
    materials = build_materials()
    trials = assign_conditions(materials, participant_count=4, seed=2)
    for participant_id in range(4):
        indices = sorted(t.sequence_index for t in trials if t.participant_id == participant_id)
        assert indices == list(range(len(DefectClass)))


def test_balance_order_first_is_exactly_even_per_seed() -> None:
    materials = build_materials()
    for participant_count in (8, 9, 24, 25):
        for seed in range(10):
            trials = assign_conditions(materials, participant_count=participant_count, seed=seed)
            first_per_participant = {
                t.participant_id: t.order_first for t in trials if t.sequence_index == 0
            }
            assert len(first_per_participant) == participant_count
            rendered_first = sum(
                1 for order in first_per_participant.values() if order is Condition.RENDERED
            )
            assert rendered_first == participant_count // 2


def test_balance_variant_to_condition_mapping_is_exactly_even_per_class_per_seed() -> None:
    materials = build_materials()
    for seed in range(10):
        trials = assign_conditions(materials, participant_count=20, seed=seed)
        for defect_class in DefectClass:
            variant_a, _variant_b = materials_for(materials, defect_class)
            class_trials = [t for t in trials if t.defect_class == defect_class]
            assert len(class_trials) == 20
            variant_a_rendered = sum(
                1 for t in class_trials if t.rendered_task_id == variant_a.task_id
            )
            assert variant_a_rendered == 20 // 2


def test_balance_participant_count_below_two_raises() -> None:
    materials = build_materials()
    with pytest.raises(ValueError, match="at least 2"):
        assign_conditions(materials, participant_count=1, seed=0)


def test_balance_assignment_is_reproducible_for_the_same_seed() -> None:
    materials = build_materials()
    first = assign_conditions(materials, participant_count=12, seed=7)
    second = assign_conditions(materials, participant_count=12, seed=7)
    assert first == second


def test_balance_variant_assignment_actually_varies_across_seeds() -> None:
    """Exact per-seed evenness (verified above) could, in principle, be
    satisfied by a rigged, non-random assignment that always gives
    participant 0 the same variant. This confirms real randomization: both
    possible task assignments for participant 0's first trial are observed
    across a range of seeds.
    """
    materials = build_materials()
    defect_class = next(iter(DefectClass))
    observed_rendered_tasks = {
        next(
            t.rendered_task_id
            for t in assign_conditions(materials, participant_count=8, seed=seed)
            if t.participant_id == 0 and t.defect_class == defect_class
        )
        for seed in range(30)
    }
    variant_a, variant_b = materials_for(materials, defect_class)
    assert observed_rendered_tasks == {variant_a.task_id, variant_b.task_id}


def test_balance_order_first_assignment_is_not_positionally_biased_across_seeds() -> None:
    """Statistical verification over repeated seeds: whichever participant
    slot is checked, the probability it lands "rendered first" must sit
    inside a proper binomial confidence interval around 0.5 -- a shuffle
    with a hidden positional bias (e.g. participant 0 always landing in the
    same half) would fail this over enough seeds even though every single
    seed's own split (tested above) is exactly even.
    """
    materials = build_materials()
    participant_count = 20
    seeds = range(600)
    rendered_first_for_participant_0 = 0
    for seed in seeds:
        trials = assign_conditions(materials, participant_count=participant_count, seed=seed)
        first_trial = next(t for t in trials if t.participant_id == 0 and t.sequence_index == 0)
        if first_trial.order_first is Condition.RENDERED:
            rendered_first_for_participant_0 += 1

    n = len(seeds)
    p_hat = rendered_first_for_participant_0 / n
    z = statistics.NormalDist().inv_cdf(0.9995)  # two-sided 99.9% CI
    standard_error = (0.5 * 0.5 / n) ** 0.5
    low, high = 0.5 - z * standard_error, 0.5 + z * standard_error
    assert low <= p_hat <= high, (p_hat, low, high)


def test_capture_computes_calibration_gap_when_defect_detected() -> None:
    response = capture_response(
        participant_id=0,
        task_id="boundary-a",
        defect_class=DefectClass.BOUNDARY_CONDITION,
        condition=Condition.RENDERED,
        detected=True,
        time_to_decision_s=12.5,
        confidence=0.75,
    )
    assert response.calibration_gap == pytest.approx(-0.25)


def test_capture_computes_calibration_gap_when_defect_missed() -> None:
    response = capture_response(
        participant_id=0,
        task_id="boundary-a",
        defect_class=DefectClass.BOUNDARY_CONDITION,
        condition=Condition.RAW,
        detected=False,
        time_to_decision_s=8.0,
        confidence=0.75,
    )
    assert response.calibration_gap == pytest.approx(0.75)


def test_capture_accepts_boundary_confidence_values() -> None:
    low = capture_response(
        participant_id=0,
        task_id="boundary-a",
        defect_class=DefectClass.BOUNDARY_CONDITION,
        condition=Condition.RAW,
        detected=False,
        time_to_decision_s=1.0,
        confidence=0.0,
    )
    high = capture_response(
        participant_id=0,
        task_id="boundary-a",
        defect_class=DefectClass.BOUNDARY_CONDITION,
        condition=Condition.RAW,
        detected=True,
        time_to_decision_s=1.0,
        confidence=1.0,
    )
    assert low.confidence == 0.0
    assert high.confidence == 1.0


@pytest.mark.parametrize("confidence", [-0.01, 1.01, -5.0, 2.0])
def test_capture_rejects_confidence_out_of_range(confidence: float) -> None:
    with pytest.raises(ValueError, match="confidence must be within"):
        capture_response(
            participant_id=0,
            task_id="boundary-a",
            defect_class=DefectClass.BOUNDARY_CONDITION,
            condition=Condition.RAW,
            detected=False,
            time_to_decision_s=1.0,
            confidence=confidence,
        )


def test_capture_rejects_negative_time_to_decision() -> None:
    with pytest.raises(ValueError, match="time_to_decision_s must not be negative"):
        capture_response(
            participant_id=0,
            task_id="boundary-a",
            defect_class=DefectClass.BOUNDARY_CONDITION,
            condition=Condition.RAW,
            detected=False,
            time_to_decision_s=-0.1,
            confidence=0.5,
        )


def test_capture_rejects_negative_participant_id() -> None:
    with pytest.raises(ValueError, match="participant_id must not be negative"):
        capture_response(
            participant_id=-1,
            task_id="boundary-a",
            defect_class=DefectClass.BOUNDARY_CONDITION,
            condition=Condition.RAW,
            detected=False,
            time_to_decision_s=1.0,
            confidence=0.5,
        )


def test_capture_response_round_trips_every_field() -> None:
    response = capture_response(
        participant_id=3,
        task_id="wrong-target-b",
        defect_class=DefectClass.WRONG_TARGET_EDIT,
        condition=Condition.RENDERED,
        detected=True,
        time_to_decision_s=42.0,
        confidence=0.6,
    )
    assert isinstance(response, ResponseRecord)
    assert response.participant_id == 3
    assert response.task_id == "wrong-target-b"
    assert response.defect_class is DefectClass.WRONG_TARGET_EDIT
    assert response.condition is Condition.RENDERED
    assert response.detected is True
    assert response.time_to_decision_s == 42.0
    assert response.confidence == 0.6


def _response(
    *,
    participant_id: int = 0,
    task_id: str = "boundary-a",
    defect_class: DefectClass = DefectClass.BOUNDARY_CONDITION,
    condition: Condition,
    detected: bool,
    confidence: float = 0.7,
    time_to_decision_s: float = 10.0,
) -> ResponseRecord:
    return capture_response(
        participant_id=participant_id,
        task_id=task_id,
        defect_class=defect_class,
        condition=condition,
        detected=detected,
        time_to_decision_s=time_to_decision_s,
        confidence=confidence,
    )


def test_analysis_equivalence_margin_is_five_percentage_points() -> None:
    assert EQUIVALENCE_MARGIN_PP == 5.0


def test_analysis_takes_no_margin_parameter() -> None:
    """Structural enforcement of the pre-registration constraint: no
    call-site anywhere can pass a different margin, because there is no
    parameter to pass it through.
    """
    import inspect

    signature = inspect.signature(analyze)
    assert list(signature.parameters) == ["responses"]


def test_analysis_raises_on_unmatched_single_response() -> None:
    responses = [_response(condition=Condition.RENDERED, detected=True)]
    with pytest.raises(InsufficientDataError, match="missing"):
        analyze(responses)


def test_analysis_raises_on_duplicate_condition_response() -> None:
    responses = [
        _response(condition=Condition.RENDERED, detected=True),
        _response(condition=Condition.RENDERED, detected=False),
    ]
    with pytest.raises(InsufficientDataError, match="duplicate"):
        analyze(responses)


def test_analysis_raises_on_empty_response_set() -> None:
    with pytest.raises(InsufficientDataError, match="no matched"):
        analyze([])


def test_analysis_raises_below_minimum_participants() -> None:
    responses = [
        _response(participant_id=participant, condition=condition, detected=True)
        for participant in range(MINIMUM_PARTICIPANTS - 1)
        for condition in (Condition.RENDERED, Condition.RAW)
    ]
    with pytest.raises(InsufficientDataError, match=f"at least {MINIMUM_PARTICIPANTS}"):
        analyze(responses)


def test_analysis_perfect_agreement_at_the_minimum_is_equivalent() -> None:
    responses = [
        _response(participant_id=participant, condition=condition, detected=True)
        for participant in range(MINIMUM_PARTICIPANTS)
        for condition in (Condition.RENDERED, Condition.RAW)
    ]
    result = analyze(responses)
    assert result.n_pairs == MINIMUM_PARTICIPANTS
    assert result.n_participants == MINIMUM_PARTICIPANTS
    assert result.detection.diff_pp == pytest.approx(0.0)
    assert result.detection.ci_low_pp < 0.0 < result.detection.ci_high_pp
    assert result.detection.equivalent is True


def test_analysis_large_true_difference_is_not_judged_equivalent() -> None:
    responses = [
        _response(participant_id=participant, condition=Condition.RENDERED, detected=False)
        for participant in range(MINIMUM_PARTICIPANTS)
    ] + [
        _response(participant_id=participant, condition=Condition.RAW, detected=True)
        for participant in range(MINIMUM_PARTICIPANTS)
    ]
    result = analyze(responses)
    assert result.detection.diff_pp == pytest.approx(-100.0)
    assert result.detection.equivalent is False


def _pair_for_diff(participant_id: int, quarter_steps: int) -> list[ResponseRecord]:
    """Build one participant's four defect-class pairs so their mean
    detection-rate difference (rendered - raw) is exactly
    ``quarter_steps / 4``: ``|quarter_steps|`` pairs are discordant in the
    matching direction, the rest agree in both conditions.
    """
    defect_classes = list(DefectClass)
    responses: list[ResponseRecord] = []
    for index, defect_class in enumerate(defect_classes):
        if index < abs(quarter_steps):
            rendered_detected = quarter_steps > 0
            raw_detected = not rendered_detected
        else:
            rendered_detected = raw_detected = True
        responses.append(
            _response(
                participant_id=participant_id,
                task_id=f"{defect_class.value}-task",
                defect_class=defect_class,
                condition=Condition.RENDERED,
                detected=rendered_detected,
            )
        )
        responses.append(
            _response(
                participant_id=participant_id,
                task_id=f"{defect_class.value}-task",
                defect_class=defect_class,
                condition=Condition.RAW,
                detected=raw_detected,
            )
        )
    return responses


def test_analysis_narrow_variance_within_margin_is_equivalent() -> None:
    """Real between-participant variance (not the zero-variance shape
    B1 exercised), with the CI narrow enough to sit inside the margin.
    """
    per_participant_steps = [0] * 18 + [1] * 3 + [-1] * 3
    responses = [
        response
        for participant_id, steps in enumerate(per_participant_steps)
        for response in _pair_for_diff(participant_id, steps)
    ]
    result = analyze(responses)
    assert result.n_participants == 24
    assert result.detection.diff_pp == pytest.approx(0.0, abs=1e-9)
    assert result.detection.ci_high_pp < EQUIVALENCE_MARGIN_PP
    assert result.detection.ci_low_pp > -EQUIVALENCE_MARGIN_PP
    assert result.detection.equivalent is True


def test_analysis_same_mean_wider_variance_is_not_equivalent() -> None:
    """Same mean difference (0pp) as the narrow-variance case above, but
    wider spread across participants -- proving the verdict tracks the
    confidence interval, not merely the point estimate.
    """
    per_participant_steps = [1] * 8 + [-1] * 8 + [2] * 4 + [-2] * 4
    responses = [
        response
        for participant_id, steps in enumerate(per_participant_steps)
        for response in _pair_for_diff(participant_id, steps)
    ]
    result = analyze(responses)
    assert result.n_participants == 24
    assert result.detection.diff_pp == pytest.approx(0.0, abs=1e-9)
    assert result.detection.ci_high_pp > EQUIVALENCE_MARGIN_PP
    assert result.detection.equivalent is False


def test_analysis_computes_paired_secondary_measures() -> None:
    responses = [
        _response(
            participant_id=participant,
            condition=condition,
            detected=True,
            confidence=0.9 if condition is Condition.RENDERED else 0.6,
            time_to_decision_s=15.0 if condition is Condition.RENDERED else 10.0,
        )
        for participant in range(MINIMUM_PARTICIPANTS)
        for condition in (Condition.RENDERED, Condition.RAW)
    ]
    result = analyze(responses)
    assert result.confidence.mean_diff == pytest.approx(0.3)
    assert result.time_to_decision.mean_diff == pytest.approx(5.0)
    assert result.calibration_gap.mean_diff == pytest.approx(0.3)
