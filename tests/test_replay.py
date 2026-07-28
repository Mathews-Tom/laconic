"""Replay engine: turn iteration, baseline reproduction, and recorded and
live replay of the codec's counterfactual behaviour."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from laconic.replay.corpus import JsonValue, MalformedRecordError, Record
from laconic.replay.engine import (
    BASELINE_TOLERANCE_USD,
    BaselineMismatchError,
    BaselineSession,
    CostCapExceededError,
    LiveModeConfigError,
    LiveReplayConfig,
    MissingRecordedResponseError,
    Provenance,
    RecordedAction,
    ReplayTurnCapture,
    SessionCost,
    TurnUsage,
    UnknownTurnIndexError,
    assert_baseline,
    iter_turns,
    load_recorded_response,
    recorded_response_path,
    replay_live,
    replay_off,
    session_cost_of,
)


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
