"""Conformance tests for K1 native evidence extraction."""

from __future__ import annotations

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
from laconic.k1.manifest import Candidate, source_sha256


def _candidate(tmp_path: Path) -> Candidate:
    source = tmp_path / "session.jsonl"
    source.write_text('{"native":"evidence"}\n', encoding="utf-8")
    return Candidate(
        candidate_id="session-a",
        source_path=source.resolve(),
        source_sha256=source_sha256(source),
        provider="claude-code",
        model="claude-sonnet-4-6",
        model_family="claude-4",
        project="acme/api",
        timestamp="2026-07-30T17:00:00Z",
        session_length=3,
        message_count=3,
        has_code=True,
        tool_density=0.5,
        time_period="2026-Q3",
        session_size_band="small",
        selection_stratum="claude-code|claude-4|2026-Q3|small",
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
