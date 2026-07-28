"""Replay engine: turn iteration, baseline reproduction, and recorded and
live replay of the codec's counterfactual behaviour."""

from __future__ import annotations

import json
from dataclasses import fields, replace
from pathlib import Path

import pytest

from laconic.costs import CostBreakdown, ModelUsage
from laconic.replay.corpus import JsonValue, MalformedRecordError, Record
from laconic.replay.engine import (
    BASELINE_TOLERANCE_USD,
    BaselineMismatchError,
    BaselineSession,
    CostCapExceededError,
    LiveModeConfigError,
    LiveReplayConfig,
    MismatchedSessionError,
    MissingRecordedResponseError,
    NetCostReport,
    Provenance,
    RecordedAction,
    RecordedResponseSession,
    ReplayTurn,
    ReplayTurnCapture,
    SessionCost,
    TurnUsage,
    UnknownTurnIndexError,
    assert_baseline,
    iter_turns,
    load_recorded_response,
    net_cost,
    recorded_response_path,
    replay_live,
    replay_off,
    replay_on,
    session_cost_of,
)
from laconic.replay.equivalence import (
    EquivalenceVerdict,
    StructuralComparison,
    compare,
    compare_session,
)
from laconic.replay.judge import Judge, JudgeConfig, JudgeConfigError, JudgeVerdict


def _usage(*, output_tokens: int = 100) -> dict[str, JsonValue]:
    return {
        "input_tokens": 10,
        "cache_read_input_tokens": 1_000,
        "cache_creation_input_tokens": 200,
        "output_tokens": output_tokens,
    }


def _assistant(
    *,
    model: str = "claude-sonnet-5",
    tool_name: str | None = None,
    tool_input: dict[str, JsonValue] | None = None,
    tool_use_id: str = "toolu_1",
    with_usage: bool = True,
    induced: bool = False,
    provenance: dict[str, JsonValue] | None = None,
) -> Record:
    content: list[JsonValue] = []
    if tool_name is not None:
        content.append(
            {"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": tool_input or {}}
        )
    message: dict[str, JsonValue] = {"role": "assistant", "model": model, "content": content}
    if with_usage:
        message["usage"] = _usage()
    record: dict[str, JsonValue] = {"type": "assistant", "message": message}
    if induced:
        record["induced"] = True
    if provenance is not None:
        record["provenance"] = provenance
    return record


def _tool_result(tool_use_id: str, content: str) -> Record:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}],
        },
    }


def _write(path: Path, records: list[Record]) -> Path:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


def _provenance(source: str = "recorded", model: str = "claude-sonnet-5") -> dict[str, JsonValue]:
    return {"source": source, "model": model, "captured_at": "2026-07-28T00:00:00Z"}


# --- iter_turns -------------------------------------------------------


def test_iter_turns_reads_actions_usage_and_index_in_order(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s.jsonl",
        [
            _assistant(tool_name="Read", tool_input={"path": "a.py"}, tool_use_id="t1"),
            _tool_result("t1", "raw"),
            _assistant(
                tool_name="Edit",
                tool_input={"path": "a.py", "old": "x", "new": "y"},
                tool_use_id="t2",
            ),
        ],
    )
    turns = list(iter_turns(path))
    assert [turn.index for turn in turns] == [0, 1]
    assert turns[0].actions == (
        RecordedAction(tool_use_id="t1", tool_name="Read", tool_input={"path": "a.py"}),
    )
    assert turns[0].usage == TurnUsage(
        model="claude-sonnet-5",
        input_tokens=10,
        cache_read=1_000,
        cache_write=200,
        output_tokens=100,
    )
    assert turns[1].actions[0].tool_name == "Edit"


def test_iter_turns_marks_induced_and_provenance(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s.jsonl",
        [
            _assistant(
                tool_name="Read",
                tool_input={"path": "a.py"},
                induced=True,
                provenance=_provenance(),
            )
        ],
    )
    (turn,) = list(iter_turns(path))
    assert turn.induced is True
    assert turn.provenance == Provenance(
        source="recorded", model="claude-sonnet-5", captured_at="2026-07-28T00:00:00Z"
    )


def test_iter_turns_leaves_a_baseline_turn_unprovenanced(tmp_path: Path) -> None:
    path = _write(tmp_path / "s.jsonl", [_assistant(tool_name="Read", tool_input={"path": "a.py"})])
    (turn,) = list(iter_turns(path))
    assert turn.provenance is None
    assert turn.induced is False


