"""CLI surface: the measure command, its exit codes, and the script shim."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from laconic.cli import (
    EXIT_ASSERT_BASELINE_REQUIRES_CODEC_OFF,
    EXIT_CLIENT_IMPORT_ERROR,
    EXIT_LIVE_CONFIG_ERROR,
    EXIT_MALFORMED_RECORD,
    EXIT_MISMATCH,
    EXIT_MISSING_RECORDED_RESPONSE,
    EXIT_NO_CORPUS,
    EXIT_NO_EXPECTATION,
    EXIT_OK,
    EXIT_REPORT_REQUIRES_CODEC,
    main,
)
from laconic.replay.engine import recorded_response_path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = REPO_ROOT / "tests" / "corpus"
EXPECTED_FILE = CORPUS_DIR / "expected.json"
SHIM = REPO_ROOT / "scripts" / "measure_session_composition.py"
CONSOLE_SCRIPT = shutil.which("laconic")


def test_version_is_the_packaged_version(capsys: pytest.CaptureFixture[str]) -> None:
    from laconic import __version__

    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == EXIT_OK
    assert capsys.readouterr().out.strip() == f"laconic {__version__}"


def test_bare_invocation_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_OK
    assert "measure" in capsys.readouterr().out


def test_measure_reports_a_cost_split_that_sums_to_one_hundred(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["measure", str(CORPUS_DIR)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Cost decomposition (share of modelled spend):" in out
    total_line = next(line for line in out.splitlines() if line.strip().startswith("total"))
    assert total_line.strip().endswith("100.00%")


def test_measure_reports_every_channel(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["measure", str(CORPUS_DIR)]) == EXIT_OK
    out = capsys.readouterr().out
    for label in (
        "tool results (observations)",
        "tool_use args (actions)",
        "assistant prose (human-facing)",
        "human prompts",
    ):
        assert label in out


def test_measure_against_matching_expectation_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["measure", str(CORPUS_DIR), "--expect", str(EXPECTED_FILE)]) == EXIT_OK
    assert "Measurement matches" in capsys.readouterr().out


def test_measure_against_drifted_expectation_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = json.loads(EXPECTED_FILE.read_text())
    expected["channels"]["prose"] += 1
    drifted = tmp_path / "expected.json"
    drifted.write_text(json.dumps(expected))

    assert main(["measure", str(CORPUS_DIR), "--expect", str(drifted)]) == 1
    assert "channels.prose" in capsys.readouterr().err


def test_measure_on_an_empty_corpus_exits_non_zero_with_a_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["measure", str(tmp_path)]) == 3
    error = capsys.readouterr().err
    assert "no *.jsonl transcripts found" in error
    assert str(tmp_path) in error


def test_measure_on_a_corpus_without_usage_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "s.jsonl").write_text(
        json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n"
    )
    assert main(["measure", str(tmp_path)]) == 3
    assert "no assistant usage records" in capsys.readouterr().err


def test_measure_does_not_double_count_a_committed_recorded_response_fixture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `laconic replay` fixture committed beside its baseline must not
    be counted as an extra measured session."""
    baseline = _write_records(
        tmp_path / "s.jsonl", [_assistant_record(tool_name="Edit", tool_input={"path": "a.py"})]
    )
    _write_records(
        recorded_response_path(baseline),
        [
            _assistant_record(
                tool_name="Edit", tool_input={"path": "a.py"}, provenance=_provenance_record()
            )
        ],
    )
    assert main(["measure", str(tmp_path)]) == EXIT_OK
    assert "Scanning 1 session transcripts" in capsys.readouterr().out


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, check=False)


def test_shim_output_is_identical_to_the_measure_command() -> None:
    """`docs/overview.md` §2 cites the script; both paths must agree."""
    via_script = _run([sys.executable, str(SHIM), str(CORPUS_DIR)])
    assert CONSOLE_SCRIPT is not None, "the laconic console script must be installed"
    via_cli = _run([CONSOLE_SCRIPT, "measure", str(CORPUS_DIR)])
    assert via_script.returncode == EXIT_OK
    assert via_cli.returncode == EXIT_OK
    assert via_script.stdout == via_cli.stdout


def test_shim_propagates_a_failing_exit_code(tmp_path: Path) -> None:
    result = _run([sys.executable, str(SHIM), str(tmp_path)])
    assert result.returncode == EXIT_NO_CORPUS
    assert "no *.jsonl transcripts found" in result.stderr


