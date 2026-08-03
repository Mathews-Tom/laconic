"""Gate protocol: threshold source, target/kill verdict logic, and the
gate suite's exit-code contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from laconic.gates import action_equivalence, codec_overhead, net_cost, reasoning_accuracy
from laconic.gates.protocol import GateResult, GateSuiteResult, GateVerdict, evaluate
from laconic.gates.runner import UnknownGateError, run_gates
from laconic.gates.thresholds import THRESHOLDS, GateThreshold
from laconic.replay.corpus import EmptyCorpusError, JsonValue
from laconic.replay.engine import recorded_response_path


def test_protocol_thresholds_match_the_published_table() -> None:
    """`docs/overview.md` §6.3 and `docs/system-design.md` §4, verbatim."""
    assert THRESHOLDS["net-cost"].target == 25.0
    assert THRESHOLDS["net-cost"].kill == 15.0
    assert THRESHOLDS["action-equivalence"].target == 95.0
    assert THRESHOLDS["action-equivalence"].kill == 90.0
    assert THRESHOLDS["codec-overhead"].target == 500.0
    assert THRESHOLDS["codec-overhead"].kill == 500.0
    assert THRESHOLDS["reasoning-accuracy"].target == 2.0
    assert THRESHOLDS["reasoning-accuracy"].kill == 2.0


def test_protocol_k3_carries_no_automated_threshold() -> None:
    assert "human-bug-catch" not in THRESHOLDS


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
    assert evaluate("net-cost", 25.0) is GateVerdict.PASS
    assert evaluate("net-cost", 40.0) is GateVerdict.PASS


def test_protocol_evaluate_at_least_fails_target_between_kill_and_target() -> None:
    assert evaluate("net-cost", 15.0) is GateVerdict.FAILED_TARGET
    assert evaluate("net-cost", 24.9) is GateVerdict.FAILED_TARGET


def test_protocol_evaluate_at_least_kills_below_kill() -> None:
    assert evaluate("net-cost", 14.9) is GateVerdict.KILL
    assert evaluate("net-cost", 0.0) is GateVerdict.KILL
    assert evaluate("net-cost", -5.0) is GateVerdict.KILL


def test_protocol_evaluate_at_most_passes_at_or_below_target() -> None:
    assert evaluate("codec-overhead", 500.0) is GateVerdict.PASS
    assert evaluate("codec-overhead", 0.0) is GateVerdict.PASS


def test_protocol_evaluate_at_most_kills_above_target_when_kill_equals_target() -> None:
    """K4 and K5 have no failed-but-not-killed zone: kill == target."""
    assert evaluate("codec-overhead", 500.01) is GateVerdict.KILL
    assert evaluate("reasoning-accuracy", 2.01) is GateVerdict.KILL


def test_protocol_evaluate_unknown_gate_raises() -> None:
    with pytest.raises(KeyError):
        evaluate("K99", 1.0)


# --- GateResult -------------------------------------------------------


def test_protocol_gate_result_measured_derives_its_own_verdict() -> None:
    result = GateResult.measured("net-cost", 30.0, detail="corpus-wide")
    assert result.verdict is GateVerdict.PASS
    assert result.target == 25.0
    assert result.kill == 15.0
    assert result.unit == "%"


def test_protocol_gate_result_measured_cannot_disagree_with_evaluate() -> None:
    """A caller cannot hand-pick a verdict that contradicts the value --
    there is no `verdict=` parameter to `measured` at all."""
    result = GateResult.measured("net-cost", 10.0, detail="corpus-wide")
    assert result.verdict is GateVerdict.KILL


def test_protocol_gate_result_manual_has_no_value_or_threshold() -> None:
    result = GateResult.manual("human-bug-catch", "Human bug-catch rate", detail="not evaluated")
    assert result.verdict is GateVerdict.MANUAL
    assert result.value is None
    assert result.target is None
    assert result.kill is None


def test_protocol_gate_result_to_json_round_trips_the_verdict_as_a_string() -> None:
    result = GateResult.measured("action-equivalence", 96.0, detail="d")
    payload = result.to_json()
    assert payload["verdict"] == "pass"
    assert payload["gate"] == "action-equivalence"


# --- GateSuiteResult ----------------------------------------------------


def test_protocol_suite_exit_code_is_zero_when_no_gate_kills() -> None:
    suite = GateSuiteResult(
        results=(
            GateResult.measured("net-cost", 30.0, detail="d"),
            GateResult.measured("action-equivalence", 18.0 + 77.0, detail="d"),  # 95.0, passes
            GateResult.manual("human-bug-catch", "d", detail="d"),
        )
    )
    assert suite.exit_code == 0


def test_protocol_suite_exit_code_is_zero_for_a_failed_target_short_of_a_kill() -> None:
    suite = GateSuiteResult(results=(GateResult.measured("net-cost", 18.0, detail="d"),))
    assert suite.results[0].verdict is GateVerdict.FAILED_TARGET
    assert suite.exit_code == 0


def test_protocol_suite_exit_code_is_non_zero_when_any_gate_kills() -> None:
    suite = GateSuiteResult(
        results=(
            GateResult.measured("net-cost", 30.0, detail="d"),
            GateResult.measured("codec-overhead", 600.0, detail="d"),
        )
    )
    assert suite.exit_code == 1


def test_protocol_suite_to_json_lists_every_gate() -> None:
    suite = GateSuiteResult(results=(GateResult.measured("net-cost", 30.0, detail="d"),))
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
    result = net_cost.measure([CORPUS_DIR])
    assert result.gate == "net-cost"
    assert result.value == pytest.approx(8.53, abs=0.01)
    assert result.verdict is GateVerdict.KILL


def test_k2_measures_the_committed_corpus_at_full_equivalence() -> None:
    result = action_equivalence.measure([CORPUS_DIR])
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
    induced: bool = False,
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
    if induced:
        record["induced"] = True
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
    result = net_cost.measure([tmp_path])
    assert result.verdict is GateVerdict.PASS
    assert result.value is not None and result.value > 25.0


def test_k1_on_a_corpus_with_no_baseline_transcripts_reports_zero(tmp_path: Path) -> None:
    result = net_cost.measure([tmp_path])
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
    result = action_equivalence.measure([tmp_path])
    assert result.value == 0.0
    assert result.verdict is GateVerdict.KILL


# --- runner -----------------------------------------------------------


def test_runner_only_filters_which_gates_run() -> None:
    suite = run_gates([CORPUS_DIR], only=["net-cost"])
    assert [r.gate for r in suite.results] == ["net-cost"]


def test_runner_default_run_includes_every_registered_gate_plus_k3() -> None:
    suite = run_gates([CORPUS_DIR])
    assert {r.gate for r in suite.results} == {
        "net-cost",
        "action-equivalence",
        "human-bug-catch",
        "codec-overhead",
        "reasoning-accuracy",
    }


def test_runner_k3_is_always_manual() -> None:
    suite = run_gates([CORPUS_DIR], only=["human-bug-catch"])
    assert suite.results[0].verdict is GateVerdict.MANUAL
    assert suite.results[0].gate == "human-bug-catch"


def test_runner_rejects_an_unknown_gate_name() -> None:
    with pytest.raises(UnknownGateError, match="K99"):
        run_gates([CORPUS_DIR], only=["K99"])


def test_runner_exit_code_reflects_a_real_kill_on_the_committed_corpus() -> None:
    suite = run_gates([CORPUS_DIR], only=["net-cost", "action-equivalence"])
    assert suite.exit_code == 1


def test_runner_raises_on_a_corpus_with_no_baseline_transcripts(tmp_path: Path) -> None:
    """A typo'd or empty corpus path must not report K2's no-evidence
    early return as a green PASS -- a gate that is green because it saw
    nothing is worse than no gate at all."""
    with pytest.raises(EmptyCorpusError, match="no baseline transcripts"):
        run_gates([tmp_path])


# --- K4 ------------------------------------------------------------------


def test_k4_measures_the_committed_corpus_under_the_kill_threshold() -> None:
    result = codec_overhead.measure([CORPUS_DIR])
    assert result.gate == "codec-overhead"
    assert result.value is not None and 0.0 < result.value < 500.0
    assert result.verdict is GateVerdict.PASS


def test_k4_on_a_corpus_with_no_baseline_transcripts_reports_zero(tmp_path: Path) -> None:
    result = codec_overhead.measure([tmp_path])
    assert result.value == 0.0
    assert result.verdict is GateVerdict.PASS


def test_k4_reports_zero_overhead_when_encoding_only_shrinks_content(tmp_path: Path) -> None:
    big_read = "\n".join(f"def f_{i}(x): return x + {i}" for i in range(200))
    _write(
        tmp_path / "s.jsonl",
        [
            _assistant(tool_name="Read", tool_input={"path": "a.py"}, tool_use_id="t1"),
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": big_read}],
                },
            },
        ],
    )
    result = codec_overhead.measure([tmp_path])
    assert result.value == 0.0


def test_k4_detects_overhead_on_a_short_search_result_with_few_unique_paths(tmp_path: Path) -> None:
    """The exact `docs/overview.md` "Caveman net-negative trap" shape:
    interning two short, once-each paths costs more than the raw text."""
    tiny_grep = "a/b.py: ok\nc/d.py: ok"
    _write(
        tmp_path / "s.jsonl",
        [
            _assistant(tool_name="Grep", tool_input={"pattern": "x"}, tool_use_id="t1"),
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": tiny_grep}],
                },
            },
        ],
    )
    result = codec_overhead.measure([tmp_path])
    assert result.value is not None and result.value > 0.0


def test_k4_measures_overhead_for_tools_outside_file_command_search(tmp_path: Path) -> None:
    """K4 must measure every observation the real codec encodes, not a
    hand-picked subset: `Edit` results go through `FallbackEncoder` in
    production (`laconic.codec.observe.ObservationCodec.encode`), so K4
    must dispatch it too, not silently skip it as it did while filtered
    to `Read`/`Bash`/`Grep`/`Glob` alone."""
    _write(
        tmp_path / "s.jsonl",
        [
            _assistant(
                tool_name="Edit",
                tool_input={"path": "a.py", "old": "x", "new": "y"},
                tool_use_id="t1",
            ),
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "The file a.py has been updated.",
                        }
                    ],
                },
            },
        ],
    )
    result = codec_overhead.measure([tmp_path])
    assert result.detail is not None
    assert "1 of which invoked a tool this codec encoded" in result.detail


# --- K5 --------------------------------------------------------------


def test_k5_extract_items_derives_from_the_committed_corpus() -> None:
    items = reasoning_accuracy.extract_items([CORPUS_DIR])
    assert len(items) == 50
    assert all(item.expected_answer.isdigit() for item in items)
    assert len(items) == len({item.item_id for item in items})


def test_k5_extract_items_respects_the_limit() -> None:
    items = reasoning_accuracy.extract_items([CORPUS_DIR], limit=5)
    assert len(items) == 5


def test_k5_accuracy_computes_the_exact_match_rate() -> None:
    items = (
        reasoning_accuracy.ReasoningItem(item_id="a_0", question="q", expected_answer="0"),
        reasoning_accuracy.ReasoningItem(item_id="b_1", question="q", expected_answer="1"),
    )
    provenance = reasoning_accuracy.Provenance(source="recorded", model="m", captured_at="t")
    responses = (
        reasoning_accuracy.ReasoningResponse(
            item_id="a_0", condition="off", answer="0", provenance=provenance
        ),
        reasoning_accuracy.ReasoningResponse(
            item_id="b_1", condition="off", answer="WRONG", provenance=provenance
        ),
    )
    assert reasoning_accuracy.accuracy(items, responses, condition="off") == 50.0


def test_k5_accuracy_raises_when_a_condition_response_is_missing() -> None:
    items = (reasoning_accuracy.ReasoningItem(item_id="a_0", question="q", expected_answer="0"),)
    with pytest.raises(reasoning_accuracy.ReasoningAccuracyFixtureError, match="a_0"):
        reasoning_accuracy.accuracy(items, (), condition="off")


def test_k5_measures_the_committed_corpus_at_zero_delta() -> None:
    result = reasoning_accuracy.measure([CORPUS_DIR])
    assert result.value == 0.0
    assert result.verdict is GateVerdict.PASS


def test_reasoning_accuracy_requires_a_committed_fixture(tmp_path: Path) -> None:
    with pytest.raises(
        reasoning_accuracy.ReasoningAccuracyFixtureError,
        match="no committed reasoning-accuracy response fixture",
    ):
        reasoning_accuracy.load_responses(tmp_path / "missing.ndjson")


def test_k5_load_responses_requires_provenance(tmp_path: Path) -> None:
    path = tmp_path / "r.ndjson"
    path.write_text(json.dumps({"item_id": "a_0", "condition": "off", "answer": "0"}) + "\n")
    with pytest.raises(
        reasoning_accuracy.ReasoningAccuracyFixtureError, match="no `provenance` block"
    ):
        reasoning_accuracy.load_responses(path)


def test_k5_load_responses_raises_a_loud_error_for_unparseable_json(tmp_path: Path) -> None:
    path = tmp_path / "r.ndjson"
    path.write_text('{"item_id": "a_0", "condition": "off"\n')
    with pytest.raises(
        reasoning_accuracy.ReasoningAccuracyFixtureError, match=r"r\.ndjson:1: not valid JSON"
    ):
        reasoning_accuracy.load_responses(path)


def test_k5_load_responses_raises_a_loud_error_for_a_non_object_line(tmp_path: Path) -> None:
    path = tmp_path / "r.ndjson"
    path.write_text("[1, 2, 3]\n")
    with pytest.raises(
        reasoning_accuracy.ReasoningAccuracyFixtureError, match=r"r\.ndjson:1: malformed"
    ):
        reasoning_accuracy.load_responses(path)


def test_k5_load_responses_rejects_a_duplicate_item_condition_pair(tmp_path: Path) -> None:
    path = tmp_path / "r.ndjson"
    record = {"item_id": "a_0", "condition": "off", "answer": "0", "provenance": _provenance()}
    path.write_text(json.dumps(record) + "\n" + json.dumps(record) + "\n")
    with pytest.raises(
        reasoning_accuracy.ReasoningAccuracyFixtureError, match="duplicate response"
    ):
        reasoning_accuracy.load_responses(path)


def test_k5_responses_path_for_rejects_more_than_one_corpus_root(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(
        reasoning_accuracy.ReasoningAccuracyFixtureError, match="exactly one corpus root"
    ):
        reasoning_accuracy.responses_path_for([tmp_path, other])


def test_k5_write_responses_round_trips_through_load_responses(tmp_path: Path) -> None:
    items = (reasoning_accuracy.ReasoningItem(item_id="a_0", question="q", expected_answer="0"),)
    responses = reasoning_accuracy.generate_synthetic_responses(items, model="m")
    path = tmp_path / "r.ndjson"
    reasoning_accuracy.write_responses(path, responses)
    loaded = reasoning_accuracy.load_responses(path)
    assert loaded == responses


def test_k5_generate_synthetic_responses_answers_correctly_both_conditions() -> None:
    items = (reasoning_accuracy.ReasoningItem(item_id="a_0", question="q", expected_answer="7"),)
    responses = reasoning_accuracy.generate_synthetic_responses(items, model="m")
    assert reasoning_accuracy.accuracy(items, responses, condition="off") == 100.0
    assert reasoning_accuracy.accuracy(items, responses, condition="on") == 100.0
    assert all(r.provenance.source == "recorded" for r in responses)


class _FakeK5Client:
    def __init__(self, answers: dict[str, str]) -> None:
        self._answers = answers
        self.calls: list[str] = []

    def answer(self, *, item: reasoning_accuracy.ReasoningItem, context: str, model: str) -> str:
        self.calls.append(context)
        return self._answers[context]


def test_k5_capture_live_responses_tags_provenance_as_live() -> None:
    items = (reasoning_accuracy.ReasoningItem(item_id="a_0", question="q", expected_answer="7"),)
    client = _FakeK5Client({"raw-text": "7", "encoded-text": "7"})
    responses = reasoning_accuracy.capture_live_responses(
        items,
        contexts={"a_0": ("raw-text", "encoded-text")},
        client=client,
        model="m",
        run_id="run-1",
    )
    assert len(responses) == 2
    assert all(r.provenance.source == "live" for r in responses)
    assert all(r.provenance.run_id == "run-1" for r in responses)
    assert client.calls == ["raw-text", "encoded-text"]


# --- negative controls: every automated gate proven to kill ---------------
#
# CONSTRAINTS: "every gate needs a negative control proving it can fail."
# K2 already has one above (test_k2_reports_below_target_when_an_action_diverges)
# and K1's empty-corpus test already proves a kill verdict; these four are
# the deliberately-broken-codec scenarios matching each gate's own risk
# framing in docs/overview.md §6.3/§8.1, run through the real `measure()`
# entry point each gate's own CLI path uses -- not an internal helper.


def test_negative_control_k1_an_expensive_induced_read_can_erase_savings_into_a_kill(
    tmp_path: Path,
) -> None:
    """docs/overview.md §8.1's primary risk, reproduced: a real per-turn
    saving, entirely erased by one expensive induced follow-up read."""
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
            ),
            _assistant(
                tool_name="Read",
                tool_input={"path": "b.py"},
                tool_use_id="t2",
                cache_write=50_000,
                provenance=_provenance(),
                induced=True,
            ),
        ],
    )
    result = net_cost.measure([tmp_path])
    assert result.verdict is GateVerdict.KILL
    assert result.value is not None and result.value < 0.0


def test_negative_control_k4_a_pathological_search_result_can_reach_the_kill_threshold(
    tmp_path: Path,
) -> None:
    """400 short, unique, once-each paths: `SearchEncoder`'s legend costs
    far more than the raw hit list -- the Caveman net-negative trap taken
    to a scale that clears K4's 500-token kill threshold, not just a
    nonzero overhead."""
    tiny_hits = "\n".join(f"p{i}/f{i}.py: ok" for i in range(400))
    _write(
        tmp_path / "s.jsonl",
        [
            _assistant(tool_name="Grep", tool_input={"pattern": "x"}, tool_use_id="t1"),
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": tiny_hits}],
                },
            },
        ],
    )
    result = codec_overhead.measure([tmp_path])
    assert result.verdict is GateVerdict.KILL
    assert result.value is not None and result.value > 500.0


def test_negative_control_k5_a_wrong_codec_on_answer_can_reach_the_kill_threshold(
    tmp_path: Path,
) -> None:
    """format tax confirmed: the codec-on condition gets an item wrong
    the codec-off condition gets right, driving the accuracy delta past
    K5's 2pp kill threshold."""
    _write(
        tmp_path / "s.jsonl",
        [
            _assistant(tool_name="Read", tool_input={"path": "a.py"}, tool_use_id="t1"),
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "def emit_0(value: int) -> int:\n    return value + 0\n",
                        }
                    ],
                },
            },
        ],
    )
    items = reasoning_accuracy.extract_items([tmp_path])
    assert len(items) == 1
    responses_path = reasoning_accuracy.responses_path_for([tmp_path])
    _write(
        responses_path,
        [
            {
                "item_id": items[0].item_id,
                "condition": "off",
                "answer": "0",
                "provenance": _provenance(),
            },
            {
                "item_id": items[0].item_id,
                "condition": "on",
                "answer": "WRONG",
                "provenance": _provenance(),
            },
        ],
    )
    result = reasoning_accuracy.measure([tmp_path])
    assert result.verdict is GateVerdict.KILL
    assert result.value == 100.0