def test_iter_turns_skips_a_turn_with_no_usage(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s.jsonl",
        [_assistant(tool_name="Read", tool_input={"path": "a.py"}, with_usage=False)],
    )
    (turn,) = list(iter_turns(path))
    assert turn.usage is None


def test_iter_turns_raises_on_a_malformed_usage_block(tmp_path: Path) -> None:
    """A non-object `usage` must raise, matching `laconic.replay.corpus.scan`'s
    identical check -- silently treating it as "no usage" would drop the
    turn's cost from a recorded-response fixture's own total and inflate
    reported net savings."""
    path = _write(
        tmp_path / "s.jsonl",
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-5",
                    "content": [],
                    "usage": "oops",
                },
            }
        ],
    )
    with pytest.raises(MalformedRecordError, match="usage is not an object"):
        list(iter_turns(path))


def test_provenance_from_record_rejects_an_unknown_source() -> None:
    assert Provenance.from_record({"source": "bogus", "model": "m", "captured_at": "t"}) is None


def test_provenance_round_trips_through_to_record() -> None:
    provenance = Provenance(
        source="live", model="claude-sonnet-5", captured_at="2026-07-28T00:00:00Z", run_id="r1"
    )
    assert Provenance.from_record(provenance.to_record()) == provenance


def test_recorded_response_path_uses_the_documented_suffix() -> None:
    assert recorded_response_path(Path("tests/corpus/session-a.jsonl")) == Path(
        "tests/corpus/session-a.codec-on.jsonl"
    )


def test_recorded_response_path_rejects_a_non_transcript_path() -> None:
    with pytest.raises(ValueError, match="not a transcript path"):
        recorded_response_path(Path("tests/corpus/session-a.txt"))


# --- codec="off" baseline reproduction ---------------------------------


def test_replay_off_reproduces_the_transcripts_own_recorded_cost(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s.jsonl",
        [
            _assistant(tool_name="Read", tool_input={"path": "a.py"}, tool_use_id="t1"),
            _tool_result("t1", "raw"),
            _assistant(
                tool_name="Edit",
                tool_input={"path": "a.py", "old": "x", "new": "y"},
                tool_use_id="t2",
            ),
        ],
    )
    (session,) = replay_off([tmp_path])
    assert session.path == path
    assert session.cost.turns == 2
    reference = session_cost_of(path).cost.total
    assert session.cost.cost.total == pytest.approx(reference, abs=BASELINE_TOLERANCE_USD)
    assert_baseline([session])


def test_replay_off_does_not_double_count_a_committed_recorded_response_fixture(
    tmp_path: Path,
) -> None:
    """A fixture sitting beside its baseline is still a ``*.jsonl`` file --
    ``find_transcripts`` alone would treat it as a second baseline
    session. It must not be counted here."""
    baseline = _write(
        tmp_path / "s.jsonl", [_assistant(tool_name="Read", tool_input={"path": "a.py"})]
    )
    _write(
        recorded_response_path(baseline),
        [_assistant(tool_name="Read", tool_input={"path": "a.py"})],
    )
    sessions = replay_off([tmp_path])
    assert [session.path for session in sessions] == [baseline]


def test_assert_baseline_accepts_multiple_sessions(tmp_path: Path) -> None:
    _write(tmp_path / "a.jsonl", [_assistant(tool_name="Read", tool_input={"path": "a.py"})])
    _write(tmp_path / "b.jsonl", [_assistant(tool_name="Read", tool_input={"path": "b.py"})])
    sessions = replay_off([tmp_path])
    assert len(sessions) == 2
    assert_baseline(sessions)


def test_assert_baseline_rejects_a_fabricated_mismatch(tmp_path: Path) -> None:
    path = _write(tmp_path / "s.jsonl", [_assistant(tool_name="Read", tool_input={"path": "a.py"})])
    real = session_cost_of(path)
    wrong = SessionCost(
        turns=real.turns,
        cost=replace(real.cost, output=real.cost.output + 1.0),
    )
    with pytest.raises(BaselineMismatchError, match="does not reproduce"):
        assert_baseline([BaselineSession(path=path, cost=wrong)])


# --- recorded-response fixture loading ----------------------------------


def test_load_recorded_response_requires_a_committed_fixture(tmp_path: Path) -> None:
    baseline = _write(
        tmp_path / "s.jsonl", [_assistant(tool_name="Read", tool_input={"path": "a.py"})]
    )
    with pytest.raises(
        MissingRecordedResponseError, match="no committed recorded-response fixture"
    ):
        load_recorded_response(baseline)


