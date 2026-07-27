"""CLI surface: the measure command, its exit codes, and the script shim."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from laconic.cli import (
    EXIT_MALFORMED_RECORD,
    EXIT_MISMATCH,
    EXIT_NO_CORPUS,
    EXIT_NO_EXPECTATION,
    EXIT_OK,
    EXIT_REPORT_REQUIRES_CODEC,
    main,
)

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
