"""Gate protocol: threshold source, target/kill verdict logic, and the
gate suite's exit-code contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from laconic.gates import k1, k2
from laconic.gates.protocol import GateResult, GateSuiteResult, GateVerdict, evaluate
from laconic.gates.runner import UnknownGateError, run_gates
from laconic.gates.thresholds import THRESHOLDS, GateThreshold
from laconic.replay.corpus import EmptyCorpusError, JsonValue
from laconic.replay.engine import recorded_response_path


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


# --- K1 / K2 against the real committed corpus ---------------------------

CORPUS_DIR = Path(__file__).parent / "corpus"


def test_k1_measures_the_committed_corpus_below_the_kill_threshold() -> None:
    """The fixture corpus's real, measured K1 -- see
    `.docs/DEVELOPMENT_PLAN_HISTORY.md` H-25: savings are concentrated in
    five whale reads out of 125 turns, and the honest number is a kill,
    not a pass. This pins that number so a change to the committed
    fixtures or the measurement itself is caught."""
    result = k1.measure([CORPUS_DIR])
    assert result.gate == "K1"
    assert result.value == pytest.approx(8.53, abs=0.01)
    assert result.verdict is GateVerdict.KILL


def test_k2_measures_the_committed_corpus_at_full_equivalence() -> None:
    result = k2.measure([CORPUS_DIR])
    assert result.value == pytest.approx(100.0)
    assert result.verdict is GateVerdict.PASS


# --- K1 / K2 on small hand-built fixtures ---------------------------------


def _usage(*, cache_write: int = 200, cache_read: int = 1_000) -> dict[str, JsonValue]:
    return {
        "input_tokens": 10,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
        "output_tokens": 100,
    }


def _assistant(
    *,
    tool_name: str,
    tool_input: dict[str, JsonValue],
    tool_use_id: str = "t1",
    cache_write: int = 200,
    cache_read: int = 1_000,
    provenance: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    record: dict[str, JsonValue] = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [
                {"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": tool_input}
            ],
            "usage": _usage(cache_write=cache_write, cache_read=cache_read),
        },
    }
    if provenance is not None:
        record["provenance"] = provenance
    return record


def _provenance() -> dict[str, JsonValue]:
    return {"source": "recorded", "model": "claude-sonnet-5", "captured_at": "2026-07-28T00:00:00Z"}


def _write(path: Path, records: list[dict[str, JsonValue]]) -> Path:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


def test_k1_passes_on_a_fixture_with_real_net_savings(tmp_path: Path) -> None:
    baseline = _write(
        tmp_path / "s.jsonl",
        [
            _assistant(
                tool_name="Edit",
                tool_input={"path": "a.py", "old": "x", "new": "y"},
                cache_write=10_000,
            )
        ],
    )

    _write(
        recorded_response_path(baseline),
        [
            _assistant(
                tool_name="Edit",
                tool_input={"path": "a.py", "old": "x", "new": "y"},
                cache_write=1_000,
                provenance=_provenance(),
            )
        ],
    )
    result = k1.measure([tmp_path])
    assert result.verdict is GateVerdict.PASS
    assert result.value is not None and result.value > 25.0


def test_k1_on_a_corpus_with_no_baseline_transcripts_reports_zero(tmp_path: Path) -> None:
    result = k1.measure([tmp_path])
    assert result.value == 0.0
    assert result.verdict is GateVerdict.KILL
    assert "no baseline transcripts" in result.detail


def test_k2_reports_below_target_when_an_action_diverges(tmp_path: Path) -> None:
    baseline = _write(
        tmp_path / "s.jsonl",
        [_assistant(tool_name="Edit", tool_input={"path": "a.py", "old": "x", "new": "y"})],
    )
    _write(
        recorded_response_path(baseline),
        [
            _assistant(
                tool_name="Edit",
                tool_input={"path": "a.py", "old": "DIFFERENT", "new": "y"},
                provenance=_provenance(),
            )
        ],
    )
    result = k2.measure([tmp_path])
    assert result.value == 0.0
    assert result.verdict is GateVerdict.KILL


# --- runner -----------------------------------------------------------


def test_runner_only_filters_which_gates_run() -> None:
    suite = run_gates([CORPUS_DIR], only=["K1"])
    assert [r.gate for r in suite.results] == ["K1"]


def test_runner_default_run_includes_k1_k2_and_k3() -> None:
    suite = run_gates([CORPUS_DIR])
    assert {r.gate for r in suite.results} >= {"K1", "K2", "K3"}


def test_runner_k3_is_always_manual() -> None:
    suite = run_gates([CORPUS_DIR], only=["K3"])
    assert suite.results[0].verdict is GateVerdict.MANUAL
    assert suite.results[0].gate == "K3"


def test_runner_rejects_an_unknown_gate_name() -> None:
    with pytest.raises(UnknownGateError, match="K99"):
        run_gates([CORPUS_DIR], only=["K99"])


def test_runner_exit_code_reflects_a_real_kill_on_the_committed_corpus() -> None:
    suite = run_gates([CORPUS_DIR], only=["K1", "K2"])
    assert suite.exit_code == 1


def test_runner_raises_on_a_corpus_with_no_baseline_transcripts(tmp_path: Path) -> None:
    """A typo'd or empty corpus path must not report K2's no-evidence
    early return as a green PASS -- a gate that is green because it saw
    nothing is worse than no gate at all."""
    with pytest.raises(EmptyCorpusError, match="no baseline transcripts"):
        run_gates([tmp_path])