def test_load_recorded_response_requires_provenance_on_every_turn(tmp_path: Path) -> None:
    baseline = _write(
        tmp_path / "s.jsonl", [_assistant(tool_name="Read", tool_input={"path": "a.py"})]
    )
    _write(
        recorded_response_path(baseline),
        [_assistant(tool_name="Read", tool_input={"path": "a.py"})],
    )
    with pytest.raises(MissingRecordedResponseError, match="carries no `provenance` block"):
        load_recorded_response(baseline)


def test_load_recorded_response_pairs_non_induced_turns_with_the_baseline(tmp_path: Path) -> None:
    baseline = _write(
        tmp_path / "s.jsonl",
        [
            _assistant(
                tool_name="Edit",
                tool_input={"path": "a.py", "old": "x", "new": "y"},
                tool_use_id="t1",
            )
        ],
    )
    _write(
        recorded_response_path(baseline),
        [
            _assistant(
                tool_name="Read",
                tool_input={"path": "a.py"},
                tool_use_id="r1",
                induced=True,
                provenance=_provenance(),
            ),
            _assistant(
                tool_name="Edit",
                tool_input={"path": "a.py", "old": "x", "new": "y"},
                tool_use_id="r2",
                provenance=_provenance(),
            ),
        ],
    )
    session = load_recorded_response(baseline)
    assert session.baseline == baseline
    assert len(session.induced_turns) == 1
    assert session.induced_turns[0].actions[0].tool_name == "Read"
    assert session.non_induced_actions == (
        RecordedAction(
            tool_use_id="r2", tool_name="Edit", tool_input={"path": "a.py", "old": "x", "new": "y"}
        ),
    )


def test_recorded_response_session_cost_includes_induced_turns(tmp_path: Path) -> None:
    baseline = _write(
        tmp_path / "s.jsonl", [_assistant(tool_name="Edit", tool_input={"path": "a.py"})]
    )
    fixture = recorded_response_path(baseline)
    _write(
        fixture,
        [
            _assistant(
                tool_name="Read",
                induced=True,
                provenance=_provenance(),
                tool_input={"path": "a.py"},
            ),
            _assistant(tool_name="Edit", tool_input={"path": "a.py"}, provenance=_provenance()),
        ],
    )
    session = load_recorded_response(baseline)
    assert session.cost.turns == 2
    both_turns_cost = sum(session_cost_of(fixture).cost.total for _ in range(1))
    assert session.cost.cost.total == pytest.approx(both_turns_cost, abs=BASELINE_TOLERANCE_USD)


# --- live replay ---------------------------------------------------------


class _FakeClient:
    """Returns a scripted sequence of turn-captures, one call's worth per
    ``respond`` invocation, consumed in order."""

    def __init__(self, scripted: list[list[ReplayTurnCapture]]) -> None:
        self._scripted = scripted
        self.calls: list[tuple[str, str]] = []

    def respond(self, *, prefix: object, observation: str, model: str) -> list[ReplayTurnCapture]:
        self.calls.append((observation, model))
        return self._scripted.pop(0)


def _capture(
    tool_name: str, tool_input: dict[str, JsonValue], *, tool_use_id: str = "c1"
) -> ReplayTurnCapture:
    return ReplayTurnCapture(
        action=RecordedAction(tool_use_id=tool_use_id, tool_name=tool_name, tool_input=tool_input),
        usage=TurnUsage(
            model="claude-sonnet-5",
            input_tokens=10,
            cache_read=100,
            cache_write=50,
            output_tokens=20,
        ),
    )


def test_live_replay_config_rejects_an_empty_model() -> None:
    with pytest.raises(LiveModeConfigError, match="model identifier"):
        LiveReplayConfig(model="", cost_cap_usd=1.0, client=_FakeClient([]))


def test_live_replay_config_rejects_a_non_positive_cost_cap() -> None:
    with pytest.raises(LiveModeConfigError, match="cost cap must be positive"):
        LiveReplayConfig(model="claude-sonnet-5", cost_cap_usd=0.0, client=_FakeClient([]))


