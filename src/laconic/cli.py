"""Command-line entrypoint for Laconic."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Literal

from laconic import __version__
from laconic.codec.encoders.file import FileEncoder
from laconic.codec.observe import ObservationCodec
from laconic.costs import CostBreakdown, ModelUsage, session_cost, unpriced_models
from laconic.ledger import Ledger
from laconic.replay.corpus import (
    Channels,
    CorpusScan,
    EmptyCorpusError,
    Expectation,
    JsonValue,
    MalformedRecordError,
    compare_expectation,
    expectation,
    find_transcripts,
    iter_records,
    scan,
    scan_corpus,
)
from laconic.replay.engine import (
    BaselineMismatchError,
    BaselineSession,
    CostCapExceededError,
    LiveModeConfigError,
    LiveReplayConfig,
    MissingRecordedResponseError,
    NetCostReport,
    RecordedResponseSession,
    ReplayClient,
    assert_baseline,
    find_baseline_transcripts,
    iter_turns,
    load_recorded_response,
    net_cost,
    recorded_response_path,
    replay_live,
    replay_off,
)
from laconic.replay.equivalence import SessionEquivalence, compare_session

DEFAULT_CORPUS = Path.home() / ".claude" / "projects"

#: Compression ratios reported as hypothetical savings on the prose channel.
_COMPRESSION_RATIOS = (0.44, 0.65, 0.90, 1.00)

#: argparse owns 2 for usage errors, so domain failures start at 3.
EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_NO_CORPUS = 3
EXIT_NO_EXPECTATION = 4
EXIT_MALFORMED_RECORD = 5
EXIT_REPORT_REQUIRES_CODEC = 6
EXIT_BASELINE_MISMATCH = 7
EXIT_ASSERT_BASELINE_REQUIRES_CODEC_OFF = 8
EXIT_MISSING_RECORDED_RESPONSE = 9
EXIT_LIVE_CONFIG_ERROR = 10
EXIT_COST_CAP_EXCEEDED = 11
EXIT_CLIENT_IMPORT_ERROR = 12


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
    measure.add_argument(
        "--codec",
        choices=["on", "off"],
        default="off",
        help="engage the observation codec (currently: file reads only) before reporting",
    )
    measure.add_argument(
        "--report",
        choices=["reduction"],
        help="print an additional codec report; 'reduction' requires --codec on",
    )
    measure.set_defaults(handler=_measure)

    replay = subcommands.add_parser(
        "replay",
        help="counterfactual cost and action equivalence, codec on vs off",
        description=(
            "Replay recorded session transcripts against the observation codec's "
            "counterfactual behaviour: baseline reproduction with the codec "
            "disabled, net cost and action equivalence with it enabled."
        ),
    )
    replay.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[DEFAULT_CORPUS],
        help=f"directories containing session transcripts (default: {DEFAULT_CORPUS})",
    )
    replay.add_argument(
        "--codec",
        choices=["on", "off"],
        default="off",
        help="engage the observation codec before replaying",
    )
    replay.add_argument(
        "--mode",
        choices=["recorded", "live"],
        default="recorded",
        help="'recorded' (default, CI-safe) reads a committed fixture; "
        "'live' calls a real model, opt-in only",
    )
    replay.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="report format",
    )
    replay.add_argument(
        "--assert-baseline",
        action="store_true",
        help="fail if codec=off replay does not reproduce each session's recorded cost",
    )
    replay.add_argument(
        "--model",
        help="model identifier for live replay (requires --mode live)",
    )
    replay.add_argument(
        "--cost-cap",
        type=float,
        metavar="USD",
        help="per-run USD cost cap for live replay (requires --mode live)",
    )
    replay.add_argument(
        "--artifact-dir",
        type=Path,
        metavar="DIR",
        help="directory for provenance-tagged live-replay artifacts (requires --mode live)",
    )
    replay.add_argument(
        "--client",
        metavar="MODULE:ATTR",
        help="dotted import path to a zero-arg callable returning a ReplayClient "
        "(requires --mode live; no concrete client ships with this package)",
    )
    replay.set_defaults(handler=_replay)
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
    if args.report is not None and args.codec != "on":
        print(
            f"laconic measure: --report {args.report} requires --codec on",
            file=sys.stderr,
        )
        return EXIT_REPORT_REQUIRES_CODEC

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
    exit_code = EXIT_OK
    if expected_file is not None:
        exit_code = _check_expectation(expected_file, expectation(result))

    if args.codec == "on" and args.report == "reduction":
        _report_reduction(paths)

    return exit_code


def _replay(args: argparse.Namespace) -> int:
    if args.assert_baseline and args.codec != "off":
        print("laconic replay: --assert-baseline requires --codec off", file=sys.stderr)
        return EXIT_ASSERT_BASELINE_REQUIRES_CODEC_OFF

    if args.mode == "live" and args.codec != "on":
        print("laconic replay: --mode live requires --codec on", file=sys.stderr)
        return EXIT_LIVE_CONFIG_ERROR

    live_only_flags_given = any(
        value is not None for value in (args.model, args.cost_cap, args.artifact_dir, args.client)
    )
    if live_only_flags_given and args.mode != "live":
        print(
            "laconic replay: --model/--cost-cap/--artifact-dir/--client require --mode live",
            file=sys.stderr,
        )
        return EXIT_LIVE_CONFIG_ERROR

    paths: list[Path] = list(args.paths)
    if args.codec == "off":
        return _replay_off_cli(paths, args)
    return _replay_on_cli(paths, args)


def _replay_off_cli(paths: list[Path], args: argparse.Namespace) -> int:
    try:
        sessions = replay_off(paths)
    except EmptyCorpusError as error:
        print(f"laconic replay: {error}", file=sys.stderr)
        return EXIT_NO_CORPUS
    except MalformedRecordError as error:
        print(f"laconic replay: {error}", file=sys.stderr)
        return EXIT_MALFORMED_RECORD
    except OSError as error:
        print(f"laconic replay: cannot read the corpus: {error}", file=sys.stderr)
        return EXIT_NO_CORPUS

    if not sessions:
        listed = ", ".join(str(path) for path in paths)
        print(f"laconic replay: no *.jsonl transcripts found under {listed}", file=sys.stderr)
        return EXIT_NO_CORPUS

    _warn_unpriced_models([session.path for session in sessions])

    if args.assert_baseline:
        try:
            assert_baseline(sessions)
        except BaselineMismatchError as error:
            print(f"laconic replay: {error}", file=sys.stderr)
            return EXIT_BASELINE_MISMATCH

    if args.format == "json":
        _print_json_baseline(sessions)
    else:
        _report_baseline(sessions)
    return EXIT_OK


#: One net-cost report plus the structural equivalence rate for one
#: baseline transcript -- what every `--codec on` reporting path builds.
type _NetEntry = tuple[Path, NetCostReport, SessionEquivalence]


def _replay_on_cli(paths: list[Path], args: argparse.Namespace) -> int:
    try:
        baselines = find_baseline_transcripts(paths)
    except EmptyCorpusError as error:
        print(f"laconic replay: {error}", file=sys.stderr)
        return EXIT_NO_CORPUS
    except OSError as error:
        print(f"laconic replay: cannot read the corpus: {error}", file=sys.stderr)
        return EXIT_NO_CORPUS

    if not baselines:
        listed = ", ".join(str(path) for path in paths)
        print(f"laconic replay: no *.jsonl transcripts found under {listed}", file=sys.stderr)
        return EXIT_NO_CORPUS

    if args.mode == "live":
        return _replay_on_live_cli(baselines, args)
    return _replay_on_recorded_cli(baselines, args)


def _replay_on_recorded_cli(baselines: list[Path], args: argparse.Namespace) -> int:
    entries: list[_NetEntry] = []
    scanned_paths: list[Path] = []
    for baseline in baselines:
        try:
            session = load_recorded_response(baseline)
            entry_or_code = _score_session(baseline, session)
        except MissingRecordedResponseError as error:
            print(f"laconic replay: {error}", file=sys.stderr)
            return EXIT_MISSING_RECORDED_RESPONSE
        except MalformedRecordError as error:
            print(f"laconic replay: {error}", file=sys.stderr)
            return EXIT_MALFORMED_RECORD
        except OSError as error:
            print(f"laconic replay: cannot read the corpus: {error}", file=sys.stderr)
            return EXIT_NO_CORPUS
        if isinstance(entry_or_code, int):
            return entry_or_code
        entries.append(entry_or_code)
        scanned_paths.extend((baseline, session.fixture))

    _warn_unpriced_models(scanned_paths)
    if args.format == "json":
        _print_json_net(entries)
    else:
        _report_net(entries)
    return EXIT_OK


def _replay_on_live_cli(baselines: list[Path], args: argparse.Namespace) -> int:
    missing_live_config = (
        args.model is None
        or args.cost_cap is None
        or args.artifact_dir is None
        or args.client is None
    )
    if missing_live_config:
        print(
            "laconic replay: --mode live requires --model, --cost-cap, "
            "--artifact-dir, and --client",
            file=sys.stderr,
        )
        return EXIT_LIVE_CONFIG_ERROR

    try:
        client = _load_client(args.client)
    except (ImportError, AttributeError, TypeError, ValueError) as error:
        print(f"laconic replay: cannot load --client {args.client!r}: {error}", file=sys.stderr)
        return EXIT_CLIENT_IMPORT_ERROR

    try:
        config = LiveReplayConfig(model=args.model, cost_cap_usd=args.cost_cap, client=client)
    except LiveModeConfigError as error:
        print(f"laconic replay: {error}", file=sys.stderr)
        return EXIT_LIVE_CONFIG_ERROR

    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    entries: list[_NetEntry] = []
    scanned_paths: list[Path] = []
    for baseline in baselines:
        artifact_path = args.artifact_dir / recorded_response_path(baseline).name
        try:
            observations = _build_observations(baseline, codec="on")
            session = replay_live(
                baseline, config, artifact_path=artifact_path, observations=observations
            )
            entry_or_code = _score_session(baseline, session, include_indices=set(observations))
        except CostCapExceededError as error:
            print(f"laconic replay: {error}", file=sys.stderr)
            return EXIT_COST_CAP_EXCEEDED
        except MalformedRecordError as error:
            print(f"laconic replay: {error}", file=sys.stderr)
            return EXIT_MALFORMED_RECORD
        except OSError as error:
            print(f"laconic replay: cannot read the corpus: {error}", file=sys.stderr)
            return EXIT_NO_CORPUS
        if isinstance(entry_or_code, int):
            return entry_or_code
        entries.append(entry_or_code)
        scanned_paths.extend((baseline, artifact_path))

    _warn_unpriced_models(scanned_paths)
    if args.format == "json":
        _print_json_net(entries)
    else:
        _report_net(entries)
    return EXIT_OK


def _score_session(
    baseline: Path,
    session: RecordedResponseSession,
    *,
    include_indices: set[int] | None = None,
) -> _NetEntry | int:
    """Compute a :class:`_NetEntry` for ``baseline`` and its recorded
    response ``session``, or return an exit code on a comparison defect.

    A comparison-length mismatch is a malformed fixture, not a crash-worthy
    bug: it is reported through the normal ``laconic replay: ...`` stderr
    convention and mapped to :data:`EXIT_MISMATCH`, the same code
    ``laconic measure`` uses for a comparison that found a real drift.

    ``include_indices``, when given, restricts the baseline action
    sequence to exactly the turn indices ``session`` actually covers --
    the live path passes the same index set :func:`_build_observations`
    used, since a baseline turn with no preceding observation (the
    session's first action) was never counterfactually replayed and has
    nothing to be compared against. A recorded-response fixture loaded
    from a committed file carries no such gap: its author is expected to
    provide one non-induced turn per baseline action turn, so the
    recorded path leaves ``include_indices`` at its default of "every
    turn with an action."
    """
    report = net_cost(baseline, session)
    baseline_actions = tuple(
        turn.actions[-1]
        for turn in iter_turns(baseline)
        if turn.actions and (include_indices is None or turn.index in include_indices)
    )
    try:
        equivalence = compare_session(baseline_actions, session.non_induced_actions)
    except ValueError as error:
        print(f"laconic replay: {baseline}: {error}", file=sys.stderr)
        return EXIT_MISMATCH
    return (baseline, report, equivalence)


def _load_client(spec: str) -> ReplayClient:
    """Resolve ``"module.path:attr"`` to a zero-arg callable and call it.

    No concrete :class:`~laconic.replay.engine.ReplayClient` ships with
    this package (CONSTRAINTS keeps dependencies minimal and live replay
    opt-in); this is how a caller wires their own without laconic needing
    a network SDK dependency of its own.
    """
    module_name, sep, attr_name = spec.partition(":")
    if not sep:
        raise ValueError(f"expected MODULE:ATTR, got {spec!r}")
    module = importlib.import_module(module_name)
    factory = getattr(module, attr_name)
    if not callable(factory):
        raise ValueError(f"{spec!r} resolved to a non-callable {factory!r}")
    client: ReplayClient = factory()
    if not callable(getattr(client, "respond", None)):
        raise ValueError(f"{spec!r} did not return a ReplayClient (no callable .respond)")
    return client


def _subject_for(tool_name: str, tool_input: Mapping[str, JsonValue]) -> str:
    del tool_name  # every known tool's subject key is content-addressed, not name-addressed
    for key in ("path", "command", "pattern", "query"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(tool_input, sort_keys=True)


def _tool_results_by_id(path: Path) -> dict[str, str]:
    """Map every ``tool_use_id`` in ``path`` to its tool result text."""
    results: dict[str, str] = {}
    for _, record in iter_records(path):
        if record is None or record.get("type") != "user":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            body = block.get("content")
            if isinstance(tool_use_id, str) and isinstance(body, str):
                results[tool_use_id] = body
    return results


def _build_observations(baseline: Path, *, codec: Literal["on", "off"]) -> dict[int, str]:
    """Return the observation text that precedes each action turn in
    ``baseline``, codec-encoded when ``codec == "on"``.

    Turn 0 has no preceding observation and is never included; neither is
    any turn whose preceding action's result this transcript never
    recorded (an unmatched ``tool_use_id``, which a well-formed transcript
    never has, but a live replay run must not crash on regardless).
    """
    results = _tool_results_by_id(baseline)
    turns = list(iter_turns(baseline))
    observations: dict[int, str] = {}
    ledger = Ledger(":memory:", "replay-live-observations")
    codec_engine = ObservationCodec(ledger) if codec == "on" else None
    try:
        for index in range(1, len(turns)):
            if not turns[index].actions:
                continue
            preceding = turns[index - 1].actions
            if not preceding:
                continue
            action = preceding[-1]
            raw = results.get(action.tool_use_id)
            if raw is None:
                continue
            if codec_engine is None:
                observations[turns[index].index] = raw
                continue
            subject = _subject_for(action.tool_name, action.tool_input)
            record = codec_engine.encode(
                action.tool_name, subject, raw, action.tool_input, turn=index
            )
            observations[turns[index].index] = record.encoded
    finally:
        ledger.close()
    return observations


def _warn_unpriced_models(paths: Sequence[Path]) -> None:
    """Print the same "no published price" warning ``laconic measure``
    does, for every model actually billed across ``paths``.

    ``codec="on"`` net cost is a subtraction between a baseline and a
    fixture, potentially priced under two different tables whenever
    either file's model is unrecognised by :data:`laconic.costs.PRICING`
    -- including the literal ``"unknown"`` :func:`~laconic.replay.engine.iter_turns`
    substitutes for a missing or non-string ``model`` field. A savings
    figure computed silently across two different price tables is a
    reportable condition, not an implementation detail, matching
    ``docs/system-design.md``'s "Honest measurement" constraint the rest
    of this module holds to.
    """
    models: set[str] = set()
    for path in paths:
        models.update(scan([path]).usage.keys())
    guessed = unpriced_models({model: ModelUsage() for model in models})
    if guessed:
        print(
            f"warning: no published price for {', '.join(guessed)}; billed at the fallback rate",
            file=sys.stderr,
        )


def _report_baseline(sessions: Sequence[BaselineSession]) -> None:
    print(f"Replayed {len(sessions)} session transcript(s), codec=off (baseline reproduction):\n")
    total_turns = 0
    total_usd = 0.0
    for session in sessions:
        print(
            f"  {session.path}  turns={session.cost.turns:<6} cost=${session.cost.cost.total:.4f}"
        )
        total_turns += session.cost.turns
        total_usd += session.cost.cost.total
    print(f"\n  total turns: {total_turns:,}")
    print(f"  total cost:  ${total_usd:.4f}")


def _print_json_baseline(sessions: Sequence[BaselineSession]) -> None:
    payload: dict[str, JsonValue] = {
        "codec": "off",
        "sessions": [
            {
                "path": str(session.path),
                "turns": session.cost.turns,
                "cost_usd": session.cost.cost.total,
            }
            for session in sessions
        ],
        "total_turns": sum(session.cost.turns for session in sessions),
        "total_cost_usd": sum(session.cost.cost.total for session in sessions),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def _report_net(entries: Sequence[_NetEntry]) -> None:
    print(
        f"Replayed {len(entries)} session transcript(s), codec=on "
        "(net cost and action equivalence):\n"
    )
    total_baseline = 0.0
    total_codec_on = 0.0
    for baseline, report, equivalence in entries:
        print(f"  {baseline}")
        print(f"    baseline cost:   ${report.baseline.cost.total:.4f}")
        print(f"    codec-on cost:   ${report.codec_on.cost.total:.4f}")
        print(f"    net savings:     ${report.net_savings_usd:.4f} ({report.net_savings_pct:.2f}%)")
        print(f"    induced turns:   {report.induced_turns} (${report.induced_cost_usd:.4f})")
        print(
            f"    equivalence:     {100 * equivalence.rate:.2f}% "
            f"({len(equivalence.divergences)}/{len(equivalence.comparisons)} divergent)"
        )
        total_baseline += report.baseline.cost.total
        total_codec_on += report.codec_on.cost.total
    total_net = total_baseline - total_codec_on
    pct = 100 * total_net / total_baseline if total_baseline > 0 else 0.0
    print(f"\n  total baseline cost: ${total_baseline:.4f}")
    print(f"  total codec-on cost: ${total_codec_on:.4f}")
    print(f"  total net savings:   ${total_net:.4f} ({pct:.2f}%)")


def _print_json_net(entries: Sequence[_NetEntry]) -> None:
    payload: dict[str, JsonValue] = {
        "codec": "on",
        "sessions": [
            {
                "path": str(baseline),
                "baseline_cost_usd": report.baseline.cost.total,
                "codec_on_cost_usd": report.codec_on.cost.total,
                "net_savings_usd": report.net_savings_usd,
                "net_savings_pct": report.net_savings_pct,
                "induced_turns": report.induced_turns,
                "induced_cost_usd": report.induced_cost_usd,
                "equivalence_rate": equivalence.rate,
                "divergent_turns": len(equivalence.divergences),
                "compared_turns": len(equivalence.comparisons),
            }
            for baseline, report, equivalence in entries
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


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


def _report_reduction(paths: Sequence[Path]) -> None:
    """Run the file observation encoder over every ``Read`` result under
    ``paths`` and report gross encoded volume against raw volume.

    This is a *gross* comparison: it never subtracts follow-up reads the
    codec might induce. Net accounting is ``laconic replay``'s job, added in
    M8 — reporting it here would let this milestone claim a savings number
    it has no way to have earned honestly.
    """
    reads = 0
    seen_handles: set[str] = set()
    raw_total = 0
    encoded_total = 0
    with Ledger(":memory:", "measure-reduction") as ledger:
        encoder = FileEncoder(ledger)
        for turn, (subject, raw, request) in enumerate(_iter_file_reads(paths)):
            record = encoder.encode(subject, raw, request, turn=turn)
            reads += 1
            if record.handle in seen_handles:
                continue
            seen_handles.add(record.handle)
            raw_total += record.raw_chars
            encoded_total += record.encoded_chars

    print("\nFile observation encoder — gross reduction (induced reads: see M8):")
    print(f"  Read tool results seen:   {reads:,}")
    print(f"  unique payloads encoded:  {len(seen_handles):,}")
    if raw_total == 0:
        print("  no Read observations found in this corpus")
        return
    reduction = 100 * (1 - encoded_total / raw_total)
    print(f"  raw volume:      {raw_total:>10,} chars")
    print(f"  encoded volume:  {encoded_total:>10,} chars")
    print(f"  reduction:       {reduction:>9.2f}%")


def _iter_file_reads(paths: Sequence[Path]) -> Iterator[tuple[str, str, dict[str, JsonValue]]]:
    """Yield ``(subject, raw content, tool input)`` for every ``Read`` tool
    result found under ``paths``, matched by ``tool_use_id``.
    """
    for transcript in find_transcripts(list(paths)):
        pending: dict[str, tuple[str, dict[str, JsonValue]]] = {}
        for _, record in iter_records(transcript):
            if record is None:
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            record_type = record.get("type")
            if record_type == "assistant" and isinstance(content, list):
                for block in content:
                    if not (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") == "Read"
                    ):
                        continue
                    tool_input = block.get("input")
                    tool_id = block.get("id")
                    path = tool_input.get("path") if isinstance(tool_input, dict) else None
                    if (
                        isinstance(path, str)
                        and isinstance(tool_id, str)
                        and isinstance(tool_input, dict)
                    ):
                        pending[tool_id] = (path, tool_input)
            elif record_type == "user" and isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    tool_id = block.get("tool_use_id")
                    entry = pending.pop(tool_id, None) if isinstance(tool_id, str) else None
                    if entry is None:
                        continue
                    body = block.get("content")
                    if isinstance(body, str):
                        subject, request = entry
                        yield subject, body, request
