"""Cross-adapter audit and privacy regression coverage (M2 PR-3).

Combines every M2 module across full session-shaped flows for both
clients: payload -> receipt -> privacy validation -> hash-chained audit
-> verification. Property tests use Hypothesis to fuzz payload content
and receipt keys well beyond the fixed fixtures PR-1/PR-2 use, matching
this repository's existing property-test convention
(`tests/test_recoverability.py`, `tests/test_encoders.py`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from laconic.observe.audit import AuditIntegrityError, append_to_file, read_chain, verify_chain
from laconic.observe.contracts import ClientId
from laconic.observe.privacy import ALLOWED_KEYS, PrivacyViolationError, validate_receipt_json
from laconic.observe.receipt import ToolCategory, build_claude_code_receipt, build_omp_receipt

PROPERTY = settings(deadline=None, max_examples=50)

_JSON_LEAF = st.none() | st.booleans() | st.integers() | st.text(max_size=40)
_JSON_VALUE = st.recursive(
    _JSON_LEAF,
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=10), children, max_size=3)
    ),
    max_leaves=8,
)


def _claude_code_success(*, tool_input: object, tool_response: object) -> dict[str, object]:
    return {
        "session_id": "s-1",
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": tool_input,
        "tool_response": tool_response,
    }


def _omp_success(*, argument: object, content: object) -> dict[str, object]:
    return {
        "session_id": "s-1",
        "event": "tool_result",
        "toolName": "write",
        "isError": False,
        "input": argument,
        "content": content,
    }


def test_a_full_claude_code_session_chains_and_verifies(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    payloads = [
        _claude_code_success(tool_input={"file_path": "/a"}, tool_response={"success": True}),
        {
            "session_id": "s-1",
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash",
            "tool_input": {"command": "false"},
            "error": "Exit code 1",
        },
        {"session_id": "s-1", "hook_event_name": "SessionEnd", "reason": "other"},
    ]
    for payload in payloads:
        receipt = build_claude_code_receipt(payload, now=0.0).to_json()
        validate_receipt_json(receipt)
        append_to_file(audit_path, receipt)

    chain = read_chain(audit_path)
    assert len(chain) == 3
    verify_chain(chain)


def test_a_full_omp_session_chains_and_verifies(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    payloads = [
        _omp_success(argument={"path": "/a"}, content=[{"type": "text", "text": "ok"}]),
        {"session_id": "s-1", "event": "tool_result", "toolName": "bash", "isError": True},
        {"session_id": "s-1", "event": "session_shutdown"},
    ]
    for payload in payloads:
        receipt = build_omp_receipt(payload, now=0.0).to_json()
        validate_receipt_json(receipt)
        append_to_file(audit_path, receipt)

    chain = read_chain(audit_path)
    assert len(chain) == 3
    verify_chain(chain)


def test_a_mixed_client_audit_file_still_chains_and_verifies(tmp_path: Path) -> None:
    """One project could run both clients against the same tree; the
    audit file itself is adapter-agnostic and must not care which client
    wrote which entry."""
    audit_path = tmp_path / "audit.jsonl"
    cc_receipt = build_claude_code_receipt(
        _claude_code_success(tool_input={"a": 1}, tool_response={"b": 2}), now=0.0
    ).to_json()
    omp_receipt = build_omp_receipt(
        _omp_success(argument={"a": 1}, content={"b": 2}), now=0.0
    ).to_json()

    append_to_file(audit_path, cc_receipt)
    append_to_file(audit_path, omp_receipt)

    chain = read_chain(audit_path)
    assert chain[0].receipt["adapter"] == ClientId.CLAUDE_CODE.value
    assert chain[1].receipt["adapter"] == ClientId.OMP.value
    verify_chain(chain)


def test_tampering_with_a_written_audit_file_is_detected(tmp_path: Path) -> None:
    """A receipt's line is mutated on disk after being written -- the
    kind of tamper a local hash chain exists to catch."""
    audit_path = tmp_path / "audit.jsonl"
    receipt = build_claude_code_receipt(
        _claude_code_success(tool_input={}, tool_response={"success": True}), now=0.0
    ).to_json()
    append_to_file(audit_path, receipt)

    text = audit_path.read_text().replace('"file_write"', '"network"')
    audit_path.write_text(text)

    with pytest.raises(AuditIntegrityError):
        verify_chain(read_chain(audit_path))


def test_claude_code_and_omp_tool_categorization_stay_independent() -> None:
    """A lowercase OMP-shaped tool name must not accidentally match
    Claude Code's (capitalized) category table, and vice versa -- the two
    adapters must never silently share a lookup table."""
    cc_with_omp_style_name = build_claude_code_receipt(
        {
            "session_id": "s-1",
            "hook_event_name": "PostToolUse",
            "tool_name": "write",  # OMP's spelling, not Claude Code's "Write"
            "tool_input": {},
            "tool_response": {},
        },
        now=0.0,
    )
    assert cc_with_omp_style_name.tool_category is ToolCategory.OTHER

    omp_with_cc_style_name = build_omp_receipt(
        {
            "session_id": "s-1",
            "event": "tool_result",
            "toolName": "Write",  # Claude Code's spelling, not OMP's "write"
            "isError": False,
        },
        now=0.0,
    )
    assert omp_with_cc_style_name.tool_category is ToolCategory.OTHER


@PROPERTY
@given(tool_input=_JSON_VALUE, tool_response=_JSON_VALUE)
def test_claude_code_receipt_json_never_contains_argument_or_result_content(
    tool_input: object, tool_response: object
) -> None:
    payload = _claude_code_success(tool_input=tool_input, tool_response=tool_response)
    receipt = build_claude_code_receipt(payload, now=0.0)
    validate_receipt_json(receipt.to_json())  # must always pass the allowlist gate too


@PROPERTY
@given(argument=_JSON_VALUE, content=_JSON_VALUE)
def test_omp_receipt_json_never_contains_argument_or_result_content(
    argument: object, content: object
) -> None:
    payload = _omp_success(argument=argument, content=content)
    receipt = build_omp_receipt(payload, now=0.0)
    validate_receipt_json(receipt.to_json())


@PROPERTY
@given(extra_key=st.text(min_size=1, max_size=20).filter(lambda k: k not in ALLOWED_KEYS))
def test_privacy_validator_rejects_any_extra_key(extra_key: str) -> None:
    valid = build_claude_code_receipt(
        _claude_code_success(tool_input={}, tool_response={}), now=0.0
    ).to_json()
    poisoned = {**valid, extra_key: "anything"}
    with pytest.raises(PrivacyViolationError):
        validate_receipt_json(poisoned)


@PROPERTY
@given(dropped_key=st.sampled_from(sorted(ALLOWED_KEYS)))
def test_privacy_validator_rejects_any_missing_key(dropped_key: str) -> None:
    valid = build_claude_code_receipt(
        _claude_code_success(tool_input={}, tool_response={}), now=0.0
    ).to_json()
    incomplete = {k: v for k, v in valid.items() if k != dropped_key}
    with pytest.raises(PrivacyViolationError):
        validate_receipt_json(incomplete)