def test_replay_live_writes_a_provenance_tagged_artifact(tmp_path: Path) -> None:
    baseline = _write(
        tmp_path / "s.jsonl", [_assistant(tool_name="Edit", tool_input={"path": "a.py"})]
    )
    client = _FakeClient([[_capture("Edit", {"path": "a.py"})]])
    config = LiveReplayConfig(model="claude-sonnet-5", cost_cap_usd=10.0, client=client)
    artifact = tmp_path / "artifact.jsonl"
    session = replay_live(
        baseline,
        config,
        artifact_path=artifact,
        observations={0: "encoded observation"},
        run_id="run-1",
    )
    assert client.calls == [("encoded observation", "claude-sonnet-5")]
    assert artifact.is_file()
    lines = [json.loads(line) for line in artifact.read_text().splitlines()]
    assert lines[0]["provenance"] == {
        "source": "live",
        "model": "claude-sonnet-5",
        "captured_at": lines[0]["provenance"]["captured_at"],
        "run_id": "run-1",
    }
    assert lines[0]["induced"] is False
    assert session.turns[0].actions[0].tool_name == "Edit"


def test_replay_live_marks_every_capture_but_the_last_as_induced(tmp_path: Path) -> None:
    baseline = _write(
        tmp_path / "s.jsonl", [_assistant(tool_name="Edit", tool_input={"path": "a.py"})]
    )
    client = _FakeClient(
        [
            [
                _capture("Read", {"path": "a.py"}, tool_use_id="c1"),
                _capture("Edit", {"path": "a.py"}, tool_use_id="c2"),
            ]
        ]
    )
    config = LiveReplayConfig(model="claude-sonnet-5", cost_cap_usd=10.0, client=client)
    session = replay_live(
        baseline, config, artifact_path=tmp_path / "artifact.jsonl", observations={0: "obs"}
    )
    assert [turn.induced for turn in session.turns] == [True, False]
    assert [turn.actions[0].tool_name for turn in session.turns] == ["Read", "Edit"]


def test_replay_live_stops_and_preserves_partial_output_past_the_cost_cap(tmp_path: Path) -> None:
    baseline = _write(
        tmp_path / "s.jsonl",
        [
            _assistant(tool_name="Edit", tool_input={"path": "a.py"}),
            _assistant(tool_name="Edit", tool_input={"path": "b.py"}),
        ],
    )
    client = _FakeClient(
        [[_capture("Edit", {"path": "a.py"})], [_capture("Edit", {"path": "b.py"})]]
    )
    config = LiveReplayConfig(model="claude-sonnet-5", cost_cap_usd=1e-6, client=client)
    artifact = tmp_path / "artifact.jsonl"
    with pytest.raises(CostCapExceededError, match="partial results captured"):
        replay_live(baseline, config, artifact_path=artifact, observations={0: "obs-0", 1: "obs-1"})
    lines = artifact.read_text().splitlines()
    assert len(lines) == 1


def test_replay_live_rejects_an_observation_index_the_baseline_never_had(tmp_path: Path) -> None:
    baseline = _write(
        tmp_path / "s.jsonl", [_assistant(tool_name="Edit", tool_input={"path": "a.py"})]
    )
    config = LiveReplayConfig(model="claude-sonnet-5", cost_cap_usd=1.0, client=_FakeClient([]))
    with pytest.raises(UnknownTurnIndexError, match="no turn"):
        replay_live(
            baseline, config, artifact_path=tmp_path / "artifact.jsonl", observations={5: "obs"}
        )


def test_replay_live_validates_every_index_before_calling_the_client_or_writing(
    tmp_path: Path,
) -> None:
    """A bad index behind a good one must never spend a real call, and a
    pre-existing artifact from a previous run must survive untouched --
    validation runs against the whole `observations` map before the
    artifact file is ever opened."""
    baseline = _write(
        tmp_path / "s.jsonl",
        [
            _assistant(tool_name="Edit", tool_input={"path": "a.py"}),
            _assistant(tool_name="Edit", tool_input={"path": "b.py"}),
        ],
    )
    client = _FakeClient([[_capture("Edit", {"path": "a.py"})]])
    config = LiveReplayConfig(model="claude-sonnet-5", cost_cap_usd=10.0, client=client)
    artifact = tmp_path / "artifact.jsonl"
    artifact.write_text("previous run's committable data\n")
    with pytest.raises(UnknownTurnIndexError):
        replay_live(
            baseline, config, artifact_path=artifact, observations={0: "obs-0", 99: "obs-99"}
        )
    assert client.calls == []
    assert artifact.read_text() == "previous run's committable data\n"


# --- net cost accounting --------------------------------------------------


def _turn_usage(*, cache_write: int = 200, output_tokens: int = 100) -> TurnUsage:
    return TurnUsage(
        model="claude-sonnet-5",
        input_tokens=10,
        cache_read=1_000,
        cache_write=cache_write,
        output_tokens=output_tokens,
    )


