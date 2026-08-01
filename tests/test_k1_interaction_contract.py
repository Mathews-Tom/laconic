"""Tests for K1's executable native interaction receipt contract."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from laconic.k1.environment import SnapshotEnvironment, SnapshotToolResolver, snapshot_tree_sha256
from laconic.k1.evidence import NativeEvent, NativeSession, ToolCall, ToolResult
from laconic.k1.interaction import (
    InteractionActionResolver,
    InteractionReceipt,
    InteractionReceiptError,
    ToolInputSchema,
    derive_interaction_receipt,
    read_interaction_receipt,
    schema_for_json,
    write_interaction_receipt,
)


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
