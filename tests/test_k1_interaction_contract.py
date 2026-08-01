"""Tests for K1's executable native interaction receipt contract."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast

import pytest

from laconic.cli import EXIT_K1_MANIFEST, EXIT_OK, main
from laconic.k1.eligibility import assess_manifest, write_eligibility_ledger
from laconic.k1.environment import SnapshotEnvironment, SnapshotToolResolver, snapshot_tree_sha256
from laconic.k1.environment_ledger import assess_environments, write_environment_ledger
from laconic.k1.epoch import create_epoch
from laconic.k1.evidence import NativeEvent, NativeSession, ToolCall, ToolResult
from laconic.k1.interaction import (
    InteractionActionResolver,
    InteractionReceipt,
    InteractionReceiptError,
    ToolInputSchema,
    build_interaction_receipt,
    derive_interaction_receipt,
    read_interaction_receipt,
    schema_for_json,
    verify_interaction_receipt,
    write_interaction_receipt,
)
from laconic.k1.manifest import Candidate, Manifest, source_sha256, write_manifest


def _session() -> NativeSession:
    return NativeSession(
        candidate_id="redesign-1",
        provider="omp",
        parser="omp-jsonl-v1",
        source_path=Path("/private/redesign-1.jsonl"),
        source_sha256="a" * 64,
        model="openai-codex/gpt-5.6-terra",
        events=(
            NativeEvent(0, "2026-08-01T00:00:00Z", "user_prompt", text="inspect source"),
            NativeEvent(
                1,
                "2026-08-01T00:01:00Z",
                "assistant",
                text="",
                tool_calls=(ToolCall("call-1", "Read", {"path": "src/main.py"}),),
            ),
            NativeEvent(
                2,
                "2026-08-01T00:02:00Z",
                "tool_result",
                tool_result=ToolResult("call-1", {"text": "print('safe')\n"}),
            ),
            NativeEvent(3, "2026-08-01T00:03:00Z", "user_prompt", text="summarize"),
        ),
    )


def _receipt() -> InteractionReceipt:
    return derive_interaction_receipt(
        _session(),
        epoch_digest="b" * 64,
        manifest_digest="c" * 64,
        eligibility_ledger_digest="d" * 64,
        environment_ledger_digest="e" * 64,
        audit_head_digest="f" * 64,
        environment_digest="1" * 64,
        environment_mode="recorded_tool",
    )


def _private_receipt_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
    private = tmp_path / "private"
    sources = private / "sources"
    sources.mkdir(parents=True, mode=0o700)
    redesign_source = sources / "redesign.jsonl"
    redesign_source.write_text(
        "\n".join(
            json.dumps(record, sort_keys=True)
            for record in (
                {
                    "timestamp": "2026-07-30T21:00:00Z",
                    "type": "user",
                    "message": {"role": "user", "content": "Inspect redesign."},
                },
                {
                    "timestamp": "2026-07-30T21:00:01Z",
                    "type": "assistant",
                    "message": {
                        "id": "message-redesign",
                        "role": "assistant",
                        "model": "claude-sonnet-4-6",
                        "usage": {"input_tokens": 100, "output_tokens": 20},
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": "Read",
                                "input": {"path": "src/app.py"},
                            }
                        ],
                    },
                },
                {
                    "timestamp": "2026-07-30T21:00:02Z",
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call-1",
                                "content": {"text": "print('ok')"},
                            }
                        ],
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    holdout_source = sources / "holdout.jsonl"
    candidate_specs: tuple[tuple[str, Path, Literal["redesign", "holdout"]], ...] = (
        ("redesign", redesign_source, "redesign"),
        ("holdout", holdout_source, "holdout"),
    )
    candidates = tuple(
        Candidate(
            candidate_id=candidate_id,
            source_path=source.resolve(),
            source_sha256=source_sha256(redesign_source)
            if candidate_id == "redesign"
            else "a" * 64,
            provider="claude-code",
            model="claude-sonnet-4-6",
            model_family="claude-4",
            project=f"project-{candidate_id}",
            timestamp="2026-07-30T21:00:00Z",
            session_length=1,
            message_count=1,
            has_code=True,
            tool_density=1.0,
            time_period="2026-Q3",
            session_size_band="small",
            selection_stratum="claude-code|claude-4|2026-Q3|small",
            lineage=f"lineage-{candidate_id}",
            eligibility_disposition="confirmatory",
            split=split,
        )
        for candidate_id, source, split in candidate_specs
    )
    manifest_path = private / "manifest.json"
    write_manifest(manifest_path, Manifest(candidates))
    epoch_path = private / "epoch.json"
    create_epoch(
        manifest_path,
        epoch_path,
        audit_path=private / "access-audit.json",
        approved_roots=(private,),
        epoch_id="k1-test-interaction",
        created_at="2026-07-31T11:00:00Z",
    )
    eligibility_path = private / "eligibility.json"
    write_eligibility_ledger(eligibility_path, assess_manifest(epoch_path, manifest_path))
    environment_path = private / "environment.json"
    write_environment_ledger(
        environment_path, assess_environments(epoch_path, manifest_path, eligibility_path)
    )
    return (
        epoch_path,
        manifest_path,
        eligibility_path,
        environment_path,
        private / "interaction.json",
        holdout_source,
    )


def test_private_receipt_integration_revalidates_redesign_only_chain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    epoch, manifest, eligibility, environment, receipt_path, holdout_source = (
        _private_receipt_inputs(tmp_path)
    )

    receipt = build_interaction_receipt(
        epoch, manifest, eligibility, environment, "redesign", receipt_path
    )

    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert not holdout_source.exists()
    assert (
        verify_interaction_receipt(receipt_path, epoch, manifest, eligibility, environment)
        == receipt
    )
    assert (
        main(
            [
                "k1",
                "interaction",
                "verify",
                "--receipt",
                str(receipt_path),
                "--epoch",
                str(epoch),
                "--manifest",
                str(manifest),
                "--eligibility-ledger",
                str(eligibility),
                "--environment-ledger",
                str(environment),
                "--split",
                "redesign",
            ]
        )
        == EXIT_OK
    )
    assert "verified K1 interaction receipt" in capsys.readouterr().out


def test_receipt_preserves_non_content_chronology_and_tool_linkage() -> None:
    receipt = _receipt()

    assert receipt.candidate_id == "redesign-1"
    assert [(event.native_index, event.kind) for event in receipt.events] == [
        (0, "user_prompt"),
        (1, "assistant"),
        (1, "tool_call"),
        (2, "tool_result"),
        (3, "user_prompt"),
    ]
    call = receipt.events[2]
    result = receipt.events[3]
    assert call.call_digest == result.call_digest
    assert call.tool_name == "Read"
    assert call.authority == "recorded_exact"
    assert call.input_schema is not None
    assert call.input_schema.document == {
        "additionalProperties": False,
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "type": "object",
    }
    serialized = json.dumps(receipt.to_document())
    assert "inspect source" not in serialized
    assert "src/main.py" not in serialized
    assert "print('safe')" not in serialized


def test_receipt_rejects_unlinked_tool_result() -> None:
    session = _session()
    broken = NativeSession(
        candidate_id=session.candidate_id,
        provider=session.provider,
        parser=session.parser,
        source_path=session.source_path,
        source_sha256=session.source_sha256,
        model=session.model,
        events=(
            session.events[0],
            NativeEvent(
                1,
                "2026-08-01T00:02:00Z",
                "tool_result",
                tool_result=ToolResult("call-1", {"text": "unexpected"}),
            ),
        ),
    )

    with pytest.raises(InteractionReceiptError, match="unlinked tool result"):
        derive_interaction_receipt(
            broken,
            epoch_digest="b" * 64,
            manifest_digest="c" * 64,
            eligibility_ledger_digest="d" * 64,
            environment_ledger_digest="e" * 64,
            audit_head_digest="f" * 64,
            environment_digest="1" * 64,
            environment_mode="recorded_tool",
        )


def test_schema_is_closed_and_exact_shape() -> None:
    schema = ToolInputSchema(schema_for_json({"query": ["a"], "limit": 3}))

    assert schema.document == {
        "additionalProperties": False,
        "properties": {
            "limit": {"type": "integer"},
            "query": {
                "items": [{"type": "string"}],
                "maxItems": 1,
                "minItems": 1,
                "type": "array",
            },
        },
        "required": ["limit", "query"],
        "type": "object",
    }
    with pytest.raises(InteractionReceiptError, match="object input schema has invalid fields"):
        ToolInputSchema({"type": "object"})


def test_snapshot_authority_rejects_non_read_tool() -> None:
    session = replace(
        _session(),
        events=(
            NativeEvent(0, "2026-08-01T00:00:00Z", "user_prompt", text="inspect"),
            NativeEvent(
                1,
                "2026-08-01T00:01:00Z",
                "assistant",
                text="",
                tool_calls=(ToolCall("call-1", "Bash", {"argv": ["pwd"]}),),
            ),
            NativeEvent(
                2,
                "2026-08-01T00:02:00Z",
                "tool_result",
                tool_result=ToolResult("call-1", {"text": "/private"}),
            ),
        ),
    )

    with pytest.raises(InteractionReceiptError, match="cannot authorize"):
        derive_interaction_receipt(
            session,
            epoch_digest="b" * 64,
            manifest_digest="c" * 64,
            eligibility_ledger_digest="d" * 64,
            environment_ledger_digest="e" * 64,
            audit_head_digest="f" * 64,
            environment_digest="1" * 64,
            environment_mode="snapshot",
        )


def test_receipt_links_repeated_identical_calls_by_native_identifier() -> None:
    session = replace(
        _session(),
        events=(
            _session().events[0],
            NativeEvent(
                1,
                "2026-08-01T00:01:00Z",
                "assistant",
                tool_calls=(
                    ToolCall("call-1", "Read", {"path": "src/main.py"}),
                    ToolCall("call-2", "Read", {"path": "src/main.py"}),
                ),
            ),
            NativeEvent(
                2,
                "2026-08-01T00:02:00Z",
                "tool_result",
                tool_result=ToolResult("call-1", {"text": "first"}),
            ),
            NativeEvent(
                3,
                "2026-08-01T00:03:00Z",
                "tool_result",
                tool_result=ToolResult("call-2", {"text": "second"}),
            ),
        ),
    )

    receipt = derive_interaction_receipt(
        session,
        epoch_digest="b" * 64,
        manifest_digest="c" * 64,
        eligibility_ledger_digest="d" * 64,
        environment_ledger_digest="e" * 64,
        audit_head_digest="f" * 64,
        environment_digest="1" * 64,
        environment_mode="recorded_tool",
    )

    calls = [event for event in receipt.events if event.kind == "tool_call"]
    results = [event for event in receipt.events if event.kind == "tool_result"]
    assert len(calls) == len(results) == 2
    assert calls[0].call_digest != calls[1].call_digest
    assert [event.call_digest for event in calls] == [event.call_digest for event in results]


def test_interaction_cli_rejects_missing_private_receipt_without_source_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.json"

    exit_code = main(
        [
            "k1",
            "interaction",
            "verify",
            "--receipt",
            str(missing),
            "--epoch",
            str(missing),
            "--manifest",
            str(missing),
            "--eligibility-ledger",
            str(missing),
            "--environment-ledger",
            str(missing),
            "--split",
            "redesign",
        ]
    )

    assert exit_code == EXIT_K1_MANIFEST
    assert "cannot stat interaction receipt" in capsys.readouterr().err


def test_recorded_action_resolver_rejects_any_off_trace_call() -> None:
    resolver = InteractionActionResolver(_receipt(), _session())

    resolution = resolver.resolve("Read", {"path": "other.py"})

    assert resolution.disposition == "unsupported"
    assert resolution.output is None
    assert resolver.position == 0
    assert resolver.terminated


def test_snapshot_action_resolver_marks_rooted_divergence_induced(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    source = root / "src"
    source.mkdir(parents=True, mode=0o700)
    (source / "a.py").write_text("a\n", encoding="utf-8")
    (source / "b.py").write_text("b\n", encoding="utf-8")
    for item in source.iterdir():
        item.chmod(0o400)
    source.chmod(0o500)
    root.chmod(0o500)
    session = _session()
    receipt = derive_interaction_receipt(
        session,
        epoch_digest="b" * 64,
        manifest_digest="c" * 64,
        eligibility_ledger_digest="d" * 64,
        environment_ledger_digest="e" * 64,
        audit_head_digest="f" * 64,
        environment_digest=snapshot_tree_sha256(root),
        environment_mode="snapshot",
    )
    resolver = InteractionActionResolver(
        receipt,
        session,
        snapshot=SnapshotToolResolver(SnapshotEnvironment(root, receipt.environment_digest)),
    )

    resolution = resolver.resolve("Read", {"path": "src/b.py"})

    assert resolution.disposition == "induced"
    assert resolution.output == "b\n"
    assert resolver.position == 1


def test_private_receipt_round_trip_detects_tampering(tmp_path: Path) -> None:
    private = tmp_path / "private"
    path = private / "interaction.json"
    receipt = _receipt()

    write_interaction_receipt(path, receipt)

    assert path.stat().st_mode & 0o777 == 0o600
    assert private.stat().st_mode & 0o777 == 0o700
    assert read_interaction_receipt(path) == receipt

    def alter_candidate(document: dict[str, object]) -> None:
        document["candidate_id"] = "altered"

    def corrupt_input_schema(document: dict[str, object]) -> None:
        events = cast(list[dict[str, object]], document["events"])
        events[0]["input_schema"] = []

    def corrupt_schema_version(document: dict[str, object]) -> None:
        document["schema_version"] = True

    original = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    for mutate in (alter_candidate, corrupt_input_schema, corrupt_schema_version):
        document = cast(dict[str, object], json.loads(json.dumps(original)))
        mutate(document)
        path.write_text(json.dumps(document), encoding="utf-8")
        path.chmod(0o600)

        with pytest.raises(InteractionReceiptError):
            read_interaction_receipt(path)