def _replay_turn(index: int, *, usage: TurnUsage | None, induced: bool = False) -> ReplayTurn:
    action = RecordedAction(tool_use_id=f"t{index}", tool_name="Edit", tool_input={"path": "a.py"})
    return ReplayTurn(
        index=index, actions=(action,), usage=usage, induced=induced, provenance=_provenance_obj()
    )


def _provenance_obj() -> Provenance:
    return Provenance(
        source="recorded", model="claude-sonnet-5", captured_at="2026-07-28T00:00:00Z"
    )


def test_net_cost_is_the_difference_between_baseline_and_codec_on_totals(tmp_path: Path) -> None:
    baseline = _write(
        tmp_path / "s.jsonl", [_assistant(tool_name="Edit", tool_input={"path": "a.py"})]
    )
    cheaper_turn = _replay_turn(0, usage=_turn_usage(cache_write=50))
    session = RecordedResponseSession(
        baseline=baseline, fixture=tmp_path / "fixture.jsonl", turns=(cheaper_turn,)
    )
    report = net_cost(baseline, session)
    assert report.induced_turns == 0
    assert report.induced_cost_usd == 0.0
    # Independently derived expectation, not report's own stored fields.
    expected_baseline = session_cost_of(baseline).cost.total
    assert cheaper_turn.usage is not None
    expected_codec_usage = ModelUsage().add_turn(
        input_tokens=cheaper_turn.usage.input_tokens,
        cache_read=cheaper_turn.usage.cache_read,
        cache_write=cheaper_turn.usage.cache_write,
        output_tokens=cheaper_turn.usage.output_tokens,
    )
    expected_codec_on = expected_codec_usage.cost(cheaper_turn.usage.model).total
    assert report.net_savings_usd == pytest.approx(expected_baseline - expected_codec_on)
    assert report.net_savings_usd > 0.0


def test_net_cost_rejects_a_mismatched_baseline_and_session(tmp_path: Path) -> None:
    baseline_a = _write(
        tmp_path / "a.jsonl", [_assistant(tool_name="Edit", tool_input={"path": "a.py"})]
    )
    baseline_b = _write(
        tmp_path / "b.jsonl", [_assistant(tool_name="Edit", tool_input={"path": "b.py"})]
    )
    session_for_b = RecordedResponseSession(
        baseline=baseline_b,
        fixture=tmp_path / "fixture.jsonl",
        turns=(_replay_turn(0, usage=_turn_usage()),),
    )
    with pytest.raises(MismatchedSessionError, match="does not match"):
        net_cost(baseline_a, session_for_b)


def test_net_cost_nets_out_induced_turn_cost(tmp_path: Path) -> None:
    """An extra induced turn must reduce reported savings, not just be
    tallied alongside them -- proving the netting actually subtracts."""
    baseline = _write(
        tmp_path / "s.jsonl", [_assistant(tool_name="Edit", tool_input={"path": "a.py"})]
    )
    cheaper_turn = _replay_turn(0, usage=_turn_usage(cache_write=50))
    without_induced = RecordedResponseSession(
        baseline=baseline, fixture=tmp_path / "f1.jsonl", turns=(cheaper_turn,)
    )
    induced_turn = _replay_turn(1, usage=_turn_usage(cache_write=200), induced=True)
    with_induced = RecordedResponseSession(
        baseline=baseline, fixture=tmp_path / "f2.jsonl", turns=(cheaper_turn, induced_turn)
    )
    savings_without = net_cost(baseline, without_induced).net_savings_usd
    report_with = net_cost(baseline, with_induced)
    assert report_with.induced_turns == 1
    assert report_with.induced_cost_usd > 0.0
    assert report_with.net_savings_usd < savings_without
    assert report_with.net_savings_usd == pytest.approx(
        savings_without - report_with.induced_cost_usd
    )


def test_net_cost_report_exposes_no_gross_only_field() -> None:
    """Structural guard for `docs/system-design.md`'s "gross-only reporting
    is unrepresentable" constraint: the only savings-shaped attribute this
    type carries is net."""
    field_names = {f.name for f in fields(NetCostReport)}
    assert field_names == {"baseline", "codec_on", "induced_turns", "induced_cost_usd"}
    property_names = {
        name for name in dir(NetCostReport) if isinstance(getattr(NetCostReport, name), property)
    }
    savings_properties = {name for name in property_names if "savings" in name}
    assert savings_properties == {"net_savings_usd", "net_savings_pct"}


def test_net_savings_pct_is_zero_for_a_zero_cost_baseline() -> None:
    zero = SessionCost(turns=0, cost=CostBreakdown())
    report = NetCostReport(baseline=zero, codec_on=zero, induced_turns=0, induced_cost_usd=0.0)
    assert report.net_savings_pct == 0.0


