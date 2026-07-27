"""Transcript ingest and channel attribution behaviour."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from laconic.replay.corpus import (
    EmptyCorpusError,
    JsonValue,
    MalformedRecordError,
    Record,
    find_transcripts,
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
