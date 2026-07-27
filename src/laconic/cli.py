"""Command-line entrypoint for Laconic."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from laconic import __version__
from laconic.costs import CostBreakdown, ModelUsage, session_cost, unpriced_models
from laconic.replay.corpus import (
    Channels,
    CorpusScan,
    EmptyCorpusError,
    Expectation,
    MalformedRecordError,
    compare_expectation,
    expectation,
    scan_corpus,
)

DEFAULT_CORPUS = Path.home() / ".claude" / "projects"

#: Compression ratios reported as hypothetical savings on the prose channel.
_COMPRESSION_RATIOS = (0.44, 0.65, 0.90, 1.00)

#: argparse owns 2 for usage errors, so domain failures start at 3.
EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_NO_CORPUS = 3
EXIT_NO_EXPECTATION = 4
EXIT_MALFORMED_RECORD = 5


def build_parser() -> argparse.ArgumentParser:
    """Build the currently available CLI surface."""
    parser = argparse.ArgumentParser(description="A context-loop codec for coding agents.")
    parser.add_argument("--version", action="version", version=f"laconic {__version__}")
    subcommands = parser.add_subparsers(dest="command")

    measure = subcommands.add_parser(
        "measure",
        help="channel decomposition and cost split of real sessions",
        description=(
            "Decompose session transcripts into tool results, tool_use arguments, "
            "assistant prose, and human prompts, weighted by real API pricing."
        ),
    )
    measure.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[DEFAULT_CORPUS],
        help=f"directories containing session transcripts (default: {DEFAULT_CORPUS})",
    )
    measure.add_argument(
        "--expect",
        type=Path,
        metavar="FILE",
        help="compare the measurement against a committed expected-values file",
    )
    measure.set_defaults(handler=_measure)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Laconic CLI and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_OK
    exit_code: int = handler(args)
    return exit_code


def _measure(args: argparse.Namespace) -> int:
    paths: list[Path] = list(args.paths)
    try:
        result = scan_corpus(paths)
    except EmptyCorpusError as error:
        print(f"laconic measure: {error}", file=sys.stderr)
        return EXIT_NO_CORPUS
    except MalformedRecordError as error:
        print(f"laconic measure: {error}", file=sys.stderr)
        return EXIT_MALFORMED_RECORD
    except OSError as error:
        print(f"laconic measure: cannot read the corpus: {error}", file=sys.stderr)
        return EXIT_NO_CORPUS

    print(f"Scanning {result.transcripts} session transcripts...\n")
    if result.malformed_lines:
        print(
            f"warning: skipped {result.malformed_lines} unparseable record(s)",
            file=sys.stderr,
        )
    guessed = unpriced_models(result.usage)
    if guessed:
        print(
            f"warning: no published price for {', '.join(guessed)}; billed at the fallback rate",
            file=sys.stderr,
        )
    _report(result)

    expected_file: Path | None = args.expect
    if expected_file is None:
        return EXIT_OK
    return _check_expectation(expected_file, expectation(result))


def _check_expectation(expected_file: Path, measured: Expectation) -> int:
    try:
        loaded = json.loads(expected_file.read_text(encoding="utf-8"))
    except OSError as error:
        print(f"laconic measure: cannot read {expected_file}: {error}", file=sys.stderr)
        return EXIT_NO_EXPECTATION
    except UnicodeDecodeError as error:
        print(f"laconic measure: {expected_file} is not UTF-8 text: {error}", file=sys.stderr)
        return EXIT_NO_EXPECTATION
    except json.JSONDecodeError as error:
        print(f"laconic measure: {expected_file} is not valid JSON: {error}", file=sys.stderr)
        return EXIT_NO_EXPECTATION
    if not isinstance(loaded, dict):
        print(
            f"laconic measure: {expected_file} is not an expected-values object",
            file=sys.stderr,
        )
        return EXIT_NO_EXPECTATION
    differences = compare_expectation(loaded, measured)
    if differences:
        print(f"\nMeasurement differs from {expected_file}:", file=sys.stderr)
        for difference in differences:
            print(f"  {difference}", file=sys.stderr)
        return EXIT_MISMATCH
    print(f"\nMeasurement matches {expected_file}.")
    return EXIT_OK


def _report(result: CorpusScan) -> None:
    cost = session_cost(result.usage)
    _report_models(result.usage)
    _report_cost_split(cost)
    _report_channels(result.channels)
    _report_prose_economics(result.channels, cost)
    _report_prose_distribution(result.channels)
    _report_top_tools(result.channels)


def _report_models(usage: dict[str, ModelUsage]) -> None:
    print(f"{'model':22}{'turns':>8}{'out tok':>12}{'$out':>10}{'$total':>10}")
    for model, model_usage in sorted(usage.items(), key=lambda kv: (-kv[1].output_tokens, kv[0])):
        model_cost = model_usage.cost(model)
        print(
            f"{model:22}{model_usage.turns:>8}{model_usage.output_tokens:>12,}"
            f"{model_cost.output:>10.2f}{model_cost.total:>10.2f}"
        )


def _report_cost_split(cost: CostBreakdown) -> None:
    shares = cost.shares()
    print("\nCost decomposition (share of modelled spend):")
    for label, share in (
        ("cache reads (resident context)", shares.cache_read),
        ("cache writes", shares.cache_write),
        ("output tokens", shares.output),
        ("uncached input", shares.uncached_input),
    ):
        print(f"  {label:34}{share:>21.2f}%")
    print(f"  {'total':34}{shares.total:>21.2f}%")


def _report_channels(channels: Channels) -> None:
    total = channels.total
    print("\nChannel volume (characters entering the context window):")
    for label, value in (
        ("tool results (observations)", channels.tool_results),
        ("tool_use args (actions)", channels.tool_args),
        ("assistant prose (human-facing)", channels.prose),
        ("human prompts", channels.user_prompts),
    ):
        print(f"  {label:34}{value:>13,}{100 * value / total:>8.2f}%")


def _report_prose_economics(channels: Channels, cost: CostBreakdown) -> None:
    prose_share_of_output = channels.prose / channels.emitted
    output_share_of_cost = cost.output / cost.total
    prose_share_of_cost = output_share_of_cost * prose_share_of_output

    print(f"\nTotal spend                          ${cost.total:>12,.2f}")
    print(f"Output share of spend                 {100 * output_share_of_cost:>12.2f}%")
    print(f"Prose share of emitted output         {100 * prose_share_of_output:>12.2f}%")
    print(f"HUMAN-FACING PROSE SHARE OF SPEND     {100 * prose_share_of_cost:>12.2f}%")
    for ratio in _COMPRESSION_RATIOS:
        print(
            f"  prose compressed {int(ratio * 100):>3}% -> total saving "
            f"{100 * prose_share_of_cost * ratio:.2f}%"
        )


def _report_prose_distribution(channels: Channels) -> None:
    per_turn = sorted(channels.prose_per_turn)
    if not per_turn:
        return
    count = len(per_turn)

    def percentile(quantile: float) -> int:
        return per_turn[min(count - 1, int(count * quantile))]

    zero = sum(1 for value in per_turn if value == 0)
    print(
        f"\nProse chars/turn: p50={percentile(0.5)} p90={percentile(0.9)} "
        f"p99={percentile(0.99)} max={per_turn[-1]}"
    )
    print(f"Turns emitting zero prose: {100 * zero / count:.1f}%")


def _report_top_tools(channels: Channels) -> None:
    print("\nTop tools by observation volume:")
    for name, chars in channels.result_chars_by_tool.most_common(8):
        calls = channels.calls_by_tool[name]
        mean = f"{chars // calls:,}" if calls else "n/a"
        print(f"  {name:26}{chars:>12,} chars  calls={calls:<6} mean={mean}")