def test_replay_on_aggregates_every_baseline_with_a_committed_fixture(tmp_path: Path) -> None:
    baseline_a = _write(
        tmp_path / "a.jsonl", [_assistant(tool_name="Edit", tool_input={"path": "a.py"})]
    )
    baseline_b = _write(
        tmp_path / "b.jsonl", [_assistant(tool_name="Edit", tool_input={"path": "b.py"})]
    )
    for baseline in (baseline_a, baseline_b):
        _write(
            recorded_response_path(baseline),
            [_assistant(tool_name="Edit", tool_input={"path": "a.py"}, provenance=_provenance())],
        )
    reports = replay_on([tmp_path])
    assert {path for path, _ in reports} == {baseline_a, baseline_b}
    assert all(isinstance(report, NetCostReport) for _, report in reports)


def test_replay_on_raises_loudly_when_any_baseline_lacks_a_fixture(tmp_path: Path) -> None:
    with_fixture = _write(
        tmp_path / "a.jsonl", [_assistant(tool_name="Edit", tool_input={"path": "a.py"})]
    )
    _write(
        recorded_response_path(with_fixture),
        [_assistant(tool_name="Edit", tool_input={"path": "a.py"}, provenance=_provenance())],
    )
    _write(tmp_path / "b.jsonl", [_assistant(tool_name="Edit", tool_input={"path": "b.py"})])
    with pytest.raises(MissingRecordedResponseError):
        replay_on([tmp_path])


# --- structural equivalence ------------------------------------------------


def test_equivalence_agrees_on_tool_target_and_anchor() -> None:
    recorded = RecordedAction(
        tool_use_id="t1", tool_name="Edit", tool_input={"path": "a.py", "old": "x", "new": "y"}
    )
    proposed = RecordedAction(
        tool_use_id="t2", tool_name="Edit", tool_input={"path": "a.py", "old": "x", "new": "z"}
    )
    result = compare(recorded, proposed)
    assert result.verdict is EquivalenceVerdict.EQUIVALENT
    assert result.is_equivalent


def test_equivalence_diverges_on_a_different_tool() -> None:
    recorded = RecordedAction(
        tool_use_id="t1", tool_name="Edit", tool_input={"path": "a.py", "old": "x", "new": "y"}
    )
    proposed = RecordedAction(tool_use_id="t2", tool_name="Read", tool_input={"path": "a.py"})
    result = compare(recorded, proposed)
    assert result.verdict is EquivalenceVerdict.DIVERGENT
    assert "tool differs" in result.reason


def test_equivalence_diverges_on_a_different_target_file() -> None:
    recorded = RecordedAction(
        tool_use_id="t1", tool_name="Edit", tool_input={"path": "a.py", "old": "x", "new": "y"}
    )
    proposed = RecordedAction(
        tool_use_id="t2", tool_name="Edit", tool_input={"path": "b.py", "old": "x", "new": "y"}
    )
    result = compare(recorded, proposed)
    assert result.verdict is EquivalenceVerdict.DIVERGENT
    assert "target differs" in result.reason


def test_equivalence_diverges_when_both_actions_lack_the_mapped_target_key() -> None:
    """Two unrelated `Edit`s that both omit `path` must never compare as
    EQUIVALENT by virtue of both missing the same key -- a recognized
    tool's absent target is a real gap, not a match."""
    recorded = RecordedAction(
        tool_use_id="t1", tool_name="Edit", tool_input={"old": "x", "new": "y"}
    )
    proposed = RecordedAction(
        tool_use_id="t2", tool_name="Edit", tool_input={"old": "totally different", "new": "z"}
    )
    result = compare(recorded, proposed)
    assert result.verdict is EquivalenceVerdict.DIVERGENT
    assert "carries no" in result.reason


def test_equivalence_diverges_on_a_different_anchor() -> None:
    recorded = RecordedAction(
        tool_use_id="t1", tool_name="Edit", tool_input={"path": "a.py", "old": "x", "new": "y"}
    )
    proposed = RecordedAction(
        tool_use_id="t2", tool_name="Edit", tool_input={"path": "a.py", "old": "q", "new": "y"}
    )
    result = compare(recorded, proposed)
    assert result.verdict is EquivalenceVerdict.DIVERGENT
    assert "anchor differs" in result.reason


