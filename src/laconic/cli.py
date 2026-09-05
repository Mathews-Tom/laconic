"""Command-line entrypoint for Laconic."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Literal

from laconic import __version__
from laconic.codec.encoders.file import FileEncoder
from laconic.codec.observe import ObservationCodec, subject_for
from laconic.costs import CostBreakdown, ModelUsage, session_cost, unpriced_models
from laconic.gates.protocol import GateSuiteResult
from laconic.gates.reasoning_accuracy import ReasoningAccuracyFixtureError
from laconic.gates.runner import UnknownGateError, run_gates
from laconic.k1corpus.report import (
    AUTHORIZED_PROVIDERS,
    DEFAULT_LEDGER_PATH,
    Disposition,
    StageAReport,
    scan_all_providers,
    write_ledger,
)
from laconic.k1corpus.stage_a import AUTHORIZED_ROOTS
from laconic.k1corpus.stage_b import (
    DEFAULT_CORPUS_MANIFEST_PATH,
    DEFAULT_SESSION_MANIFEST_PATH,
    FrozenCorpus,
    ManifestSet,
    TotalsMismatchError,
    build_session_manifest,
    write_session_manifest,
)
from laconic.k1corpus.stage_c import (
    DEFAULT_STAGE_C_ROOT,
    LiveStageCSessionRunner,
    StageCAudit,
    StageCLedger,
    StageCManifestError,
    audit_retailogists_exclusions,
    load_stage_c_manifest,
    run_resumable_batch,
)
from laconic.k1corpus.stage_c_report import generate_stage_c_report
from laconic.ledger import InvalidSpanError, Ledger, UnknownHandleError
from laconic.observe.audit import DEFAULT_AUDIT_PATH
from laconic.observe.contracts import ClientId
from laconic.observe.installer import (
    ConfigParseError,
    OwnershipConflictError,
    apply_claude_code_install,
    apply_claude_code_remove,
    apply_omp_install,
    apply_omp_remove,
    preview_claude_code,
    preview_omp,
)
from laconic.observe.preview import InstallPlan
from laconic.observe.status import compute_report, compute_status
from laconic.render.narrate import (
    NarrationConfig,
    NarrationConfigurationError,
    NarrationResponseError,
    NarrationUnavailableError,
    provider_for,
)
from laconic.render.templates import render, render_narration
from laconic.render.view import (
    UnmatchedToolResultError,
    UnsupportedToolResultError,
    assemble,
    load_fixture_ledger,
)
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
from laconic.runtime.omp_installer import (
    OBSERVE_OWNED_MARKER,
    OMP_EXTENSION_FILENAME,
    OMP_OWNED_MARKER,
    OmpInstallError,
    OmpInstallPlan,
)
from laconic.runtime.omp_installer import (
    apply_omp_install as apply_runtime_omp_install,
)
from laconic.runtime.omp_installer import (
    apply_omp_uninstall as apply_runtime_omp_uninstall,
)
from laconic.runtime.omp_installer import (
    omp_extensions_directory as runtime_omp_extensions_directory,
)
from laconic.runtime.omp_installer import (
    preview_omp_install as preview_runtime_omp_install,
)
from laconic.runtime.omp_installer import (
    preview_omp_uninstall as preview_runtime_omp_uninstall,
)
from laconic.runtime.operator import (
    PurgePlan,
    RuntimeStorageStatus,
    apply_purge,
    parse_duration,
    preview_purge_older_than,
    preview_purge_session,
    runtime_storage_status,
)
from laconic.runtime.storage import UnsafeStoragePathError
from laconic.study.analysis import MINIMUM_PARTICIPANTS
from laconic.study.dryrun import DEFAULT_PARTICIPANT_COUNT, DryRunResult
from laconic.study.dryrun import run as run_study_dry_run
from laconic.study.dryrun import to_json as study_dry_run_to_json

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
EXIT_UNKNOWN_GATE = 13
EXIT_REASONING_ACCURACY_FIXTURE = 14
EXIT_RENDER_TRACE = 15
EXIT_NARRATION_CONFIG = 16
EXIT_NARRATION_RESPONSE = 17
EXIT_STUDY_OUTPUT_ERROR = 18
EXIT_STUDY_INSUFFICIENT_PARTICIPANTS = 19
EXIT_OBSERVE_CONFIG_PARSE_ERROR = 20
EXIT_OBSERVE_OWNERSHIP_CONFLICT = 21
EXIT_K1_STAGE_A_STOP = 22
EXIT_K1_STAGE_B_TOTALS_MISMATCH = 23
EXIT_K1_STAGE_C_INCOMPLETE = 24
EXIT_OMP_INSTALL_ERROR = 25
EXIT_RUNTIME_STORAGE_ERROR = 26


def build_parser() -> argparse.ArgumentParser:
    """Build the currently available CLI surface."""
    parser = argparse.ArgumentParser(description="A context-loop codec for coding agents.")
    parser.add_argument("--version", action="version", version=f"laconic {__version__}")
    subcommands = parser.add_subparsers(dest="command")
    install = subcommands.add_parser(
        "install",
        help="install an owned runtime adapter",
    )
    install_subcommands = install.add_subparsers(dest="install_target")
    install_omp = install_subcommands.add_parser(
        "omp",
        help="install Laconic's OMP runtime extension",
        description=(
            "Preview or atomically install one Laconic-owned native OMP extension. "
            "The project scope is the default. This command never contacts a provider."
        ),
    )
    install_omp.add_argument("--scope", choices=["project", "user"], default="project")
    install_omp.add_argument("--profile", help="named OMP profile for user scope")
    install_omp.add_argument(
        "--user-dir",
        type=Path,
        help="explicit user-scope extension directory",
    )
    install_omp.add_argument(
        "--python",
        help="absolute interpreter recorded by the extension (default: this interpreter)",
    )
    install_omp.add_argument(
        "--data-dir",
        type=Path,
        help="runtime ledger directory recorded by the extension",
    )
    install_omp.add_argument("--dry-run", action="store_true", help="preview only; never write")
    install_omp.add_argument("--format", choices=["text", "json"], default="text")
    install_omp.set_defaults(handler=_runtime_omp_install)

    uninstall = subcommands.add_parser(
        "uninstall",
        help="remove an owned runtime adapter without purging data",
    )
    uninstall_subcommands = uninstall.add_subparsers(dest="uninstall_target")
    uninstall_omp = uninstall_subcommands.add_parser(
        "omp",
        help="remove Laconic's owned OMP runtime extension",
        description=(
            "Preview or remove only Laconic's marked OMP extension. "
            "Runtime ledgers are never purged by uninstall."
        ),
    )
    uninstall_omp.add_argument("--scope", choices=["project", "user"], default="project")
    uninstall_omp.add_argument("--profile", help="named OMP profile for user scope")
    uninstall_omp.add_argument(
        "--user-dir",
        type=Path,
        help="explicit user-scope extension directory",
    )
    uninstall_omp.add_argument("--dry-run", action="store_true", help="preview only; never write")
    uninstall_omp.add_argument("--format", choices=["text", "json"], default="text")
    uninstall_omp.set_defaults(handler=_runtime_omp_uninstall)
    status = subcommands.add_parser(
        "status",
        help="inspect OMP adapter and content-free runtime storage health",
    )
    status.add_argument("--profile", help="named OMP profile to inspect")
    status.add_argument("--user-dir", type=Path, help="explicit user extension directory")
    status.add_argument("--data-dir", type=Path, help="runtime storage root")
    status.add_argument("--format", choices=["text", "json"], default="text")
    status.set_defaults(handler=_runtime_status)

    purge = subcommands.add_parser(
        "purge",
        help="explicitly delete selected runtime recovery ledgers",
        description=(
            "Delete one session ledger or whole ledgers older than a duration. "
            "Uninstall never performs this operation."
        ),
    )
    purge_selector = purge.add_mutually_exclusive_group(required=True)
    purge_selector.add_argument("--session", help="exact OMP session id")
    purge_selector.add_argument(
        "--older-than",
        metavar="DURATION",
        help="positive duration such as 24h, 30d, or 4w",
    )
    purge.add_argument("--data-dir", type=Path, help="runtime storage root")
    purge.add_argument("--dry-run", action="store_true", help="preview only; never delete")
    purge.add_argument("--format", choices=["text", "json"], default="text")
    purge.set_defaults(handler=_runtime_purge)

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

    gates = subcommands.add_parser(
        "gates",
        help="run product evaluation criteria and print pass/fail",
        description="Evaluate the codec against committed transcript-corpus fixtures.",
    )
    gates.add_argument(
        "--corpus",
        type=Path,
        default=DEFAULT_CORPUS,
        help=f"directory containing session transcripts (default: {DEFAULT_CORPUS})",
    )
    gates.add_argument(
        "--only",
        metavar="NAME,...",
        help="comma-separated criterion names (default: every criterion this build knows)",
    )
    gates.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="report format",
    )
    gates.set_defaults(handler=_gates)

    expand = subcommands.add_parser(
        "expand",
        help="resolve a ledger handle or line span from a transcript corpus",
    )
    expand.add_argument("reference", metavar="HANDLE[:FIRST-LAST]")
    expand.add_argument(
        "--corpus",
        type=Path,
        default=DEFAULT_CORPUS,
        help=f"directory containing session transcripts (default: {DEFAULT_CORPUS})",
    )
    expand.set_defaults(handler=_expand)

    view = subcommands.add_parser(
        "view",
        help="render a structural trace from a transcript corpus",
    )
    view.add_argument(
        "--turns",
        type=_turn_range,
        required=True,
        metavar="FIRST-LAST",
        help="inclusive, one-based turn range",
    )
    view.add_argument(
        "--corpus",
        type=Path,
        default=DEFAULT_CORPUS,
        help=f"directory containing session transcripts (default: {DEFAULT_CORPUS})",
    )
    view.add_argument(
        "--deterministic-only",
        action="store_true",
        help="render structural facts only; never invoke a narration provider",
    )
    view.add_argument(
        "--provider",
        choices=("none", "ollama"),
        default="none",
        help="optional local narration provider (default: none)",
    )
    view.add_argument(
        "--provider-endpoint",
        help="local provider endpoint; required when --provider ollama",
    )
    view.add_argument(
        "--provider-model",
        help="local provider model; required when --provider ollama",
    )
    view.add_argument(
        "--provider-timeout",
        type=float,
        default=5.0,
        help="seconds to wait for a narration provider (default: 5)",
    )
    view.set_defaults(handler=_view)

    study = subcommands.add_parser(
        "study",
        help="human-study harness (docs/system-design.md §4.1)",
    )
    study_subcommands = study.add_subparsers(dest="study_command")
    study_dry_run = study_subcommands.add_parser(
        "dry-run",
        help="run the full harness with simulated participants",
        description=(
            "Build seeded-defect materials, assign counterbalanced conditions, "
            "simulate participant responses, and run the pre-registered analysis "
            "-- producing an analysis-ready dataset without any real participant."
        ),
    )
    study_dry_run.add_argument("--seed", type=int, default=0, help="RNG seed (default: 0)")
    study_dry_run.add_argument(
        "--participants",
        type=int,
        default=DEFAULT_PARTICIPANT_COUNT,
        help=f"simulated participant count (default: {DEFAULT_PARTICIPANT_COUNT})",
    )
    study_dry_run.add_argument(
        "--out",
        type=Path,
        required=True,
        metavar="FILE",
        help="output path for the analysis-ready dataset (JSON)",
    )
    study_dry_run.set_defaults(handler=_study_dry_run)

    observe = subcommands.add_parser(
        "observe",
        help="local, content-free hook receipts and audit (docs/observe-design.md)",
    )
    observe_subcommands = observe.add_subparsers(dest="observe_command")

    observe_install = observe_subcommands.add_parser(
        "install",
        help="preview or write Observe's owned hook entry/extension file",
        description=(
            "Preview, or atomically write, a single Observe-owned hook entry "
            "(Claude Code) or extension file (OMP). Never enables the codec, "
            "changes K1 status, or contacts a provider."
        ),
    )
    observe_install.add_argument("--client", choices=["claude-code", "omp"], required=True)
    observe_install.add_argument("--scope", choices=["project", "user"], default="project")
    observe_install.add_argument("--dry-run", action="store_true", help="preview only; never write")
    observe_install.add_argument(
        "--user-dir",
        type=Path,
        help="override the user-scope target (required for a non-default OMP profile)",
    )
    observe_install.add_argument(
        "--python", help="interpreter path installed hooks invoke (default: this interpreter)"
    )
    observe_install.add_argument("--format", choices=["text", "json"], default="text")
    observe_install.set_defaults(handler=_observe_install)

    observe_remove = observe_subcommands.add_parser(
        "remove",
        help="preview or remove Observe's owned hook entry/extension file",
    )
    observe_remove.add_argument("--client", choices=["claude-code", "omp"], required=True)
    observe_remove.add_argument("--scope", choices=["project", "user"], default="project")
    observe_remove.add_argument("--dry-run", action="store_true", help="preview only; never write")
    observe_remove.add_argument("--user-dir", type=Path, help="override the user-scope target")
    observe_remove.add_argument("--format", choices=["text", "json"], default="text")
    observe_remove.set_defaults(handler=_observe_remove)

    observe_status = observe_subcommands.add_parser(
        "status", help="local receipt count and audit-chain integrity"
    )
    observe_status.add_argument(
        "--audit-path",
        type=Path,
        default=None,
        help=f"default: {DEFAULT_AUDIT_PATH}",
    )
    observe_status.add_argument("--format", choices=["text", "json"], default="text")
    observe_status.set_defaults(handler=_observe_status)

    observe_report = observe_subcommands.add_parser(
        "report", help="local receipt breakdown by adapter/category/result"
    )
    observe_report.add_argument(
        "--audit-path",
        type=Path,
        default=None,
        help=f"default: {DEFAULT_AUDIT_PATH}",
    )
    observe_report.add_argument("--format", choices=["text", "json"], default="text")
    observe_report.set_defaults(handler=_observe_report)

    k1 = subcommands.add_parser("k1", help="K1 representative-corpus governance tooling")
    k1_subcommands = k1.add_subparsers(dest="k1_command")
    k1_stage_a = k1_subcommands.add_parser("stage-a", help="Stage A metadata feasibility screening")
    k1_stage_a_subcommands = k1_stage_a.add_subparsers(dest="k1_stage_a_command")
    k1_stage_a_scan = k1_stage_a_subcommands.add_parser(
        "scan",
        help="scan authorized providers/roots and write a body-free metadata ledger",
        description=(
            "Enumerate historical Claude Code, Codex, and OMP session files under the "
            "two source roots authorized in .docs/DEVELOPMENT_PLAN_HISTORY.md H-53, "
            "admit only closed, unambiguously in-scope files, and write a body-free "
            "ledger plus a Stage A stop-condition disposition. Never reads a transcript "
            "body, prompt, tool result, assistant response, source file, credential, "
            "or title. A PROCEED_TO_STAGE_B_REQUEST disposition is not itself a Stage B "
            "authorization -- that remains a separate, explicit owner decision."
        ),
    )
    k1_stage_a_scan.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"default: {DEFAULT_LEDGER_PATH}",
    )
    k1_stage_a_scan.set_defaults(handler=_k1_stage_a_scan)

    k1_stage_b = k1_subcommands.add_parser(
        "stage-b", help="Stage B session-level manifest construction"
    )
    k1_stage_b_subcommands = k1_stage_b.add_subparsers(dest="k1_stage_b_command")
    k1_stage_b_build_manifest = k1_stage_b_subcommands.add_parser(
        "build-manifest",
        help="rebuild the session-level manifest from H-59's frozen lineage-level decision",
        description=(
            "Re-derive the specific sessions belonging to H-59's frozen design and "
            "confirmatory lineages, anchored to the freeze timestamp (never wall-clock "
            "time), and validate against H-59's recorded totals before writing anything. "
            "A totals mismatch is a hard stop, not a warning. Never reads a transcript "
            "body, prompt, tool result, assistant response, source file, credential, or "
            "title, and performs no live replay, provider call, or Stage C action."
        ),
    )
    k1_stage_b_build_manifest.add_argument(
        "--corpus-manifest",
        type=Path,
        default=None,
        help=f"default: {DEFAULT_CORPUS_MANIFEST_PATH}",
    )
    k1_stage_b_build_manifest.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"default: {DEFAULT_SESSION_MANIFEST_PATH}",
    )
    k1_stage_b_build_manifest.set_defaults(handler=_k1_stage_b_build_manifest)

    k1_stage_c = k1_subcommands.add_parser(
        "stage-c",
        help="run the explicitly authorized, resumable bounded live-replay batch",
    )
    k1_stage_c_subcommands = k1_stage_c.add_subparsers(dest="k1_stage_c_command")
    k1_stage_c_run = k1_stage_c_subcommands.add_parser(
        "run",
        help="run one selected Stage C manifest set and emit its protocol report",
        description=(
            "Runs only a user-selected frozen-manifest set through an explicitly supplied "
            "client. It records body-free ledger and audit state beneath --state-dir. "
            "This command does not authorize a live provider run."
        ),
    )
    k1_stage_c_run.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_SESSION_MANIFEST_PATH,
        help=f"default: {DEFAULT_SESSION_MANIFEST_PATH}",
    )
    k1_stage_c_run.add_argument(
        "--set", dest="selected_set", choices=["design", "confirmatory"], required=True
    )
    k1_stage_c_run.add_argument("--spend-cap", type=float, required=True)
    k1_stage_c_run.add_argument("--client", required=True, help="local MODULE:ATTR client factory")
    k1_stage_c_run.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STAGE_C_ROOT,
        help=f"default: {DEFAULT_STAGE_C_ROOT}",
    )
    k1_stage_c_run.add_argument("--format", choices=["text", "json"], default="text")
    k1_stage_c_run.set_defaults(handler=_k1_stage_c_run)
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


def _runtime_omp_target(args: argparse.Namespace) -> Path:
    if args.scope == "project":
        if args.profile is not None or args.user_dir is not None:
            raise OmpInstallError("--profile and --user-dir require --scope user")
    elif args.profile is not None and args.user_dir is not None:
        raise OmpInstallError("--profile and --user-dir are mutually exclusive")
    return runtime_omp_extensions_directory(
        scope=args.scope,
        cwd=Path.cwd(),
        home=Path.home(),
        user_dir=args.user_dir,
        profile=args.profile,
    )


def _print_runtime_omp_plan(
    plan: OmpInstallPlan,
    fmt: str,
    *,
    applied: bool,
    preview: bool,
) -> None:
    if fmt == "json":
        print(
            json.dumps(
                {
                    "adapter": "omp",
                    "operation": plan.operation,
                    "applied": applied,
                    "preview": preview,
                    "path": str(plan.path),
                    "python": plan.python,
                    "entrypoint": list(plan.entrypoint),
                    "data_directory": (
                        str(plan.data_directory) if plan.data_directory is not None else None
                    ),
                    "preserved": list(plan.preserved),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    state = "preview" if preview else ("applied" if applied else "unchanged")
    print(f"laconic OMP adapter ({state}): [{plan.operation}] {plan.path}")
    if plan.python is not None:
        print(f"  python: {plan.python}")
    print(f"  entrypoint: {' '.join(plan.entrypoint)}")
    if plan.data_directory is not None:
        print(f"  data directory: {plan.data_directory}")
    for item in plan.preserved:
        print(f"  [preserved] {item}")


def _runtime_omp_install(args: argparse.Namespace) -> int:
    try:
        directory = _runtime_omp_target(args)
        if args.dry_run:
            plan = preview_runtime_omp_install(
                directory,
                python=args.python,
                data_directory=args.data_dir,
            )
            _print_runtime_omp_plan(plan, args.format, applied=False, preview=True)
        else:
            result = apply_runtime_omp_install(
                directory,
                python=args.python,
                data_directory=args.data_dir,
            )
            _print_runtime_omp_plan(
                result.plan,
                args.format,
                applied=result.applied,
                preview=False,
            )
    except (OmpInstallError, OSError) as error:
        print(f"laconic install omp: {error}", file=sys.stderr)
        return EXIT_OMP_INSTALL_ERROR
    return EXIT_OK


def _runtime_omp_uninstall(args: argparse.Namespace) -> int:
    try:
        directory = _runtime_omp_target(args)
        if args.dry_run:
            plan = preview_runtime_omp_uninstall(directory)
            _print_runtime_omp_plan(plan, args.format, applied=False, preview=True)
        else:
            result = apply_runtime_omp_uninstall(directory)
            _print_runtime_omp_plan(
                result.plan,
                args.format,
                applied=result.applied,
                preview=False,
            )
    except (OmpInstallError, OSError) as error:
        print(f"laconic uninstall omp: {error}", file=sys.stderr)
        return EXIT_OMP_INSTALL_ERROR
    return EXIT_OK


def _runtime_adapter_state(directory: Path) -> str:
    if not directory.exists():
        return "not_installed"
    if directory.is_symlink() or not directory.is_dir():
        raise UnsafeStoragePathError(
            f"OMP extension path is not an ordinary directory: {directory}"
        )
    target = directory / OMP_EXTENSION_FILENAME
    markers = 0
    for path in directory.iterdir():
        if path.suffix not in (".ts", ".js"):
            continue
        if path.is_symlink() or not path.is_file():
            raise UnsafeStoragePathError(f"OMP extension entry is not an ordinary file: {path}")
        source = path.read_text(encoding="utf-8")
        if OMP_OWNED_MARKER in source or OBSERVE_OWNED_MARKER in source:
            markers += 1
    if markers > 1:
        return "duplicate"
    if target.exists():
        source = target.read_text(encoding="utf-8")
        return "installed" if OMP_OWNED_MARKER in source else "foreign_conflict"
    return "conflicting_adapter" if markers else "not_installed"


def _runtime_status_document(
    storage: RuntimeStorageStatus,
    *,
    project_adapter: str,
    user_adapter: str,
) -> dict[str, object]:
    return {
        "project_adapter": project_adapter,
        "user_adapter": user_adapter,
        "engine_health": "active-session-only; use /laconic status in OMP",
        "storage": {
            "path": str(storage.root),
            "exists": storage.exists,
            "bytes": storage.storage_bytes,
            "sessions": storage.sessions,
            "eligible_observations": storage.eligible_observations,
            "compressed_observations": storage.compressed_observations,
            "pass_through_observations": storage.pass_through_observations,
            "raw_chars": storage.raw_chars,
            "visible_chars": storage.visible_chars,
            "full_expansions": storage.full_expansions,
            "span_expansions": storage.span_expansions,
        },
    }


def _runtime_status(args: argparse.Namespace) -> int:
    try:
        if args.profile is not None and args.user_dir is not None:
            raise OmpInstallError("--profile and --user-dir are mutually exclusive")
        project_directory = runtime_omp_extensions_directory(
            scope="project",
            cwd=Path.cwd(),
            home=Path.home(),
        )
        user_directory = runtime_omp_extensions_directory(
            scope="user",
            cwd=Path.cwd(),
            home=Path.home(),
            user_dir=args.user_dir,
            profile=args.profile,
        )
        document = _runtime_status_document(
            runtime_storage_status(args.data_dir),
            project_adapter=_runtime_adapter_state(project_directory),
            user_adapter=_runtime_adapter_state(user_directory),
        )
    except (OmpInstallError, UnsafeStoragePathError, sqlite3.Error, OSError) as error:
        print(f"laconic status: {error}", file=sys.stderr)
        return EXIT_RUNTIME_STORAGE_ERROR
    if args.format == "json":
        print(json.dumps(document, indent=2, sort_keys=True))
        return EXIT_OK
    storage = document["storage"]
    assert isinstance(storage, dict)
    print("Laconic runtime status")
    print(f"  project adapter: {document['project_adapter']}")
    print(f"  user adapter: {document['user_adapter']}")
    print(f"  engine health: {document['engine_health']}")
    print(f"  storage: {storage['path']}")
    print(f"  sessions: {storage['sessions']}")
    print(
        "  decisions: "
        f"eligible={storage['eligible_observations']} "
        f"compressed={storage['compressed_observations']} "
        f"pass-through={storage['pass_through_observations']}"
    )
    print(f"  stored bytes: {storage['bytes']}")
    print(f"  expansions: full={storage['full_expansions']} span={storage['span_expansions']}")
    return EXIT_OK


def _print_purge_plan(plan: PurgePlan, fmt: str, *, applied: bool, deleted_files: int) -> None:
    document = {
        "selector": plan.selector,
        "storage": str(plan.root),
        "targets": [path.name for path in plan.targets],
        "sessions": len(plan.targets),
        "reclaim_bytes": plan.reclaim_bytes,
        "applied": applied,
        "deleted_files": deleted_files,
    }
    if fmt == "json":
        print(json.dumps(document, indent=2, sort_keys=True))
        return
    state = "applied" if applied else "preview"
    print(f"laconic purge ({state}): {plan.selector}")
    print(f"  sessions: {len(plan.targets)}")
    print(f"  reclaim bytes: {plan.reclaim_bytes}")
    for target in plan.targets:
        print(f"  [delete] {target.name}")


def _runtime_purge(args: argparse.Namespace) -> int:
    try:
        if args.session is not None:
            plan = preview_purge_session(args.session, args.data_dir)
        else:
            assert args.older_than is not None
            seconds = parse_duration(args.older_than)
            plan = preview_purge_older_than(
                seconds,
                args.data_dir,
                selector=f"older-than={args.older_than}",
            )
        if args.dry_run:
            _print_purge_plan(plan, args.format, applied=False, deleted_files=0)
        else:
            result = apply_purge(plan)
            _print_purge_plan(
                result.plan,
                args.format,
                applied=True,
                deleted_files=result.deleted_files,
            )
    except (UnsafeStoragePathError, sqlite3.Error, OSError, ValueError) as error:
        print(f"laconic purge: {error}", file=sys.stderr)
        return EXIT_RUNTIME_STORAGE_ERROR
    return EXIT_OK


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


def _load_client_factory(spec: str) -> Callable[[], ReplayClient]:
    """Resolve a local zero-argument ReplayClient factory without invoking it."""
    module_name, sep, attr_name = spec.partition(":")
    if not sep:
        raise ValueError(f"expected MODULE:ATTR, got {spec!r}")
    module = importlib.import_module(module_name)
    factory = getattr(module, attr_name)
    if not callable(factory):
        raise ValueError(f"{spec!r} resolved to a non-callable {factory!r}")

    def create() -> ReplayClient:
        client: ReplayClient = factory()
        if not callable(getattr(client, "respond", None)):
            raise ValueError(f"{spec!r} did not return a ReplayClient (no callable .respond)")
        return client

    return create


def _load_client(spec: str) -> ReplayClient:
    """Resolve ``"module.path:attr"`` to a zero-arg callable and call it.

    No concrete :class:`~laconic.replay.engine.ReplayClient` ships with
    this package (CONSTRAINTS keeps dependencies minimal and live replay
    opt-in); this is how a caller wires their own without laconic needing
    a network SDK dependency of its own.
    """
    return _load_client_factory(spec)()


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
            subject = subject_for(action.tool_input)
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


def _gates(args: argparse.Namespace) -> int:
    only = args.only.split(",") if args.only is not None else None
    try:
        suite = run_gates([args.corpus], only=only)
    except UnknownGateError as error:
        print(f"laconic gates: {error}", file=sys.stderr)
        return EXIT_UNKNOWN_GATE
    except EmptyCorpusError as error:
        print(f"laconic gates: {error}", file=sys.stderr)
        return EXIT_NO_CORPUS
    except MissingRecordedResponseError as error:
        print(f"laconic gates: {error}", file=sys.stderr)
        return EXIT_MISSING_RECORDED_RESPONSE
    except MalformedRecordError as error:
        print(f"laconic gates: {error}", file=sys.stderr)
        return EXIT_MALFORMED_RECORD
    except ReasoningAccuracyFixtureError as error:
        print(f"laconic gates: {error}", file=sys.stderr)
        return EXIT_REASONING_ACCURACY_FIXTURE
    except OSError as error:
        print(f"laconic gates: cannot read the corpus: {error}", file=sys.stderr)
        return EXIT_NO_CORPUS

    if args.format == "json":
        print(json.dumps(suite.to_json(), indent=2, sort_keys=True))
    else:
        _report_gates(suite)
    return suite.exit_code


def _expand(args: argparse.Namespace) -> int:
    try:
        fixture = load_fixture_ledger([args.corpus])
    except (MalformedRecordError, UnmatchedToolResultError) as error:
        print(f"laconic expand: malformed transcript: {error}", file=sys.stderr)
        return EXIT_MALFORMED_RECORD
    except UnsupportedToolResultError as error:
        print(f"laconic expand: unsupported tool result: {error}", file=sys.stderr)
        return EXIT_RENDER_TRACE
    except EmptyCorpusError as error:
        print(f"laconic expand: {error}", file=sys.stderr)
        return EXIT_NO_CORPUS
    except OSError as error:
        print(f"laconic expand: cannot read the corpus: {error}", file=sys.stderr)
        return EXIT_NO_CORPUS
    try:
        print(f"laconic expand: source transcript: {fixture.transcript}", file=sys.stderr)
        sys.stdout.write(fixture.ledger.expand(args.reference))
    except (InvalidSpanError, UnknownHandleError) as error:
        print(f"laconic expand: {error}", file=sys.stderr)
        return EXIT_RENDER_TRACE
    finally:
        fixture.ledger.close()
    return EXIT_OK


def _view(args: argparse.Namespace) -> int:
    try:
        fixture = load_fixture_ledger([args.corpus])
    except (MalformedRecordError, UnmatchedToolResultError) as error:
        print(f"laconic view: malformed transcript: {error}", file=sys.stderr)
        return EXIT_MALFORMED_RECORD
    except UnsupportedToolResultError as error:
        print(f"laconic view: unsupported tool result: {error}", file=sys.stderr)
        return EXIT_RENDER_TRACE
    except EmptyCorpusError as error:
        print(f"laconic view: {error}", file=sys.stderr)
        return EXIT_NO_CORPUS
    except OSError as error:
        print(f"laconic view: cannot read the corpus: {error}", file=sys.stderr)
        return EXIT_NO_CORPUS
    try:
        first_turn, last_turn = args.turns
        if args.deterministic_only:
            print("laconic view: deterministic-only mode", file=sys.stderr)
        provider = None
        if not args.deterministic_only:
            try:
                provider = provider_for(
                    NarrationConfig(
                        provider=args.provider,
                        endpoint=args.provider_endpoint,
                        model=args.provider_model,
                        timeout_seconds=args.provider_timeout,
                    )
                )
            except NarrationConfigurationError as error:
                print(f"laconic view: invalid narration provider: {error}", file=sys.stderr)
                return EXIT_NARRATION_CONFIG
        print(f"laconic view: source transcript: {fixture.transcript}", file=sys.stderr)
        entries = assemble(fixture.ledger, first_turn, last_turn)
        output = render(entries)
        if not output:
            print(
                f"laconic view: no observations in requested turn range {first_turn}-{last_turn}",
                file=sys.stderr,
            )
            return EXIT_RENDER_TRACE
        print(output)
        if provider is not None:
            try:
                narration = provider.narrate(entries)
            except NarrationUnavailableError as error:
                print(
                    f"laconic view: {error}; showing deterministic output",
                    file=sys.stderr,
                )
            except NarrationResponseError as error:
                print(f"laconic view: invalid narration response: {error}", file=sys.stderr)
                return EXIT_NARRATION_RESPONSE
            else:
                if narration is not None:
                    print()
                    print(render_narration(narration))
    finally:
        fixture.ledger.close()
    return EXIT_OK


def _study_dry_run(args: argparse.Namespace) -> int:
    if args.participants < MINIMUM_PARTICIPANTS:
        print(
            f"laconic study: --participants must be at least {MINIMUM_PARTICIPANTS} "
            f"(the pre-registered minimum the equivalence analysis requires); "
            f"got {args.participants}",
            file=sys.stderr,
        )
        return EXIT_STUDY_INSUFFICIENT_PARTICIPANTS
    result = run_study_dry_run(seed=args.seed, participant_count=args.participants)
    payload = study_dry_run_to_json(result)
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except OSError as error:
        print(f"laconic study: cannot write {args.out}: {error}", file=sys.stderr)
        return EXIT_STUDY_OUTPUT_ERROR
    _report_study_dry_run(result)
    print(f"laconic study: wrote analysis-ready dataset to {args.out}", file=sys.stderr)
    return EXIT_OK


def _report_study_dry_run(result: DryRunResult) -> None:
    detection = result.analysis.detection
    print(
        f"laconic study dry-run: seed={result.seed} participants={result.participant_count} "
        f"responses={len(result.responses)} pairs={result.analysis.n_pairs}"
    )
    print(
        f"  detection rate: rendered={detection.rendered_rate * 100:.2f}% "
        f"raw={detection.raw_rate * 100:.2f}% diff={detection.diff_pp:+.2f}pp "
        f"(90% CI [{detection.ci_low_pp:+.2f}, {detection.ci_high_pp:+.2f}]pp, "
        f"margin=\u00b1{detection.margin_pp:.1f}pp)"
    )
    verdict = "equivalent" if detection.equivalent else "not equivalent"
    print(f"  human-study verdict (dry run, simulated data): {verdict}")


def _claude_code_settings_path(scope: str) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    return Path.cwd() / ".claude" / "settings.json"


def _omp_extensions_dir(scope: str, *, user_dir: Path | None) -> Path:
    """Resolve OMP's extensions directory for ``scope``.

    OMP's own user-scope resolution is profile- and
    ``PI_CODING_AGENT_DIR``-aware in ways this installer cannot fully
    replicate without invoking ``omp`` itself: it checks
    ``PI_CODING_AGENT_DIR`` first, falls back to ``~/.omp/agent``, and
    never guesses a ``--profile`` name. Pass ``--user-dir`` explicitly
    on a non-default profile rather than relying on this fallback.
    """
    if scope == "project":
        return Path.cwd() / ".omp" / "extensions"
    if user_dir is not None:
        return user_dir
    override = os.environ.get("PI_CODING_AGENT_DIR")
    if override:
        return Path(override) / "extensions"
    return Path.home() / ".omp" / "agent" / "extensions"


def _observe_target_path(args: argparse.Namespace) -> Path:
    client = ClientId(args.client)
    if client is ClientId.CLAUDE_CODE:
        if args.scope == "user" and args.user_dir is not None:
            return Path(args.user_dir)
        return _claude_code_settings_path(args.scope)
    return _omp_extensions_dir(args.scope, user_dir=args.user_dir)


def _print_plan(plan: InstallPlan, fmt: str, *, applied: bool) -> None:
    if fmt == "json":
        print(
            json.dumps(
                {
                    "client": plan.client.value,
                    "mechanism": plan.mechanism.value,
                    "applied": applied,
                    "actions": [
                        {"kind": action.kind, "description": action.description}
                        for action in plan.actions
                    ],
                    "preserved": list(plan.preserved),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    state = "applied" if applied else "preview"
    print(f"laconic observe ({state}): {plan.client.value} via {plan.mechanism.value}")
    for action in plan.actions:
        print(f"  [{action.kind}] {action.description}")
    for item in plan.preserved:
        print(f"  [preserved] {item}")


def _observe_install(args: argparse.Namespace) -> int:
    client = ClientId(args.client)
    target = _observe_target_path(args)
    if args.dry_run:
        plan = (
            preview_claude_code(target) if client is ClientId.CLAUDE_CODE else preview_omp(target)
        )
        _print_plan(plan, args.format, applied=False)
        return EXIT_OK
    try:
        if client is ClientId.CLAUDE_CODE:
            result = apply_claude_code_install(target, python=args.python)
        else:
            result = apply_omp_install(target, python=args.python)
    except ConfigParseError as error:
        print(f"laconic observe install: {error}", file=sys.stderr)
        return EXIT_OBSERVE_CONFIG_PARSE_ERROR
    except OwnershipConflictError as error:
        print(f"laconic observe install: {error}", file=sys.stderr)
        return EXIT_OBSERVE_OWNERSHIP_CONFLICT
    _print_plan(result.plan, args.format, applied=result.applied)
    return EXIT_OK


def _observe_remove(args: argparse.Namespace) -> int:
    client = ClientId(args.client)
    target = _observe_target_path(args)
    if args.dry_run:
        plan = (
            preview_claude_code(target, remove=True)
            if client is ClientId.CLAUDE_CODE
            else preview_omp(target, remove=True)
        )
        _print_plan(plan, args.format, applied=False)
        return EXIT_OK
    try:
        if client is ClientId.CLAUDE_CODE:
            result = apply_claude_code_remove(target)
        else:
            result = apply_omp_remove(target)
    except ConfigParseError as error:
        print(f"laconic observe remove: {error}", file=sys.stderr)
        return EXIT_OBSERVE_CONFIG_PARSE_ERROR
    _print_plan(result.plan, args.format, applied=result.applied)
    return EXIT_OK


def _observe_status(args: argparse.Namespace) -> int:
    path = args.audit_path if args.audit_path is not None else DEFAULT_AUDIT_PATH
    status = compute_status(path)
    if args.format == "json":
        print(json.dumps(status.to_json(), indent=2, sort_keys=True))
        return EXIT_OK
    print(f"laconic observe status: {status.path}")
    print(f"  exists: {status.exists}")
    print(f"  entries: {status.entry_count}")
    print(f"  chain valid: {status.chain_valid}")
    if status.integrity_error is not None:
        print(f"  integrity error: {status.integrity_error}")
    return EXIT_OK


def _observe_report(args: argparse.Namespace) -> int:
    path = args.audit_path if args.audit_path is not None else DEFAULT_AUDIT_PATH
    report = compute_report(path)
    if args.format == "json":
        print(json.dumps(report.to_json(), indent=2, sort_keys=True))
        return EXIT_OK
    print(f"laconic observe report: {report.path} ({report.entry_count} entries)")
    for label, counts in (
        ("adapter", report.by_adapter),
        ("tool category", report.by_tool_category),
        ("result class", report.by_result_class),
        ("argument size", report.by_argument_size),
        ("result size", report.by_result_size),
    ):
        print(f"  by {label}:")
        for key, count in sorted(counts.items()):
            print(f"    {key}: {count}")
    return EXIT_OK


def _k1_stage_a_scan(args: argparse.Namespace) -> int:
    out_path = args.out if args.out is not None else DEFAULT_LEDGER_PATH
    report = scan_all_providers()
    write_ledger(report, out_path)
    _report_k1_stage_a(report, out_path)
    if report.disposition is Disposition.PROCEED_TO_STAGE_B_REQUEST:
        return EXIT_OK
    return EXIT_K1_STAGE_A_STOP


def _report_k1_stage_a(report: StageAReport, out_path: Path) -> None:
    print(
        f"K1 Stage A scan -- {len(report.records)} admitted session(s) across "
        f"{len(AUTHORIZED_ROOTS)} authorized root(s) and {len(AUTHORIZED_PROVIDERS)} "
        "authorized provider(s)."
    )
    print(f"Ledger written to {out_path}")
    print()
    print("Exclusions:")
    any_excluded = False
    for provider, reasons in sorted(report.exclusions.items()):
        total = sum(reasons.values())
        if total == 0:
            continue
        any_excluded = True
        breakdown = ", ".join(f"{reason}={count}" for reason, count in sorted(reasons.items()))
        print(f"  {provider:12} {total:>5}  ({breakdown})")
    if not any_excluded:
        print("  none")
    print()
    print("Stop conditions:")
    for condition in report.conditions:
        marker = "FIRED" if condition.fired else "ok"
        print(f"  [{marker:5}] {condition.name}: {condition.detail}")
    print()
    print(f"Disposition: {report.disposition.value}")


def _k1_stage_b_build_manifest(args: argparse.Namespace) -> int:
    corpus_manifest_path = (
        args.corpus_manifest if args.corpus_manifest is not None else DEFAULT_CORPUS_MANIFEST_PATH
    )
    out_path = args.out if args.out is not None else DEFAULT_SESSION_MANIFEST_PATH
    try:
        frozen = FrozenCorpus.load(corpus_manifest_path)
    except OSError as error:
        print(
            f"laconic k1 stage-b build-manifest: cannot read {corpus_manifest_path}: {error}",
            file=sys.stderr,
        )
        return EXIT_K1_STAGE_B_TOTALS_MISMATCH
    try:
        entries = build_session_manifest(frozen)
    except TotalsMismatchError as error:
        print(f"laconic k1 stage-b build-manifest: {error}", file=sys.stderr)
        return EXIT_K1_STAGE_B_TOTALS_MISMATCH
    write_session_manifest(entries, out_path)
    design_count = sum(1 for entry in entries if entry.set.value == "design")
    confirmatory_count = len(entries) - design_count
    print(
        f"K1 Stage B session manifest -- {len(entries)} session(s): "
        f"{design_count} design, {confirmatory_count} confirmatory."
    )
    print(f"Manifest written to {out_path}")
    return EXIT_OK


def _k1_stage_c_run(args: argparse.Namespace) -> int:
    try:
        selected_set = ManifestSet(args.selected_set)
        if args.spend_cap <= 0:
            raise ValueError(f"--spend-cap must be positive, got {args.spend_cap}")
        manifest = load_stage_c_manifest(args.manifest, selected_set=selected_set)
    except (OSError, StageCManifestError, ValueError) as error:
        print(f"laconic k1 stage-c run: {error}", file=sys.stderr)
        return EXIT_LIVE_CONFIG_ERROR
    try:
        client_factory = _load_client_factory(args.client)
        preflight_client = client_factory()
        close = getattr(preflight_client, "close", None)
        if callable(close):
            close()
    except (ImportError, AttributeError, TypeError, ValueError) as error:
        print(
            f"laconic k1 stage-c run: cannot load --client {args.client!r}: {error}",
            file=sys.stderr,
        )
        return EXIT_CLIENT_IMPORT_ERROR

    state_dir = args.state_dir
    ledger = StageCLedger(state_dir / "ledger.json")
    audit = StageCAudit(state_dir / "audit.jsonl")
    audit_retailogists_exclusions(manifest, audit)
    runner = LiveStageCSessionRunner(
        client_factory=client_factory,
        artifact_dir=state_dir / "artifacts",
        observation_builder=lambda baseline: _build_observations(baseline, codec="on"),
    )
    run_resumable_batch(
        manifest.entries,
        spend_cap_usd=args.spend_cap,
        runner=runner,
        ledger=ledger,
        audit=audit,
    )
    report = generate_stage_c_report(manifest, selected_set=selected_set, ledger=ledger)
    payload = report.to_json()
    if args.format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        composition = report.corpus_composition
        k1 = report.k1
        print(
            "K1 Stage C "
            f"{composition['set']} -- {composition['completed_sessions']}/"
            f"{composition['selected_sessions']} complete; "
            f"K1={k1['value_pct']}; disposition={k1['disposition']}"
        )
    completed = ledger.completed
    if any(
        entry.session_id not in completed or completed[entry.session_id].metrics is None
        for entry in manifest.entries
    ):
        return EXIT_K1_STAGE_C_INCOMPLETE
    return EXIT_OK


def _turn_range(value: str) -> tuple[int, int]:
    first, separator, last = value.partition("-")
    if not separator:
        raise argparse.ArgumentTypeError("turns must use FIRST-LAST")
    try:
        first_turn = int(first)
        last_turn = int(last)
    except ValueError as error:
        raise argparse.ArgumentTypeError("turn bounds must be integers") from error
    if first_turn < 1 or last_turn < first_turn:
        raise argparse.ArgumentTypeError("turn range must be positive and increasing")
    return first_turn, last_turn


def _report_gates(suite: GateSuiteResult) -> None:
    print(f"{'gate':6}{'verdict':16}{'value':>12}  description")
    for result in suite.results:
        value = "manual" if result.value is None else f"{result.value:.2f}{result.unit}"
        print(f"{result.gate:6}{result.verdict.value:16}{value:>12}  {result.description}")
        print(f"       {result.detail}")
    if suite.exit_code != 0:
        print("\nKILL CONDITION: at least one gate breached its kill threshold.", file=sys.stderr)


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
    milestone — reporting it here would let this milestone claim a savings number
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

    print("\nFile observation encoder — gross reduction (induced reads: see milestone):")
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