def test_measure_reports_the_headline_prose_figures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The project's headline number, pinned to tests/corpus/README.md."""
    assert main(["measure", str(CORPUS_DIR)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Output share of spend                        11.25%" in out
    assert "Prose share of emitted output                19.41%" in out
    assert "HUMAN-FACING PROSE SHARE OF SPEND             2.18%" in out


def test_measure_on_a_corpus_without_billable_tokens_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "s.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-5",
                    "content": [{"type": "text", "text": "hello"}],
                    "usage": {"input_tokens": 0},
                },
            }
        )
        + "\n"
    )
    assert main(["measure", str(tmp_path)]) == 3
    assert "no billable tokens" in capsys.readouterr().err


def test_measure_on_a_corpus_without_channel_content_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "s.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-5",
                    "content": [],
                    "usage": {"output_tokens": 500},
                },
            }
        )
        + "\n"
    )
    assert main(["measure", str(tmp_path)]) == 3
    assert "no channel content" in capsys.readouterr().err


def test_measure_on_a_mistyped_path_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["measure", str(tmp_path / "typo")]) == 3
    assert "does not exist" in capsys.readouterr().err


def test_missing_expectation_file_is_distinct_from_a_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.json"
    assert main(["measure", str(CORPUS_DIR), "--expect", str(missing)]) == 4
    assert "cannot read" in capsys.readouterr().err


def test_malformed_expectation_file_is_distinct_from_a_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "expected.json"
    broken.write_text("{not json")
    assert main(["measure", str(CORPUS_DIR), "--expect", str(broken)]) == 4
    assert "not valid JSON" in capsys.readouterr().err


def test_an_unpriced_model_is_reported_as_a_guess(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "s.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-someday-9",
                    "content": [{"type": "text", "text": "hello"}],
                    "usage": {"output_tokens": 500},
                },
            }
        )
        + "\n"
    )
    assert main(["measure", str(tmp_path)]) == EXIT_OK
    assert "no published price for claude-someday-9" in capsys.readouterr().err


def test_exit_codes_are_distinct_and_avoid_the_argparse_usage_code() -> None:
    """A caller must be able to tell a drift from an unusable corpus or file."""
    codes = [
        EXIT_OK,
        EXIT_MISMATCH,
        EXIT_NO_CORPUS,
        EXIT_NO_EXPECTATION,
        EXIT_MALFORMED_RECORD,
    ]
    assert codes == [0, 1, 3, 4, 5]
    assert len(set(codes)) == len(codes)
    assert 2 not in codes


def test_an_undecodable_expectation_file_is_not_reported_as_a_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binary = tmp_path / "expected.json"
    binary.write_bytes(b"\xff\xfe\x00\x01\x80\x81")
    assert main(["measure", str(CORPUS_DIR), "--expect", str(binary)]) == 4
    assert "not UTF-8 text" in capsys.readouterr().err


def test_a_mistyped_token_counter_exits_with_its_own_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "s.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-5",
                    "content": [{"type": "text", "text": "hello"}],
                    "usage": {"output_tokens": 12.5},
                },
            }
        )
        + "\n"
    )
    assert main(["measure", str(tmp_path)]) == 5
    assert "output_tokens is not an integer" in capsys.readouterr().err