def test_equivalence_ignores_the_replacement_text() -> None:
    """Two edits touching the same target and anchor are equivalent even
    with a different `new` -- the model doing the same thing at the same
    place is what equivalence means, not restating the recorded action."""
    recorded = RecordedAction(
        tool_use_id="t1", tool_name="Edit", tool_input={"path": "a.py", "old": "x", "new": "y"}
    )
    proposed = RecordedAction(
        tool_use_id="t2",
        tool_name="Edit",
        tool_input={"path": "a.py", "old": "x", "new": "totally different"},
    )
    assert compare(recorded, proposed).is_equivalent


def test_equivalence_compares_the_whole_input_for_an_unmapped_tool() -> None:
    recorded = RecordedAction(tool_use_id="t1", tool_name="Task", tool_input={"prompt": "do x"})
    same = RecordedAction(tool_use_id="t2", tool_name="Task", tool_input={"prompt": "do x"})
    different = RecordedAction(tool_use_id="t3", tool_name="Task", tool_input={"prompt": "do y"})
    assert compare(recorded, same).is_equivalent
    assert not compare(recorded, different).is_equivalent


def test_compare_session_pairs_positionally() -> None:
    baseline = (
        RecordedAction(tool_use_id="b1", tool_name="Read", tool_input={"path": "a.py"}),
        RecordedAction(
            tool_use_id="b2", tool_name="Edit", tool_input={"path": "a.py", "old": "x", "new": "y"}
        ),
    )
    proposed = (
        RecordedAction(tool_use_id="p1", tool_name="Read", tool_input={"path": "a.py"}),
        RecordedAction(
            tool_use_id="p2", tool_name="Edit", tool_input={"path": "a.py", "old": "q", "new": "y"}
        ),
    )
    equivalence = compare_session(baseline, proposed)
    assert equivalence.rate == pytest.approx(0.5)
    assert len(equivalence.divergences) == 1


def test_compare_session_rate_is_one_for_an_empty_session() -> None:
    assert compare_session((), ()).rate == 1.0


def test_compare_session_rejects_a_length_mismatch() -> None:
    baseline = (RecordedAction(tool_use_id="b1", tool_name="Read", tool_input={"path": "a.py"}),)
    with pytest.raises(ValueError, match="pair only non-induced turns"):
        compare_session(baseline, ())


def test_session_equivalence_over_a_recorded_response_session(tmp_path: Path) -> None:
    """End to end: a baseline paired with a loaded recorded-response
    fixture, structural equivalence computed over their non-induced,
    baseline-aligned actions -- no model call anywhere in this path."""
    baseline = _write(
        tmp_path / "s.jsonl",
        [
            _assistant(
                tool_name="Edit",
                tool_input={"path": "a.py", "old": "x", "new": "y"},
                tool_use_id="b1",
            )
        ],
    )
    _write(
        recorded_response_path(baseline),
        [
            _assistant(
                tool_name="Read",
                tool_input={"path": "a.py"},
                tool_use_id="r1",
                induced=True,
                provenance=_provenance(),
            ),
            _assistant(
                tool_name="Edit",
                tool_input={"path": "a.py", "old": "x", "new": "y"},
                tool_use_id="r2",
                provenance=_provenance(),
            ),
        ],
    )
    session = load_recorded_response(baseline)
    baseline_actions = tuple(turn.actions[-1] for turn in iter_turns(baseline) if turn.actions)
    equivalence = compare_session(baseline_actions, session.non_induced_actions)
    assert equivalence.rate == 1.0


# --- opt-in semantic judge ---------------------------------------------


class _FakeJudgeClient:
    def __init__(self, verdicts: list[tuple[JudgeVerdict, str]]) -> None:
        self._verdicts = verdicts
        self.calls = 0

    def judge(
        self, *, recorded: RecordedAction, proposed: RecordedAction, model: str
    ) -> tuple[JudgeVerdict, str]:
        self.calls += 1
        return self._verdicts.pop(0)


def _actions() -> tuple[RecordedAction, RecordedAction]:
    recorded = RecordedAction(
        tool_use_id="b1", tool_name="Edit", tool_input={"path": "a.py", "old": "x", "new": "y"}
    )
    proposed = RecordedAction(
        tool_use_id="p1", tool_name="Edit", tool_input={"path": "a.py", "old": "q", "new": "y"}
    )
    return recorded, proposed


def test_judge_config_defaults_to_disabled() -> None:
    config = JudgeConfig()
    assert config.enabled is False


def test_judge_config_rejects_enabled_without_a_model() -> None:
    with pytest.raises(JudgeConfigError, match="model identifier"):
        JudgeConfig(enabled=True, sample_rate=0.5, budget=5, client=_FakeJudgeClient([]))


