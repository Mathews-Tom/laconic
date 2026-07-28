"""Gate protocol: threshold source, target/kill verdict logic, and the
gate suite's exit-code contract."""

from __future__ import annotations

import pytest

from laconic.gates.protocol import GateResult, GateSuiteResult, GateVerdict, evaluate
from laconic.gates.thresholds import THRESHOLDS, GateThreshold


def test_protocol_thresholds_match_the_published_table() -> None:
    """`docs/overview.md` §6.3 and `docs/system-design.md` §4, verbatim."""
    assert THRESHOLDS["K1"].target == 25.0
    assert THRESHOLDS["K1"].kill == 15.0
    assert THRESHOLDS["K2"].target == 95.0
    assert THRESHOLDS["K2"].kill == 90.0
    assert THRESHOLDS["K4"].target == 500.0
    assert THRESHOLDS["K4"].kill == 500.0
    assert THRESHOLDS["K5"].target == 2.0
    assert THRESHOLDS["K5"].kill == 2.0


def test_protocol_k3_carries_no_automated_threshold() -> None:
    assert "K3" not in THRESHOLDS


def test_protocol_gate_threshold_rejects_a_kill_above_target_for_at_least() -> None:
    with pytest.raises(ValueError, match="must not exceed target"):
        GateThreshold(
            gate="X", description="d", unit="%", direction="at_least", target=25.0, kill=30.0
        )


def test_protocol_gate_threshold_rejects_a_kill_below_target_for_at_most() -> None:
    with pytest.raises(ValueError, match="must not be below target"):
        GateThreshold(
            gate="X", description="d", unit="tokens", direction="at_most", target=500.0, kill=100.0
        )


# --- evaluate() -----------------------------------------------------------


def test_protocol_evaluate_at_least_passes_at_or_above_target() -> None:
    assert evaluate("K1", 25.0) is GateVerdict.PASS
    assert evaluate("K1", 40.0) is GateVerdict.PASS


def test_protocol_evaluate_at_least_fails_target_between_kill_and_target() -> None:
    assert evaluate("K1", 15.0) is GateVerdict.FAILED_TARGET
    assert evaluate("K1", 24.9) is GateVerdict.FAILED_TARGET


def test_protocol_evaluate_at_least_kills_below_kill() -> None:
    assert evaluate("K1", 14.9) is GateVerdict.KILL
    assert evaluate("K1", 0.0) is GateVerdict.KILL
    assert evaluate("K1", -5.0) is GateVerdict.KILL


def test_protocol_evaluate_at_most_passes_at_or_below_target() -> None:
    assert evaluate("K4", 500.0) is GateVerdict.PASS
    assert evaluate("K4", 0.0) is GateVerdict.PASS


def test_protocol_evaluate_at_most_kills_above_target_when_kill_equals_target() -> None:
    """K4 and K5 have no failed-but-not-killed zone: kill == target."""
    assert evaluate("K4", 500.01) is GateVerdict.KILL
    assert evaluate("K5", 2.01) is GateVerdict.KILL


def test_protocol_evaluate_unknown_gate_raises() -> None:
    with pytest.raises(KeyError):
        evaluate("K99", 1.0)


# --- GateResult -------------------------------------------------------


def test_protocol_gate_result_measured_derives_its_own_verdict() -> None:
    result = GateResult.measured("K1", 30.0, detail="corpus-wide")
    assert result.verdict is GateVerdict.PASS
    assert result.target == 25.0
    assert result.kill == 15.0
    assert result.unit == "%"


def test_protocol_gate_result_measured_cannot_disagree_with_evaluate() -> None:
    """A caller cannot hand-pick a verdict that contradicts the value --
    there is no `verdict=` parameter to `measured` at all."""
    result = GateResult.measured("K1", 10.0, detail="corpus-wide")
    assert result.verdict is GateVerdict.KILL


def test_protocol_gate_result_manual_has_no_value_or_threshold() -> None:
    result = GateResult.manual("K3", "Human bug-catch rate", detail="not evaluated")
    assert result.verdict is GateVerdict.MANUAL
    assert result.value is None
    assert result.target is None
    assert result.kill is None


def test_protocol_gate_result_to_json_round_trips_the_verdict_as_a_string() -> None:
    result = GateResult.measured("K2", 96.0, detail="d")
    payload = result.to_json()
    assert payload["verdict"] == "pass"
    assert payload["gate"] == "K2"


# --- GateSuiteResult ----------------------------------------------------


def test_protocol_suite_exit_code_is_zero_when_no_gate_kills() -> None:
    suite = GateSuiteResult(
        results=(
            GateResult.measured("K1", 30.0, detail="d"),
            GateResult.measured("K2", 18.0 + 77.0, detail="d"),  # 95.0, passes
            GateResult.manual("K3", "d", detail="d"),
        )
    )
    assert suite.exit_code == 0


def test_protocol_suite_exit_code_is_zero_for_a_failed_target_short_of_a_kill() -> None:
    suite = GateSuiteResult(results=(GateResult.measured("K1", 18.0, detail="d"),))
    assert suite.results[0].verdict is GateVerdict.FAILED_TARGET
    assert suite.exit_code == 0


def test_protocol_suite_exit_code_is_non_zero_when_any_gate_kills() -> None:
    suite = GateSuiteResult(
        results=(
            GateResult.measured("K1", 30.0, detail="d"),
            GateResult.measured("K4", 600.0, detail="d"),
        )
    )
    assert suite.exit_code == 1


def test_protocol_suite_to_json_lists_every_gate() -> None:
    suite = GateSuiteResult(results=(GateResult.measured("K1", 30.0, detail="d"),))
    payload = suite.to_json()
    assert isinstance(payload["gates"], list)
    assert len(payload["gates"]) == 1
