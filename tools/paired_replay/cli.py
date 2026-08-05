"""Repository-only private evidence and paired replay command."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from tools.paired_replay.config import (
    PairedReplayConfigError,
    create_execution_config,
    read_paired_config,
    verify_execution_config,
    verify_provider_contract,
)
from tools.paired_replay.eligibility import (
    EligibilityLedgerError,
    assess_manifest,
    verify_eligibility,
    write_eligibility_ledger,
)
from tools.paired_replay.environment_ledger import (
    EnvironmentLedgerError,
    assess_environments,
    environment_counts,
    verify_environment,
    write_environment_ledger,
)
from tools.paired_replay.epoch import EpochError, create_epoch, verify_epoch, verify_epoch_manifest
from tools.paired_replay.interaction import InteractionReceiptError, verify_interaction_receipt
from tools.paired_replay.manifest import ManifestError, verify_manifest
from tools.paired_replay.openai_responses import OpenAIResponsesClient
from tools.paired_replay.report import (
    PairedReportError,
    build_paired_report,
    verify_paired_report,
    write_paired_report,
)
from tools.paired_replay.runner import (
    PairedReplayError,
    require_process_credential,
    run_paired_replay,
)
from tools.paired_replay.searchat_export import produce_manifest
from tools.paired_replay.split import SplitPolicy

EXIT_OK = 0
EXIT_PRIVATE_ARTIFACT = 20


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.paired_replay",
        description="Manage private evidence and paired replay artifacts from a source checkout.",
    )
    private_subcommands = parser.add_subparsers(dest="command")
    private_manifest = private_subcommands.add_parser(
        "manifest",
        help="read and verify a frozen metadata-only candidate manifest",
    )
    private_manifest_subcommands = private_manifest.add_subparsers(dest="private_manifest_command")
    private_manifest_verify = private_manifest_subcommands.add_parser(
        "verify",
        help="verify the manifest digest, frozen split, and native source hashes",
    )
    private_manifest_verify.add_argument(
        "--manifest",
        type=Path,
        required=True,
        metavar="FILE",
        help="private metadata-only paired replay manifest",
    )
    private_manifest_verify.set_defaults(handler=_private_manifest_verify)
    private_manifest_from_searchat = private_manifest_subcommands.add_parser(
        "from-searchat",
        help="freeze a manifest from a private metadata-only Searchat export",
    )
    private_manifest_from_searchat.add_argument(
        "--input",
        type=Path,
        required=True,
        metavar="FILE",
        help="metadata-only Searchat export",
    )
    private_manifest_from_searchat.add_argument(
        "--output",
        type=Path,
        required=True,
        metavar="FILE",
        help="destination for the private frozen manifest",
    )
    private_manifest_from_searchat.add_argument(
        "--holdout-fraction",
        type=float,
        default=0.2,
        help="fraction assigned to confirmatory holdout (default: 0.2)",
    )
    private_manifest_from_searchat.add_argument(
        "--seed",
        default="laconic-k1-v1",
        help="deterministic split seed (default: laconic-k1-v1)",
    )
    private_manifest_from_searchat.set_defaults(handler=_private_manifest_from_searchat)
    private_eligibility = private_subcommands.add_parser(
        "eligibility",
        help="build and verify a private native-evidence eligibility ledger",
    )
    private_eligibility_subcommands = private_eligibility.add_subparsers(
        dest="private_eligibility_command"
    )
    private_eligibility_build = private_eligibility_subcommands.add_parser(
        "build",
        help="probe every manifest candidate and write private dispositions",
    )
    private_eligibility_build.add_argument(
        "--epoch",
        type=Path,
        required=True,
        metavar="FILE",
        help="private sealed paired replay epoch",
    )
    private_eligibility_build.add_argument(
        "--manifest",
        type=Path,
        required=True,
        metavar="FILE",
        help="private frozen paired replay manifest",
    )
    private_eligibility_build.add_argument(
        "--ledger", type=Path, required=True, metavar="FILE", help="private eligibility ledger"
    )
    private_eligibility_build.set_defaults(handler=_private_eligibility_build)
    private_eligibility_verify = private_eligibility_subcommands.add_parser(
        "verify",
        help="verify a complete eligibility ledger and recheck confirmatory records",
    )
    private_eligibility_verify.add_argument(
        "--epoch",
        type=Path,
        required=True,
        metavar="FILE",
        help="private sealed paired replay epoch",
    )
    private_eligibility_verify.add_argument(
        "--manifest",
        type=Path,
        required=True,
        metavar="FILE",
        help="private frozen paired replay manifest",
    )
    private_eligibility_verify.add_argument(
        "--ledger", type=Path, required=True, metavar="FILE", help="private eligibility ledger"
    )
    private_eligibility_verify.set_defaults(handler=_private_eligibility_verify)
    private_environment = private_subcommands.add_parser(
        "environment",
        help="verify private non-content tool-environment admission receipts",
    )
    private_environment_subcommands = private_environment.add_subparsers(
        dest="private_environment_command"
    )
    private_environment_build = private_environment_subcommands.add_parser(
        "build",
        help="validate recorded-tool environments and write private admission receipts",
    )
    private_environment_build.add_argument(
        "--epoch",
        type=Path,
        required=True,
        metavar="FILE",
        help="private sealed paired replay epoch",
    )
    private_environment_build.add_argument(
        "--manifest",
        type=Path,
        required=True,
        metavar="FILE",
        help="private frozen paired replay manifest",
    )
    private_environment_build.add_argument(
        "--eligibility-ledger",
        type=Path,
        required=True,
        metavar="FILE",
        help="private verified eligibility ledger",
    )
    private_environment_build.add_argument(
        "--ledger", type=Path, required=True, metavar="FILE", help="private environment ledger"
    )
    private_environment_build.set_defaults(handler=_private_environment_build)
    private_environment_verify = private_environment_subcommands.add_parser(
        "verify",
        help="verify every confirmatory candidate has a valid tool environment",
    )
    private_environment_verify.add_argument(
        "--epoch",
        type=Path,
        required=True,
        metavar="FILE",
        help="private sealed paired replay epoch",
    )
    private_environment_verify.add_argument(
        "--manifest",
        type=Path,
        required=True,
        metavar="FILE",
        help="private frozen paired replay manifest",
    )
    private_environment_verify.add_argument(
        "--eligibility-ledger",
        type=Path,
        required=True,
        metavar="FILE",
        help="private verified eligibility ledger",
    )
    private_environment_verify.add_argument(
        "--ledger", type=Path, required=True, metavar="FILE", help="private environment ledger"
    )
    private_environment_verify.set_defaults(handler=_private_environment_verify)
    private_epoch = private_subcommands.add_parser(
        "epoch",
        help="create and verify a private paired replay sealed evidence epoch",
    )
    private_epoch_subcommands = private_epoch.add_subparsers(dest="private_epoch_command")
    private_epoch_create = private_epoch_subcommands.add_parser(
        "create",
        help="seal a frozen metadata-only manifest without opening candidate sources",
    )
    private_epoch_create.add_argument(
        "--manifest",
        type=Path,
        required=True,
        metavar="FILE",
        help="private frozen paired replay manifest",
    )
    private_epoch_create.add_argument(
        "--epoch",
        type=Path,
        required=True,
        metavar="FILE",
        help="private sealed paired replay epoch",
    )
    private_epoch_create.add_argument(
        "--audit", type=Path, required=True, metavar="FILE", help="private access audit"
    )
    private_epoch_create.add_argument(
        "--approved-root",
        action="append",
        type=Path,
        required=True,
        metavar="DIRECTORY",
        help="approved private root; repeat for multiple roots",
    )
    private_epoch_create.add_argument(
        "--epoch-id", required=True, help="new private epoch identifier"
    )
    private_epoch_create.add_argument(
        "--created-at", required=True, help="ISO-8601 epoch creation timestamp"
    )
    private_epoch_create.set_defaults(handler=_private_epoch_create)
    private_epoch_verify = private_epoch_subcommands.add_parser(
        "verify",
        help="verify an epoch receipt and hash-chained audit without opening candidate sources",
    )
    private_epoch_verify.add_argument(
        "--epoch",
        type=Path,
        required=True,
        metavar="FILE",
        help="private sealed paired replay epoch",
    )
    private_epoch_verify.add_argument(
        "--manifest",
        type=Path,
        required=True,
        metavar="FILE",
        help="private frozen paired replay manifest",
    )
    private_epoch_verify.set_defaults(handler=_private_epoch_verify)

    private_interaction = private_subcommands.add_parser(
        "interaction",
        help="verify a private chronological interaction receipt",
    )
    private_interaction_subcommands = private_interaction.add_subparsers(
        dest="private_interaction_command"
    )
    private_interaction_verify = private_interaction_subcommands.add_parser(
        "verify",
        help="verify receipt chronology, environment bindings, and audit provenance",
    )
    private_interaction_verify.add_argument(
        "--receipt", type=Path, required=True, metavar="FILE", help="private interaction receipt"
    )
    private_interaction_verify.add_argument(
        "--epoch",
        type=Path,
        required=True,
        metavar="FILE",
        help="private sealed paired replay epoch",
    )
    private_interaction_verify.add_argument(
        "--manifest",
        type=Path,
        required=True,
        metavar="FILE",
        help="private frozen paired replay manifest",
    )
    private_interaction_verify.add_argument(
        "--eligibility-ledger",
        type=Path,
        required=True,
        metavar="FILE",
        help="private M2 eligibility ledger",
    )
    private_interaction_verify.add_argument(
        "--environment-ledger",
        type=Path,
        required=True,
        metavar="FILE",
        help="private M3 environment ledger",
    )
    private_interaction_verify.add_argument(
        "--split",
        choices=["redesign"],
        required=True,
        help="receipt split; only redesign is admitted before release approval",
    )
    private_interaction_verify.set_defaults(handler=_private_interaction_verify)

    private_replay = private_subcommands.add_parser(
        "replay",
        help="validate private configuration for a contemporary paired replay",
    )
    private_replay_subcommands = private_replay.add_subparsers(dest="private_replay_command")
    private_replay_create_config = private_replay_subcommands.add_parser(
        "create-config",
        help="write an approved private paired replay configuration from receipts",
    )
    private_replay_create_config.add_argument(
        "--epoch",
        type=Path,
        required=True,
        metavar="FILE",
        help="private sealed paired replay epoch",
    )
    private_replay_create_config.add_argument(
        "--manifest",
        type=Path,
        required=True,
        metavar="FILE",
        help="private frozen paired replay manifest",
    )
    private_replay_create_config.add_argument(
        "--eligibility-ledger",
        type=Path,
        required=True,
        metavar="FILE",
        help="private verified eligibility ledger",
    )
    private_replay_create_config.add_argument(
        "--environment-ledger",
        type=Path,
        required=True,
        metavar="FILE",
        help="private verified environment ledger",
    )
    private_replay_create_config.add_argument(
        "--interaction-receipt",
        type=Path,
        action="append",
        required=True,
        metavar="FILE",
        help="private verified redesign interaction receipt; repeat per candidate",
    )
    private_replay_create_config.add_argument(
        "--artifact-root",
        type=Path,
        required=True,
        metavar="DIR",
        help="private root for response and report artifacts",
    )
    private_replay_create_config.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="FILE",
        help="private output configuration",
    )
    private_replay_create_config.set_defaults(handler=_private_replay_create_config)
    private_replay_verify_config = private_replay_subcommands.add_parser(
        "verify-config",
        help="verify a private, integrity-checked paired replay configuration",
    )
    private_replay_verify_config.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="FILE",
        help="private paired replay configuration",
    )
    private_replay_verify_config.set_defaults(handler=_private_replay_verify_config)
    private_replay_run = private_replay_subcommands.add_parser(
        "run",
        help="execute the approved redesign-only paired replay through OpenAI Responses",
    )
    private_replay_run.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="FILE",
        help="private paired replay configuration",
    )
    private_replay_run.add_argument(
        "--run-id", required=True, metavar="ID", help="private identifier for this execution"
    )
    private_replay_run.add_argument(
        "--report",
        type=Path,
        metavar="FILE",
        help="private paired-replay paired report output; defaults under the private artifact root",
    )
    private_replay_run.set_defaults(handler=_private_replay_run)
    private_replay_verify_report = private_replay_subcommands.add_parser(
        "verify-report",
        help="verify a private paired-replay paired receipt report and response-artifact digests",
    )
    private_replay_verify_report.add_argument(
        "--report", type=Path, required=True, metavar="FILE", help="private paired receipt report"
    )
    private_replay_verify_report.add_argument(
        "--config",
        type=Path,
        required=True,
        metavar="FILE",
        help="private paired replay configuration",
    )
    private_replay_verify_report.add_argument(
        "--epoch",
        type=Path,
        required=True,
        metavar="FILE",
        help="private sealed paired replay epoch",
    )
    private_replay_verify_report.add_argument(
        "--manifest",
        type=Path,
        required=True,
        metavar="FILE",
        help="private frozen paired replay manifest",
    )
    private_replay_verify_report.set_defaults(handler=_private_replay_verify_report)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        build_parser().print_help()
        return EXIT_OK
    return handler(args)


def _private_epoch_create(args: argparse.Namespace) -> int:
    try:
        epoch = create_epoch(
            args.manifest,
            args.epoch,
            audit_path=args.audit,
            approved_roots=tuple(args.approved_root),
            epoch_id=args.epoch_id,
            created_at=args.created_at,
        )
    except (EpochError, ManifestError) as error:
        print(f"paired-replay epoch create: {error}", file=sys.stderr)
        return EXIT_PRIVATE_ARTIFACT
    print(
        f"sealed paired replay epoch {args.epoch}: digest {epoch.digest}, audit {epoch.audit_path}"
    )
    return EXIT_OK


def _private_epoch_verify(args: argparse.Namespace) -> int:
    try:
        epoch = verify_epoch(args.epoch, args.manifest)
    except (EpochError, ManifestError) as error:
        print(f"paired-replay epoch verify: {error}", file=sys.stderr)
        return EXIT_PRIVATE_ARTIFACT
    print(
        f"verified paired replay epoch {args.epoch}: digest {epoch.digest}, "
        f"audit {epoch.audit_path}"
    )
    return EXIT_OK


def _private_manifest_verify(args: argparse.Namespace) -> int:
    try:
        manifest = verify_manifest(args.manifest)
    except ManifestError as error:
        print(f"paired-replay manifest verify: {error}", file=sys.stderr)
        return EXIT_PRIVATE_ARTIFACT
    print(
        f"verified paired replay manifest {args.manifest}: "
        f"{len(manifest.candidates)} candidate(s), digest {manifest.digest}"
    )
    return EXIT_OK


def _private_eligibility_build(args: argparse.Namespace) -> int:
    try:
        ledger = assess_manifest(args.epoch, args.manifest)
        write_eligibility_ledger(args.ledger, ledger)
    except EligibilityLedgerError as error:
        print(f"paired-replay eligibility build: {error}", file=sys.stderr)
        return EXIT_PRIVATE_ARTIFACT
    counts = {
        disposition: sum(record.disposition == disposition for record in ledger.records)
        for disposition in ("confirmatory", "diagnostic_only", "excluded")
    }
    print(
        f"wrote paired replay eligibility ledger {args.ledger}: "
        f"{len(ledger.records)} candidate(s), confirmatory={counts['confirmatory']}, "
        f"diagnostic-only={counts['diagnostic_only']}, excluded={counts['excluded']}"
    )
    return EXIT_OK


def _private_eligibility_verify(args: argparse.Namespace) -> int:
    try:
        ledger = verify_eligibility(args.epoch, args.manifest, args.ledger)
    except EligibilityLedgerError as error:
        print(f"paired-replay eligibility verify: {error}", file=sys.stderr)
        return EXIT_PRIVATE_ARTIFACT
    print(
        f"verified paired replay eligibility ledger {args.ledger}: "
        f"{len(ledger.records)} candidate(s)"
    )
    return EXIT_OK


def _private_environment_build(args: argparse.Namespace) -> int:
    try:
        ledger = assess_environments(args.epoch, args.manifest, args.eligibility_ledger)
        write_environment_ledger(args.ledger, ledger)
    except EnvironmentLedgerError as error:
        print(f"paired-replay environment build: {error}", file=sys.stderr)
        return EXIT_PRIVATE_ARTIFACT
    counts = environment_counts(ledger)
    print(
        f"wrote paired replay environment ledger {args.ledger}: "
        f"{len(ledger.records)} candidate(s), valid={counts['valid']}, "
        f"unsupported={counts['unsupported']}, unavailable={counts['unavailable']}"
    )
    return EXIT_OK


def _private_environment_verify(args: argparse.Namespace) -> int:
    try:
        ledger = verify_environment(args.epoch, args.manifest, args.eligibility_ledger, args.ledger)
    except EnvironmentLedgerError as error:
        print(f"paired-replay environment verify: {error}", file=sys.stderr)
        return EXIT_PRIVATE_ARTIFACT
    counts = environment_counts(ledger)
    print(
        f"verified paired replay environment ledger {args.ledger}: "
        f"{len(ledger.records)} candidate(s), valid={counts['valid']}, "
        f"unsupported={counts['unsupported']}, unavailable={counts['unavailable']}"
    )
    return EXIT_OK


def _private_interaction_verify(args: argparse.Namespace) -> int:
    try:
        receipt = verify_interaction_receipt(
            args.receipt,
            args.epoch,
            args.manifest,
            args.eligibility_ledger,
            args.environment_ledger,
        )
    except InteractionReceiptError as error:
        print(f"paired-replay interaction verify: {error}", file=sys.stderr)
        return EXIT_PRIVATE_ARTIFACT
    print(
        f"verified paired replay interaction receipt {args.receipt}: "
        f"candidate={receipt.candidate_id}, digest {receipt.digest}"
    )
    return EXIT_OK


def _private_replay_create_config(args: argparse.Namespace) -> int:
    try:
        config = create_execution_config(
            epoch_path=args.epoch,
            manifest_path=args.manifest,
            eligibility_ledger_path=args.eligibility_ledger,
            environment_ledger_path=args.environment_ledger,
            artifact_root=args.artifact_root,
            interaction_receipt_paths=args.interaction_receipt,
            config_path=args.config,
        )
    except PairedReplayConfigError as error:
        print(f"paired-replay replay create-config: {error}", file=sys.stderr)
        return EXIT_PRIVATE_ARTIFACT
    print(
        f"wrote paired replay config {args.config}: "
        f"candidates={len(config.candidate_ids)}, digest {config.digest}"
    )
    return EXIT_OK


def _private_replay_verify_config(args: argparse.Namespace) -> int:
    try:
        config = read_paired_config(args.config)
        verify_execution_config(config)
    except PairedReplayConfigError as error:
        print(f"paired-replay replay verify-config: {error}", file=sys.stderr)
        return EXIT_PRIVATE_ARTIFACT
    print(
        f"verified paired replay config {args.config}: "
        f"provider={config.provider}, model={config.model}, digest {config.digest}"
    )
    return EXIT_OK


def _private_replay_run(args: argparse.Namespace) -> int:
    try:
        config = read_paired_config(args.config)
        verify_provider_contract(config)
        require_process_credential(config.credential_environment)
        receipt = run_paired_replay(config, OpenAIResponsesClient(), run_id=args.run_id)
        epoch, _ = verify_epoch_manifest(config.epoch_path, config.manifest_path)
        report_path = args.report or (config.artifact_root / args.run_id / "paired-report.json")
        report = build_paired_report(config.epoch_path, config.manifest_path, config, receipt)
        write_paired_report(report_path, epoch, report)
    except (
        InteractionReceiptError,
        PairedReplayConfigError,
        PairedReplayError,
        PairedReportError,
        EpochError,
    ) as error:
        print(f"paired-replay replay run: {error}", file=sys.stderr)
        return EXIT_PRIVATE_ARTIFACT
    print(
        f"wrote paired replay paired report {report_path}: receipt={receipt.digest}, "
        f"report={report.digest}, cost=${receipt.total_cost_usd}"
    )
    return EXIT_OK


def _private_replay_verify_report(args: argparse.Namespace) -> int:
    try:
        config = read_paired_config(args.config)
        report = verify_paired_report(args.report, args.epoch, args.manifest, config)
    except (PairedReplayConfigError, PairedReportError) as error:
        print(f"paired-replay replay verify-report: {error}", file=sys.stderr)
        return EXIT_PRIVATE_ARTIFACT
    print(
        f"verified paired replay paired report {args.report}: {len(report.strata)} stratum/strata, "
        f"digest {report.digest}"
    )
    return EXIT_OK


def _private_manifest_from_searchat(args: argparse.Namespace) -> int:
    try:
        manifest = produce_manifest(
            args.input,
            args.output,
            policy=SplitPolicy(
                holdout_fraction=args.holdout_fraction,
                seed=args.seed,
            ),
        )
    except ManifestError as error:
        print(f"paired-replay manifest from-searchat: {error}", file=sys.stderr)
        return EXIT_PRIVATE_ARTIFACT
    print(
        f"wrote paired replay manifest {args.output}: {len(manifest.candidates)} candidate(s), "
        f"digest {manifest.digest}"
    )
    return EXIT_OK