def test_judge_config_rejects_a_sample_rate_outside_zero_one() -> None:
    with pytest.raises(JudgeConfigError, match="sample_rate"):
        JudgeConfig(enabled=True, model="m", sample_rate=1.5, budget=5, client=_FakeJudgeClient([]))


def test_judge_config_rejects_a_non_positive_budget() -> None:
    with pytest.raises(JudgeConfigError, match="budget must be positive"):
        JudgeConfig(enabled=True, model="m", sample_rate=0.5, budget=0, client=_FakeJudgeClient([]))


def test_judge_config_rejects_enabled_without_a_client() -> None:
    with pytest.raises(JudgeConfigError, match="no client is configured"):
        JudgeConfig(enabled=True, model="m", sample_rate=0.5, budget=5)


def test_judge_never_reviews_an_already_equivalent_comparison() -> None:
    client = _FakeJudgeClient([(JudgeVerdict.DIVERGENT, "should never be called")])
    config = JudgeConfig(enabled=True, model="m", sample_rate=1.0, budget=5, client=client)
    judge = Judge(config=config, seed=0)
    equivalent = StructuralComparison(EquivalenceVerdict.EQUIVALENT, "already equivalent")
    recorded, proposed = _actions()
    result = judge.review(equivalent, recorded=recorded, proposed=proposed)
    assert result is equivalent
    assert client.calls == 0


def test_judge_leaves_a_divergence_unchanged_when_disabled() -> None:
    judge = Judge(config=JudgeConfig(), seed=0)
    divergent = StructuralComparison(EquivalenceVerdict.DIVERGENT, "anchor differs")
    recorded, proposed = _actions()
    result = judge.review(divergent, recorded=recorded, proposed=proposed)
    assert result is divergent
    assert judge.calls_spent == 0
    assert judge.audits == ()


def test_judge_overturns_a_divergence_the_client_calls_equivalent() -> None:
    client = _FakeJudgeClient([(JudgeVerdict.EQUIVALENT, "same intent, different phrasing")])
    config = JudgeConfig(enabled=True, model="m", sample_rate=1.0, budget=5, client=client)
    judge = Judge(config=config, seed=0)
    divergent = StructuralComparison(EquivalenceVerdict.DIVERGENT, "anchor differs")
    recorded, proposed = _actions()
    result = judge.review(divergent, recorded=recorded, proposed=proposed)
    assert result.is_equivalent
    assert "overturned by judge" in result.reason
    assert "same intent, different phrasing" in result.reason
    assert judge.calls_spent == 1
    assert judge.audits[0].verdict is JudgeVerdict.EQUIVALENT


def test_judge_keeps_a_divergence_the_client_confirms() -> None:
    client = _FakeJudgeClient([(JudgeVerdict.DIVERGENT, "genuinely different edit")])
    config = JudgeConfig(enabled=True, model="m", sample_rate=1.0, budget=5, client=client)
    judge = Judge(config=config, seed=0)
    divergent = StructuralComparison(EquivalenceVerdict.DIVERGENT, "anchor differs")
    recorded, proposed = _actions()
    result = judge.review(divergent, recorded=recorded, proposed=proposed)
    assert not result.is_equivalent
    assert result is divergent
    assert judge.audits[0].verdict is JudgeVerdict.DIVERGENT


def test_judge_never_exceeds_its_budget() -> None:
    client = _FakeJudgeClient([(JudgeVerdict.DIVERGENT, "r") for _ in range(5)])
    config = JudgeConfig(enabled=True, model="m", sample_rate=1.0, budget=2, client=client)
    judge = Judge(config=config, seed=0)
    divergent = StructuralComparison(EquivalenceVerdict.DIVERGENT, "anchor differs")
    recorded, proposed = _actions()
    for _ in range(5):
        judge.review(divergent, recorded=recorded, proposed=proposed)
    assert judge.calls_spent == 2
    assert client.calls == 2


def test_judge_skips_a_sample_miss() -> None:
    """A seed that never clears `sample_rate` must never call the client."""
    client = _FakeJudgeClient([])
    config = JudgeConfig(enabled=True, model="m", sample_rate=1e-9, budget=100, client=client)
    judge = Judge(config=config, seed=1)
    divergent = StructuralComparison(EquivalenceVerdict.DIVERGENT, "anchor differs")
    recorded, proposed = _actions()
    for _ in range(20):
        judge.review(divergent, recorded=recorded, proposed=proposed)
    assert client.calls == 0
    assert judge.calls_spent == 0