def test_an_unreadable_transcript_is_not_reported_as_a_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An I/O failure must not look like a measurement drift."""
    transcript = tmp_path / "s.jsonl"
    transcript.write_text('{"type": "user", "message": {"content": "hi"}}\n')
    transcript.chmod(0o000)
    try:
        assert main(["measure", str(tmp_path)]) == 3
    finally:
        transcript.chmod(0o600)
    assert "cannot read the corpus" in capsys.readouterr().err


def test_a_tool_with_no_recorded_calls_reports_no_mean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An orphaned tool result must not be given a fabricated mean over one call."""
    records = [
        {
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "checking the widget registry"}],
                "usage": {"cache_read_input_tokens": 5_000, "output_tokens": 40},
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "gone", "content": "x" * 90}]
            },
        },
    ]
    (tmp_path / "s.jsonl").write_text("\n".join(json.dumps(record) for record in records) + "\n")
    assert main(["measure", str(tmp_path)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "calls=0" in out
    assert "mean=n/a" in out


def test_report_reduction_requires_codec_on(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["measure", str(CORPUS_DIR), "--report", "reduction"]) == EXIT_REPORT_REQUIRES_CODEC
    assert "requires --codec on" in capsys.readouterr().err


def test_codec_on_without_report_prints_no_reduction_section(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["measure", str(CORPUS_DIR), "--codec", "on"]) == EXIT_OK
    assert "File observation encoder" not in capsys.readouterr().out


def test_codec_on_report_reduction_prints_a_gross_reduction_below_one_hundred_percent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["measure", str(CORPUS_DIR), "--codec", "on", "--report", "reduction"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "File observation encoder" in out
    assert "gross" in out
    reduction_line = next(line for line in out.splitlines() if "reduction:" in line)
    reduction = float(reduction_line.split(":")[1].strip().rstrip("%"))
    assert 0.0 < reduction < 100.0


def test_codec_on_report_reduction_never_claims_net_savings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`DEVELOPMENT_PLAN.md` §6 M4 constraint: this milestone must not claim
    net savings — induced-read accounting belongs to M8."""
    assert main(["measure", str(CORPUS_DIR), "--codec", "on", "--report", "reduction"]) == EXIT_OK
    out = capsys.readouterr().out
    reduction_section = out.rsplit("File observation encoder", 1)[1].lower()
    assert "net " not in reduction_section
    assert "savings" not in reduction_section


def test_codec_on_report_reduction_composes_with_expect(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "measure",
            str(CORPUS_DIR),
            "--codec",
            "on",
            "--report",
            "reduction",
            "--expect",
            str(EXPECTED_FILE),
        ]
    )
    assert exit_code == EXIT_OK
    out = capsys.readouterr().out
    assert "Measurement matches" in out
    assert "File observation encoder" in out


def test_report_reduction_on_a_corpus_with_no_reads_reports_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    records = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "hello"}],
                "usage": {
                    "input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 10,
                },
            },
        },
    ]
    (tmp_path / "s.jsonl").write_text("\n".join(json.dumps(record) for record in records) + "\n")
    exit_code = main(["measure", str(tmp_path), "--codec", "on", "--report", "reduction"])
    assert exit_code == EXIT_OK
    assert "no Read observations found" in capsys.readouterr().out


# --- laconic replay ------------------------------------------------------


def _assistant_record(
    *,
    tool_name: str | None = None,
    tool_input: dict[str, object] | None = None,
    tool_use_id: str = "toolu_1",
    provenance: dict[str, object] | None = None,
    induced: bool = False,
) -> dict[str, object]:
    content: list[object] = []
    if tool_name is not None:
        content.append(
            {"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": tool_input or {}}
        )
    record: dict[str, object] = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": content,
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 1_000,
                "cache_creation_input_tokens": 200,
                "output_tokens": 100,
            },
        },
    }
    if induced:
        record["induced"] = True
    if provenance is not None:
        record["provenance"] = provenance
    return record


def _tool_result_record(tool_use_id: str, content: str) -> dict[str, object]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}],
        },
    }


def _write_records(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


def _provenance_record() -> dict[str, object]:
    return {"source": "recorded", "model": "claude-sonnet-5", "captured_at": "2026-07-28T00:00:00Z"}


def test_replay_codec_off_text_reports_every_session_and_a_total(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["replay", str(CORPUS_DIR)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "session-a-refactor.jsonl" in out
    assert "total cost" in out


def test_replay_codec_off_json_reports_a_parseable_total(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["replay", str(CORPUS_DIR), "--format", "json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["codec"] == "off"
    assert payload["total_turns"] == 125
    assert len(payload["sessions"]) == 3


def test_replay_assert_baseline_passes_on_the_fixture_corpus() -> None:
    assert main(["replay", str(CORPUS_DIR), "--assert-baseline"]) == EXIT_OK


def test_replay_assert_baseline_requires_codec_off(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["replay", str(CORPUS_DIR), "--codec", "on", "--assert-baseline"])
    assert exit_code == EXIT_ASSERT_BASELINE_REQUIRES_CODEC_OFF
    assert "--assert-baseline requires --codec off" in capsys.readouterr().err


def test_replay_codec_on_without_a_fixture_reports_it_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_records(
        tmp_path / "s.jsonl", [_assistant_record(tool_name="Edit", tool_input={"path": "a.py"})]
    )
    exit_code = main(["replay", str(tmp_path), "--codec", "on"])
    assert exit_code == EXIT_MISSING_RECORDED_RESPONSE
    assert "no committed recorded-response fixture" in capsys.readouterr().err


def test_replay_codec_on_recorded_reports_net_cost_and_equivalence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = _write_records(
        tmp_path / "s.jsonl",
        [_assistant_record(tool_name="Edit", tool_input={"path": "a.py", "old": "x", "new": "y"})],
    )
    _write_records(
        recorded_response_path(baseline),
        [
            _assistant_record(
                tool_name="Edit",
                tool_input={"path": "a.py", "old": "x", "new": "y"},
                provenance=_provenance_record(),
            )
        ],
    )
    exit_code = main(["replay", str(tmp_path), "--codec", "on"])
    assert exit_code == EXIT_OK
    out = capsys.readouterr().out
    assert "net savings" in out
    assert "equivalence" in out


def test_replay_codec_on_json_reports_a_parseable_net_cost_payload(tmp_path: Path) -> None:
    baseline = _write_records(
        tmp_path / "s.jsonl",
        [_assistant_record(tool_name="Edit", tool_input={"path": "a.py", "old": "x", "new": "y"})],
    )
    _write_records(
        recorded_response_path(baseline),
        [
            _assistant_record(
                tool_name="Edit",
                tool_input={"path": "a.py", "old": "x", "new": "y"},
                provenance=_provenance_record(),
            )
        ],
    )

    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        exit_code = main(["replay", str(tmp_path), "--codec", "on", "--format", "json"])
    assert exit_code == EXIT_OK
    payload = json.loads(buffer.getvalue())
    assert payload["codec"] == "on"
    assert payload["sessions"][0]["equivalence_rate"] == 1.0


def test_replay_live_requires_all_four_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_records(
        tmp_path / "s.jsonl", [_assistant_record(tool_name="Edit", tool_input={"path": "a.py"})]
    )
    exit_code = main(["replay", str(tmp_path), "--codec", "on", "--mode", "live"])
    assert exit_code == EXIT_LIVE_CONFIG_ERROR
    assert "requires --model, --cost-cap, --artifact-dir, and --client" in capsys.readouterr().err


def test_replay_live_reports_an_unresolvable_client(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_records(
        tmp_path / "s.jsonl", [_assistant_record(tool_name="Edit", tool_input={"path": "a.py"})]
    )
    exit_code = main(
        [
            "replay",
            str(tmp_path),
            "--codec",
            "on",
            "--mode",
            "live",
            "--model",
            "claude-sonnet-5",
            "--cost-cap",
            "1.0",
            "--artifact-dir",
            str(tmp_path / "artifacts"),
            "--client",
            "laconic.nonexistent_module:factory",
        ]
    )
    assert exit_code == EXIT_CLIENT_IMPORT_ERROR
    assert "cannot load --client" in capsys.readouterr().err


def test_replay_live_runs_end_to_end_with_an_injected_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    client_module = tmp_path / "fake_replay_client.py"
    client_module.write_text(
        "from laconic.replay.engine import RecordedAction, ReplayTurnCapture, TurnUsage\n\n"
        "class _Client:\n"
        "    def respond(self, *, prefix, observation, model):\n"
        "        return [ReplayTurnCapture(\n"
        "            action=RecordedAction(tool_use_id='live1', tool_name='Edit', "
        "tool_input={'path': 'a.py', 'old': 'x', 'new': 'y'}),\n"
        "            usage=TurnUsage(model=model, input_tokens=1, cache_read=1, "
        "cache_write=1, output_tokens=1),\n"
        "        )]\n\n"
        "def factory():\n"
        "    return _Client()\n"
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_records(
        corpus / "s.jsonl",
        [
            _assistant_record(tool_name="Read", tool_input={"path": "a.py"}, tool_use_id="t1"),
            _tool_result_record("t1", "def f(): pass"),
            _assistant_record(
                tool_name="Edit",
                tool_input={"path": "a.py", "old": "x", "new": "y"},
                tool_use_id="t2",
            ),
        ],
    )
    exit_code = main(
        [
            "replay",
            str(corpus),
            "--codec",
            "on",
            "--mode",
            "live",
            "--model",
            "claude-sonnet-5",
            "--cost-cap",
            "10.0",
            "--artifact-dir",
            str(tmp_path / "artifacts"),
            "--client",
            "fake_replay_client:factory",
        ]
    )
    assert exit_code == EXIT_OK
    artifact = tmp_path / "artifacts" / "s.codec-on.jsonl"
    assert artifact.is_file()
    written = json.loads(artifact.read_text().splitlines()[0])
    assert written["provenance"]["source"] == "live"
