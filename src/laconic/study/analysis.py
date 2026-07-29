"""Pre-registered K3 analysis: paired equivalence test plus secondary measures.

``docs/system-design.md`` §4.1's Analysis line: "Pre-registered; paired
comparison with the equivalence margin stated in advance rather than chosen
after seeing the data." ``DEVELOPMENT_PLAN.md`` §6 M14's acceptance line:
"the analysis script is committed before any real data is collected... with
its equivalence margin fixed in advance."

:data:`EQUIVALENCE_MARGIN_PP` is that fixed margin. It is a module-level
constant, sourced from the published K3 target in ``docs/overview.md`` §6.3
/ ``docs/system-design.md`` §4 ("within 5pp"), not invented for this module.
:func:`analyze` takes no margin parameter -- there is no call-site or CLI
flag anywhere in this package that can override it -- so applying this
analysis to real data can change its *result*, never its *rule*.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from statistics import NormalDist, fmean, stdev

from laconic.study.assignment import Condition
from laconic.study.capture import ResponseRecord

#: ``docs/overview.md`` §6.3 / ``docs/system-design.md`` §4's K3 row: target
#: "within 5pp", kill "worse by > 10pp". Fixed before any participant data
#: exists, confirmed unchanged at M14's design gate (H-31).
EQUIVALENCE_MARGIN_PP: float = 5.0

#: Two one-sided tests at alpha = 0.05 each is the standard TOST
#: (two one-sided tests) equivalence procedure via a 90% two-sided CI.
_TOST_CONFIDENCE = 0.90

#: The within-subjects design gives every participant one matched pair per
#: defect class, so a naive pair-level confidence interval would treat
#: correlated, non-independent observations from the same participant as
#: independent trials -- understating the true variance and inflating false
#: -equivalent declarations under a realistic participant-by-condition
#: interaction (some readers benefit from the rendered view more than
#: others). The primary measure is therefore analyzed at the participant
#: level: each participant contributes exactly one mean detection-rate
#: difference, aggregated across their defect-class pairs, and that
#: participant-level sample is what the confidence interval is computed
#: over. ``MINIMUM_PARTICIPANTS`` is the pre-registered floor below which a
#: normal-approximation confidence interval is not trustworthy -- fixed
#: before any participant data exists, alongside the margin itself.
MINIMUM_PARTICIPANTS = 10


class InsufficientDataError(ValueError):
    """Raised when a response set cannot be paired into a valid matched design."""


@dataclass(frozen=True, slots=True)
class PairedMeasure:
    """One secondary measure's paired (rendered - raw) mean difference."""

    label: str
    mean_diff: float
    n: int


