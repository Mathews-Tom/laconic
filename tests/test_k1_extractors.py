"""Conformance tests for K1 native evidence extraction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from laconic.k1.evidence import (
    BillableUsage,
    NativeEvent,
    NativeEvidenceError,
    NativeSession,
    ToolCall,
    ToolResult,
    validate_confirmatory_evidence,
)
from laconic.k1.extractors import extract_claude_code, extract_native, extract_omp
from laconic.k1.manifest import Candidate, source_sha256


def _candidate(
    tmp_path: Path,
    *,
    provider: str = "claude-code",
    model: str = "claude-sonnet-4-6",
    model_family: str = "claude-4",
) -> Candidate:
    source = tmp_path / "session.jsonl"
    source.write_text('{"native":"evidence"}\n', encoding="utf-8")
    return Candidate(
        candidate_id="session-a",
        source_path=source.resolve(),
        source_sha256=source_sha256(source),
        provider=provider,
        model=model,
        model_family=model_family,
        project="acme/api",
        timestamp="2026-07-30T17:00:00Z",
        session_length=3,
        message_count=3,
        has_code=True,
        tool_density=0.5,
        time_period="2026-Q3",
        session_size_band="small",
        selection_stratum=f"{provider}|{model_family}|2026-Q3|small",
        lineage="issue-42",
        eligibility_disposition="unreviewed",
        split="redesign",
    )


def _session(candidate: Candidate) -> NativeSession:
    return NativeSession(
        candidate_id=candidate.candidate_id,
        provider=candidate.provider,
        parser="claude-code-jsonl-v1",
        source_path=candidate.source_path,
        source_sha256=candidate.source_sha256,
        model="claude-sonnet-4-6",
        events=(
            NativeEvent(0, "2026-07-30T17:00:00Z", "user_prompt", text="Inspect the code."),
            NativeEvent(
                1,
                "2026-07-30T17:00:01Z",
                "assistant",
                usage=BillableUsage(100, 20, 50, 10),
                tool_calls=(ToolCall("call-1", "read", {"path": "src/app.py"}),),
            ),
            NativeEvent(
                2,
                "2026-07-30T17:00:02Z",
                "tool_result",
                tool_result=ToolResult("call-1", {"content": "print('ok')"}),
            ),
        ),
    )


def test_confirmatory_evidence_accepts_complete_native_trace(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)

    validate_confirmatory_evidence(candidate, _session(candidate))


def test_confirmatory_evidence_rejects_missing_assistant_usage(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    session = _session(candidate)
    events = (
        session.events[0],
        NativeEvent(
            1,
            "2026-07-30T17:00:01Z",
            "assistant",
            tool_calls=(ToolCall("call-1", "read", {"path": "src/app.py"}),),
        ),
        session.events[2],
    )

    with pytest.raises(NativeEvidenceError, match="assistant usage is missing or incomplete"):
        validate_confirmatory_evidence(candidate, _replace_events(session, events))


def test_confirmatory_evidence_rejects_unmatched_tool_result(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    session = _session(candidate)
    events = (
        session.events[0],
        session.events[1],
        NativeEvent(
            2,
            "2026-07-30T17:00:02Z",
            "tool_result",
            tool_result=ToolResult("call-2", {"content": "print('ok')"}),
        ),
    )

    with pytest.raises(NativeEvidenceError, match="unmatched tool result 'call-2'"):
        validate_confirmatory_evidence(candidate, _replace_events(session, events))


def test_confirmatory_evidence_rejects_missing_model_identifier(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    session = _session(candidate)
    unknown_model = NativeSession(
        candidate_id=session.candidate_id,
        provider=session.provider,
        parser=session.parser,
        source_path=session.source_path,
        source_sha256=session.source_sha256,
        model=None,
        events=session.events,
    )

    with pytest.raises(NativeEvidenceError, match="model does not match manifest"):
        validate_confirmatory_evidence(candidate, unknown_model)


def test_confirmatory_evidence_rejects_tampered_native_source(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    session = _session(candidate)
    candidate.source_path.write_text('{"native":"tampered"}\n', encoding="utf-8")

    with pytest.raises(NativeEvidenceError, match="native source hash does not match manifest"):
        validate_confirmatory_evidence(candidate, session)


def test_claude_extractor_preserves_native_usage_and_tool_identity(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    _write_records(
        candidate.source_path,
        [
            {
                "timestamp": "2026-07-30T17:00:00Z",
                "type": "user",
                "message": {"role": "user", "content": "Inspect the code."},
            },
            {
                "timestamp": "2026-07-30T17:00:01Z",
                "message": {
                    "id": "message-1",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 50,
                        "cache_creation_input_tokens": 10,
                    },
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-1",
                            "name": "read",
                            "input": {"path": "src/app.py"},
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-07-30T17:00:01.500Z",
                "message": {
                    "id": "message-1",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 50,
                        "cache_creation_input_tokens": 10,
                    },
                    "content": [{"type": "text", "text": "I will inspect the code."}],
                },
            },
            {
                "timestamp": "2026-07-30T17:00:02Z",
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
        ],
    )
    candidate = _refresh_candidate_hash(candidate)

    session = extract_claude_code(candidate)

    validate_confirmatory_evidence(candidate, session)
    assert session.events[1].usage == BillableUsage(100, 20, 50, 10)
    assert session.events[1].timestamp == "2026-07-30T17:00:01Z"
    assert session.events[1].text == "I will inspect the code."
    assert session.events[2].tool_result == ToolResult("call-1", {"text": "print('ok')"})


def test_omp_extractor_preserves_native_usage_and_tool_identity(tmp_path: Path) -> None:
    candidate = _candidate(
        tmp_path,
        provider="omp",
        model="openai-codex/gpt-5.6-terra",
        model_family="gpt-5.6",
    )
    _write_records(
        candidate.source_path,
        [
            {"type": "model_change", "model": "openai-codex/gpt-5.6-terra"},
            {
                "type": "message",
                "timestamp": "2026-07-30T17:00:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Inspect the code."}],
                },
            },
            {
                "type": "message",
                "timestamp": "2026-07-30T17:00:01Z",
                "message": {
                    "role": "assistant",
                    "model": "gpt-5.6-terra",
                    "usage": {"input": 100, "output": 20, "cacheRead": 50, "cacheWrite": 10},
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "read",
                            "arguments": {"path": "src/app.py"},
                        }
                    ],
                },
            },
            {
                "type": "message",
                "timestamp": "2026-07-30T17:00:02Z",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "content": [{"type": "text", "text": "print('ok')"}],
                },
            },
        ],
    )
    candidate = _refresh_candidate_hash(candidate)

    session = extract_omp(candidate)

    validate_confirmatory_evidence(candidate, session)
    assert session.model == "openai-codex/gpt-5.6-terra"
    assert session.events[1].usage == BillableUsage(100, 20, 50, 10)
    assert session.events[2].tool_result == ToolResult(
        "call-1", [{"type": "text", "text": "print('ok')"}]
    )


def test_codex_probe_refuses_unlinked_billable_usage(tmp_path: Path) -> None:
    candidate = _candidate(
        tmp_path,
        provider="codex",
        model="gpt-5.6",
        model_family="gpt-5.6",
    )
    _write_records(
        candidate.source_path,
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "read",
                    "arguments": '{"path":"src/app.py"}',
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "print('ok')",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "cached_input_tokens": 50,
                        }
                    },
                },
            },
        ],
    )
    candidate = _refresh_candidate_hash(candidate)

    with pytest.raises(NativeEvidenceError, match="has no response identifier"):
        extract_native(candidate)


def _write_records(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records), encoding="utf-8"
    )


def _refresh_candidate_hash(candidate: Candidate) -> Candidate:
    return Candidate(
        candidate_id=candidate.candidate_id,
        source_path=candidate.source_path,
        source_sha256=source_sha256(candidate.source_path),
        provider=candidate.provider,
        model=candidate.model,
        model_family=candidate.model_family,
        project=candidate.project,
        timestamp=candidate.timestamp,
        session_length=candidate.session_length,
        message_count=candidate.message_count,
        has_code=candidate.has_code,
        tool_density=candidate.tool_density,
        time_period=candidate.time_period,
        session_size_band=candidate.session_size_band,
        selection_stratum=candidate.selection_stratum,
        lineage=candidate.lineage,
        eligibility_disposition=candidate.eligibility_disposition,
        split=candidate.split,
    )


def _replace_events(session: NativeSession, events: tuple[NativeEvent, ...]) -> NativeSession:
    return NativeSession(
        candidate_id=session.candidate_id,
        provider=session.provider,
        parser=session.parser,
        source_path=session.source_path,
        source_sha256=session.source_sha256,
        model=session.model,
        events=events,
    )
