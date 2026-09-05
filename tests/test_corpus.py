"""Transcript ingest, channel attribution, and redaction behaviour."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from laconic.costs import session_cost
from laconic.replay.corpus import (
    COST_TOLERANCE_USD,
    EXPECTATION_SCHEMA_VERSION,
    REPLAY_ARTIFACT_SUFFIX,
    EmptyCorpusError,
    Expectation,
    JsonValue,
    MalformedRecordError,
    Record,
    compare_expectation,
    expectation,
    find_transcripts,
    redact_record,
    redact_text,
    redact_transcript,
    scan,
    scan_corpus,
)


def _assistant(
    *,
    model: str = "claude-sonnet-5",
    content: list[JsonValue],
    output_tokens: int = 100,
) -> Record:
    return {
        "type": "assistant",
        "message": {
            "model": model,
            "content": content,
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 1_000,
                "cache_creation_input_tokens": 200,
                "output_tokens": output_tokens,
            },
        },
    }


def _write_transcript(path: Path, records: list[Record]) -> Path:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


def test_prose_excludes_fenced_code(tmp_path: Path) -> None:
    text = "before\n```\nsecret_code()\n```\nafter"
    fence = "```\nsecret_code()\n```"
    path = _write_transcript(
        tmp_path / "s.jsonl",
        [_assistant(content=[{"type": "text", "text": text}])],
    )
    channels = scan([path]).channels
    assert channels.fenced_code_in_prose == len(fence)
    assert channels.prose == len(text) - len(fence)


def test_tool_results_are_attributed_to_the_calling_tool(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "s.jsonl",
        [
            _assistant(
                content=[{"type": "tool_use", "id": "t1", "name": "Read", "input": {"p": "a"}}]
            ),
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "12345"}]
                },
            },
        ],
    )
    channels = scan([path]).channels
    assert channels.result_chars_by_tool["Read"] == 5
    assert channels.calls_by_tool["Read"] == 1
    assert channels.tool_args == len(json.dumps({"p": "a"}))


def test_unmatched_tool_result_is_attributed_to_the_unknown_bucket(
    tmp_path: Path,
) -> None:
    path = _write_transcript(
        tmp_path / "s.jsonl",
        [
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "gone", "content": "xy"}]
                },
            }
        ],
    )
    channels = scan([path]).channels
    assert channels.tool_results == 2
    assert channels.result_chars_by_tool["?"] == 2


def test_user_prompt_text_lands_in_its_own_channel(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "s.jsonl",
        [{"type": "user", "message": {"content": "hello there"}}],
    )
    channels = scan([path]).channels
    assert channels.user_prompts == len("hello there")
    assert channels.prose == 0


def test_usage_accumulates_per_model(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "s.jsonl",
        [
            _assistant(content=[], output_tokens=100),
            _assistant(content=[], output_tokens=50),
            _assistant(model="claude-opus-4-8", content=[], output_tokens=7),
        ],
    )
    usage = scan([path]).usage
    assert usage["claude-sonnet-5"].turns == 2
    assert usage["claude-sonnet-5"].output_tokens == 150
    assert usage["claude-opus-4-8"].output_tokens == 7


def test_assistant_turn_without_usage_is_not_counted(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "s.jsonl",
        [{"type": "assistant", "message": {"model": "claude-sonnet-5", "content": []}}],
    )
    result = scan([path])
    assert result.usage == {}
    assert result.channels.total == 0


def test_record_line_numbers_are_real_file_lines(tmp_path: Path) -> None:
    """A diagnostic must point at the line a reader can open, blank lines and all."""
    path = tmp_path / "s.jsonl"
    path.write_text(
        '{"type": "user", "message": {"content": "hi"}}\n'
        "\n"
        "   \n"
        '{"type": "assistant", "message": {"model": "m", "content": [],'
        ' "usage": {"output_tokens": 1.5}}}\n'
    )
    with pytest.raises(MalformedRecordError, match=r"s\.jsonl:4: output_tokens"):
        scan([path])


def test_a_mistyped_usage_block_stops_the_scan(tmp_path: Path) -> None:
    """`usage` present but not an object is a defect, not an absent usage block."""
    path = _write_transcript(
        tmp_path / "s.jsonl",
        [
            {
                "type": "assistant",
                "message": {"model": "claude-sonnet-5", "content": [], "usage": "500"},
            }
        ],
    )
    with pytest.raises(MalformedRecordError, match="usage is not an object"):
        scan([path])


def test_malformed_lines_are_counted_not_hidden(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text('{"type": "user", "message": {"content": "hi"}}\nnot json\n[1,2]\n')
    result = scan([path])
    assert result.malformed_lines == 2
    assert result.records == 1


def test_transcripts_are_discovered_in_a_stable_order(tmp_path: Path) -> None:
    (tmp_path / "b").mkdir()
    for name in ("z.jsonl", "a.jsonl", "b/m.jsonl"):
        (tmp_path / name).write_text("")
    (tmp_path / "ignored.txt").write_text("")
    found = find_transcripts([tmp_path])
    assert found == sorted(found)
    assert [p.name for p in found] == ["a.jsonl", "m.jsonl", "z.jsonl"]


def test_a_recorded_response_replay_artifact_is_excluded_from_a_directory_scan(
    tmp_path: Path,
) -> None:
    """A committed `laconic.replay.engine` fixture beside its baseline
    (`<stem>.codec-on.jsonl`) is synthetic material about a session, never
    measurable session of its own -- `laconic research measure` must not
    double-count it."""
    (tmp_path / "session-a.jsonl").write_text("")
    (tmp_path / f"session-a{REPLAY_ARTIFACT_SUFFIX}").write_text("")
    found = find_transcripts([tmp_path])
    assert [p.name for p in found] == ["session-a.jsonl"]


def test_a_replay_artifact_named_explicitly_is_still_readable(tmp_path: Path) -> None:
    """The directory-scan exclusion is not a blanket ban: a caller naming
    a replay artifact file directly is not scanning a corpus by accident."""
    artifact = tmp_path / f"session-a{REPLAY_ARTIFACT_SUFFIX}"
    artifact.write_text("")
    assert find_transcripts([artifact]) == [artifact]


def test_empty_corpus_raises_with_the_searched_path(tmp_path: Path) -> None:
    with pytest.raises(EmptyCorpusError, match=str(tmp_path)):
        scan_corpus([tmp_path])


def test_corpus_without_usage_records_raises(tmp_path: Path) -> None:
    _write_transcript(tmp_path / "s.jsonl", [{"type": "user", "message": {"content": "hi"}}])
    with pytest.raises(EmptyCorpusError, match="no assistant usage records"):
        scan_corpus([tmp_path])


def test_list_form_tool_results_are_measured_as_serialised_json(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "s.jsonl",
        [
            _assistant(content=[{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]),
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [{"type": "text", "text": "secret body"}],
                        }
                    ]
                },
            },
        ],
    )
    channels = scan([path]).channels
    assert channels.tool_results == len(
        json.dumps([{"type": "text", "text": "secret body"}], ensure_ascii=False)
    )
    assert channels.result_chars_by_tool["Read"] == channels.tool_results


def test_a_mistyped_token_counter_stops_the_scan(tmp_path: Path) -> None:
    """A float token count must not silently bill as zero, nor drop the record."""
    path = _write_transcript(
        tmp_path / "s.jsonl",
        [
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-5",
                    "content": [],
                    "usage": {"input_tokens": 1234.0, "output_tokens": 10},
                },
            }
        ],
    )
    with pytest.raises(MalformedRecordError, match="input_tokens is not an integer: 1234.0"):
        scan([path])


def test_a_missing_corpus_path_is_reported(tmp_path: Path) -> None:
    with pytest.raises(EmptyCorpusError, match="does not exist"):
        find_transcripts([tmp_path / "nope"])


def test_a_corpus_with_no_billable_tokens_raises(tmp_path: Path) -> None:
    _write_transcript(
        tmp_path / "s.jsonl",
        [
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-5",
                    "content": [{"type": "text", "text": "hello"}],
                    "usage": {"input_tokens": 0},
                },
            }
        ],
    )
    with pytest.raises(EmptyCorpusError, match="no billable tokens"):
        scan_corpus([tmp_path])


def test_a_corpus_with_no_channel_content_raises(tmp_path: Path) -> None:
    _write_transcript(
        tmp_path / "s.jsonl",
        [
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-5",
                    "content": [],
                    "usage": {"output_tokens": 500},
                },
            }
        ],
    )
    with pytest.raises(EmptyCorpusError, match="no channel content"):
        scan_corpus([tmp_path])


def test_redaction_removes_words_but_keeps_length() -> None:
    original = "def load_customer(id: str) -> None:  # ACME internal"
    redacted = redact_text(original)
    assert len(redacted) == len(original)
    assert "load_customer" not in redacted
    assert "ACME" not in redacted
    assert redacted.count("(") == original.count("(")


def test_redaction_preserves_structural_fields() -> None:
    """Block structure survives; tool arguments never do, whatever they are named."""
    record: Record = {
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-5",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Grep",
                    "input": {
                        "pattern": "AcmeCorp",
                        "type": "py",
                        "name": "customer_ledger",
                    },
                }
            ],
            "usage": {"output_tokens": 12},
        },
    }
    redacted = redact_record(record)
    message = redacted["message"]
    assert isinstance(message, dict)
    assert message["model"] == "claude-sonnet-5"
    assert message["usage"] == {"output_tokens": 12}
    blocks = message["content"]
    assert isinstance(blocks, list)
    block = blocks[0]
    assert isinstance(block, dict)
    assert block["type"] == "tool_use"
    assert block["name"] == "Grep"
    assert block["id"] == "t1"
    assert block["input"] == {"pattern": "xxxxxxxx", "type": "xx", "name": "xxxxxxxx_xxxxxx"}


def test_redacted_corpus_reproduces_original_channel_sizes(tmp_path: Path) -> None:
    records: list[Record] = [
        _assistant(
            content=[
                {"type": "text", "text": "Renaming Widget.\n```py\nx = 1\n```\ndone."},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Edit",
                    "input": {"path": "src/widget.py", "body": 'a "quoted" line\n\ttab'},
                },
            ]
        ),
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "applied to src/widget.py\n",
                    }
                ]
            },
        },
        {"type": "user", "message": {"content": "now update the caller"}},
    ]
    source = _write_transcript(tmp_path / "raw.jsonl", records)
    target = tmp_path / "redacted.jsonl"

    assert redact_transcript(source, target) == len(records)

    before = scan([source]).channels
    after = scan([target]).channels
    assert (after.prose, after.fenced_code_in_prose) == (
        before.prose,
        before.fenced_code_in_prose,
    )
    assert (after.tool_args, after.tool_results, after.user_prompts) == (
        before.tool_args,
        before.tool_results,
        before.user_prompts,
    )
    assert after.result_chars_by_tool == before.result_chars_by_tool
    assert "widget" not in target.read_text()


def test_redacting_a_malformed_transcript_fails_loudly(tmp_path: Path) -> None:
    source = tmp_path / "raw.jsonl"
    source.write_text('{"type": "user"}\nnot json\n')
    with pytest.raises(ValueError, match="refusing to redact"):
        redact_transcript(source, tmp_path / "out.jsonl")


def test_redaction_preserves_channel_sizes_for_non_ascii_content(tmp_path: Path) -> None:
    """Redaction exists for real transcripts, which are not ASCII."""
    records: list[Record] = [
        _assistant(
            content=[
                {"type": "text", "text": "Le café résiste. 漢字も。"},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Edit",
                    "input": {"path": "src/wîdget.py", "body": "漢字 identifier"},
                },
            ]
        ),
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "café 漢"}],
                    }
                ]
            },
        },
    ]
    source = _write_transcript(tmp_path / "raw.jsonl", records)
    target = tmp_path / "redacted.jsonl"
    redact_transcript(source, target)

    before = scan([source]).channels
    after = scan([target]).channels
    assert (after.tool_args, after.tool_results) == (before.tool_args, before.tool_results)
    assert after.prose == before.prose
    assert "café" not in target.read_text()
    assert "漢字" not in target.read_text()


def test_redacting_a_transcript_onto_itself_is_refused(tmp_path: Path) -> None:
    source = _write_transcript(
        tmp_path / "raw.jsonl", [{"type": "user", "message": {"content": "private"}}]
    )
    with pytest.raises(ValueError, match="onto itself"):
        redact_transcript(source, source)
    assert "private" in source.read_text()


def test_a_refused_redaction_leaves_no_output_file(tmp_path: Path) -> None:
    source = tmp_path / "raw.jsonl"
    source.write_text('{"type": "user", "message": {"content": "first"}}\nnot json\n')
    target = tmp_path / "out.jsonl"
    with pytest.raises(ValueError, match="refusing to redact"):
        redact_transcript(source, target)
    assert not target.exists()
    assert list(tmp_path.glob("*.redacting")) == []


def test_redaction_does_not_trust_key_names_inside_a_tool_result(tmp_path: Path) -> None:
    """A tool result is opaque payload: no key name inside it earns a pass."""
    record: Record = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": [
                        {
                            "type": "json",
                            "name": "AcmeCorp Holdings",
                            "id": "cus_9Hx2VaultKey",
                            "model": "internal-pricing-v3",
                            "role": "chief-revenue-officer",
                        }
                    ],
                }
            ],
        },
    }
    serialised = json.dumps(redact_record(record))
    for secret in ("AcmeCorp", "VaultKey", "internal-pricing", "revenue"):
        assert secret not in serialised
    assert '"type": "tool_result"' in serialised
    assert '"tool_use_id": "t1"' in serialised


def test_redaction_covers_the_usage_block(tmp_path: Path) -> None:
    """Only the counters are structural; a string in `usage` is still content."""
    record: Record = {
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-5",
            "content": [],
            "usage": {"output_tokens": 12, "service_tier": "AcmeCorp-dedicated"},
        },
    }
    message = redact_record(record)["message"]
    assert isinstance(message, dict)
    usage = message["usage"]
    assert isinstance(usage, dict)
    assert usage["output_tokens"] == 12
    assert usage["service_tier"] == "xxxxxxxx-xxxxxxxxx"


def test_a_failing_move_leaves_no_staged_file(tmp_path: Path) -> None:
    """The staging contract holds even when the move itself fails."""
    source = _write_transcript(
        tmp_path / "raw.jsonl", [{"type": "user", "message": {"content": "private"}}]
    )
    destination = tmp_path / "out.jsonl"
    destination.mkdir()
    with pytest.raises(OSError):
        redact_transcript(source, destination)
    assert list(tmp_path.glob("*.redacting")) == []


CORPUS_DIR = Path(__file__).parent / "corpus"
EXPECTED_FILE = CORPUS_DIR / "expected.json"


def _load_expected() -> Expectation:
    loaded = json.loads(EXPECTED_FILE.read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_fixture_corpus_matches_committed_expected_values() -> None:
    measured = expectation(scan_corpus([CORPUS_DIR]))
    assert compare_expectation(_load_expected(), measured) == []


def test_fixture_expected_values_are_the_current_schema() -> None:
    assert _load_expected()["schema_version"] == EXPECTATION_SCHEMA_VERSION


def test_fixture_cost_split_sums_to_one_hundred_percent() -> None:
    shares = session_cost(scan_corpus([CORPUS_DIR]).usage).shares()
    assert round(shares.total, 2) == 100.00


def test_fixture_channel_total_matches_the_committed_channel_counters() -> None:
    """Total is pinned to the committed counters, so a shift between channels fails."""
    channels = scan_corpus([CORPUS_DIR]).channels
    committed = _load_expected()["channels"]
    assert isinstance(committed, dict)
    expected_total = sum(
        value
        for key, value in committed.items()
        if key != "fenced_code_in_prose" and isinstance(value, int)
    )
    assert channels.total == expected_total
    assert channels.fenced_code_in_prose > 0
    assert channels.total < expected_total + channels.fenced_code_in_prose


def test_fixture_corpus_parses_cleanly() -> None:
    result = scan_corpus([CORPUS_DIR])
    assert result.malformed_lines == 0
    assert result.transcripts == 3


def test_fixture_reproduces_the_documented_session_shape() -> None:
    """The fixture is only useful if it has the shape of a real session."""
    result = scan_corpus([CORPUS_DIR])
    channels = result.channels
    shares = session_cost(result.usage).shares()
    assert shares.cache_read > shares.cache_write > shares.output > shares.uncached_input
    assert channels.tool_results > 10 * channels.prose
    zero_prose = sum(1 for turn in channels.prose_per_turn if turn == 0)
    assert zero_prose / len(channels.prose_per_turn) > 0.75
    assert channels.result_chars_by_tool.most_common(1)[0][0] == "Read"


def test_fixture_comparison_reports_a_drift_rather_than_passing() -> None:
    expected = _load_expected()
    channels = expected["channels"]
    assert isinstance(channels, dict)
    prose = channels["prose"]
    assert isinstance(prose, int)
    channels["prose"] = prose + 1
    differences = compare_expectation(expected, expectation(scan_corpus([CORPUS_DIR])))
    assert differences == [f"channels.prose: expected {prose + 1}, measured {prose}"]


def test_fixture_corpus_holds_no_real_session_content() -> None:
    """Committed fixtures are synthetic; nothing may leak a real machine."""
    for transcript in sorted(CORPUS_DIR.glob("*.jsonl")):
        text = transcript.read_text()
        assert "/Users/" not in text
        assert "/home/" not in text
        assert str(Path.home()) not in text


def test_fixture_cost_drift_beyond_tolerance_is_reported() -> None:
    """COST_TOLERANCE_USD must be tight enough to catch a real accounting error."""
    measured = expectation(scan_corpus([CORPUS_DIR]))
    costs = measured["cost_usd"]
    assert isinstance(costs, dict)
    total = costs["total"]
    assert isinstance(total, float)

    within = json.loads(json.dumps(_load_expected()))
    within["cost_usd"]["total"] = total + 0.1 * COST_TOLERANCE_USD
    assert compare_expectation(within, measured) == []

    beyond = json.loads(json.dumps(_load_expected()))
    beyond["cost_usd"]["total"] = total + 10 * COST_TOLERANCE_USD
    assert len(compare_expectation(beyond, measured)) == 1

    # An absolute figure, so widening the constant cannot keep this passing.
    real_error = json.loads(json.dumps(_load_expected()))
    real_error["cost_usd"]["total"] = total + 0.01
    assert len(compare_expectation(real_error, measured)) == 1