@dataclass(frozen=True, slots=True)
class EquivalenceResult:
    """The primary measure: paired defect-detection rate, rendered vs raw."""

    rendered_rate: float
    raw_rate: float
    diff_pp: float
    margin_pp: float
    ci_low_pp: float
    ci_high_pp: float
    equivalent: bool


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Every statistic the pre-registered K3 analysis reports."""

    n_participants: int
    n_pairs: int
    detection: EquivalenceResult
    time_to_decision: PairedMeasure
    confidence: PairedMeasure
    calibration_gap: PairedMeasure


@dataclass(frozen=True, slots=True)
class _Pair:
    rendered: ResponseRecord
    raw: ResponseRecord


def _pair_responses(responses: Sequence[ResponseRecord]) -> tuple[_Pair, ...]:
    """Match each participant's rendered/raw response for the same defect class.

    Raises :class:`InsufficientDataError` for a missing or duplicated
    condition, or for zero matched pairs -- silently dropping an unmatched
    response would turn an assignment or capture bug into an unexplained
    sample-size discrepancy in the reported result instead of a loud
    failure at analysis time.
    """
    by_key: dict[tuple[int, str], dict[Condition, ResponseRecord]] = {}
    for response in responses:
        key = (response.participant_id, response.defect_class.value)
        slot = by_key.setdefault(key, {})
        if response.condition in slot:
            raise InsufficientDataError(
                f"duplicate {response.condition.value} response for participant "
                f"{response.participant_id}, defect class {response.defect_class.value}"
            )
        slot[response.condition] = response

    pairs: list[_Pair] = []
    for (participant_id, defect_class), slot in by_key.items():
        missing = {Condition.RENDERED, Condition.RAW} - slot.keys()
        if missing:
            missing_names = ", ".join(sorted(condition.value for condition in missing))
            raise InsufficientDataError(
                f"participant {participant_id}, defect class {defect_class} is missing "
                f"{missing_names} response(s)"
            )
        pairs.append(_Pair(rendered=slot[Condition.RENDERED], raw=slot[Condition.RAW]))
    if not pairs:
        raise InsufficientDataError("no matched rendered/raw response pairs to analyze")
    return tuple(pairs)


def _tost_ci(values: Sequence[float]) -> tuple[float, float]:
    """Two one-sided-tests 90% confidence interval for the mean of ``values``.

    A sample with zero observed variance (every unit shows an identical
    difference) still carries real finite-sample uncertainty -- with ``n``
    units, one dissenting unit would shift the mean by ``1/n``, so the
    variance floor here is ``(0.5 / n) ** 2``, half that resolution. Without
    this floor, a small perfectly-agreeing sample collapses to a zero-width
    interval and is judged equivalent unconditionally regardless of sample
    size, which is the wrong direction for a pre-registered equivalence
    gate to fail in.
    """
    n = len(values)
    mean = fmean(values)
    observed_sd = stdev(values) if n >= 2 else 0.0
    floor_sd = 0.5 / n
    standard_error = max(observed_sd, floor_sd) / (n**0.5)
    z = NormalDist().inv_cdf(0.5 + _TOST_CONFIDENCE / 2)
    margin = z * standard_error
    return mean - margin, mean + margin


def _paired_measure(
    label: str, pairs: Sequence[_Pair], extract: Callable[[ResponseRecord], float]
) -> PairedMeasure:
    diffs = [extract(pair.rendered) - extract(pair.raw) for pair in pairs]
    return PairedMeasure(label=label, mean_diff=fmean(diffs), n=len(diffs))


def analyze(responses: Sequence[ResponseRecord]) -> AnalysisResult:
    """Run the pre-registered K3 analysis over a matched-pair response set.

    The primary measure -- paired detection-rate difference (rendered -
    raw) -- is aggregated to one mean difference per participant (see
    :data:`MINIMUM_PARTICIPANTS`'s docstring for why) and tested for
    equivalence against :data:`EQUIVALENCE_MARGIN_PP` via a 90% two
    one-sided-tests confidence interval: the whole interval must sit within
    ``[-margin, +margin]`` for the two conditions to be judged equivalent.
    Raises :class:`InsufficientDataError` if fewer than
    :data:`MINIMUM_PARTICIPANTS` distinct participants are present -- a
    normal-approximation interval below that floor is not trustworthy
    enough to decide a pre-registered gate.

    Time-to-decision, confidence, and the calibration gap are reported as
    plain paired mean differences over every matched pair, without an
    equivalence claim -- the protocol (``docs/system-design.md`` §4.1)
    names them secondary measures, not gates, so no inferential claim is
    made about them here that would need the same participant-level
    correction.
    """
    pairs = _pair_responses(responses)
    diffs_by_participant: dict[int, list[float]] = {}
    for pair in pairs:
        diff = (1.0 if pair.rendered.detected else 0.0) - (1.0 if pair.raw.detected else 0.0)
        diffs_by_participant.setdefault(pair.rendered.participant_id, []).append(diff)

    n_participants = len(diffs_by_participant)
    if n_participants < MINIMUM_PARTICIPANTS:
        raise InsufficientDataError(
            f"at least {MINIMUM_PARTICIPANTS} participants are required for the "
            f"pre-registered equivalence test; found {n_participants}"
        )

    participant_mean_diffs = [fmean(diffs) for diffs in diffs_by_participant.values()]
    rendered_rate = fmean(1.0 if pair.rendered.detected else 0.0 for pair in pairs)
    raw_rate = fmean(1.0 if pair.raw.detected else 0.0 for pair in pairs)
    ci_low, ci_high = _tost_ci(participant_mean_diffs)
    diff_pp = fmean(participant_mean_diffs) * 100.0
    ci_low_pp, ci_high_pp = ci_low * 100.0, ci_high * 100.0
    equivalent = ci_low_pp >= -EQUIVALENCE_MARGIN_PP and ci_high_pp <= EQUIVALENCE_MARGIN_PP

    detection = EquivalenceResult(
        rendered_rate=rendered_rate,
        raw_rate=raw_rate,
        diff_pp=diff_pp,
        margin_pp=EQUIVALENCE_MARGIN_PP,
        ci_low_pp=ci_low_pp,
        ci_high_pp=ci_high_pp,
        equivalent=equivalent,
    )

    return AnalysisResult(
        n_participants=n_participants,
        n_pairs=len(pairs),
        detection=detection,
        time_to_decision=_paired_measure(
            "time_to_decision_s", pairs, lambda response: response.time_to_decision_s
        ),
        confidence=_paired_measure("confidence", pairs, lambda response: response.confidence),
        calibration_gap=_paired_measure(
            "calibration_gap", pairs, lambda response: response.calibration_gap
        ),
    )
