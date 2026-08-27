"""Privacy validator: exact-key allowlist and enum-membership checks over
a serialized receipt, independent of `Receipt`'s own dataclass shape."""

from __future__ import annotations

import pytest

from laconic.observe.privacy import PrivacyViolationError, validate_receipt_json
from laconic.observe.receipt import build_claude_code_receipt

_VALID_PAYLOAD = build_claude_code_receipt(
    {
        "session_id": "abc123",
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/x"},
        "tool_response": {"success": True},
    },
    now=1000.0,
).to_json()


def test_valid_receipt_passes() -> None:
    validate_receipt_json(dict(_VALID_PAYLOAD))


def test_extra_key_is_rejected() -> None:
    poisoned = {**_VALID_PAYLOAD, "tool_input": {"file_path": "/secret/plan.md"}}
    with pytest.raises(PrivacyViolationError, match="unallowlisted"):
        validate_receipt_json(poisoned)


def test_missing_key_is_rejected() -> None:
    incomplete = {k: v for k, v in _VALID_PAYLOAD.items() if k != "session_id"}
    with pytest.raises(PrivacyViolationError, match="missing"):
        validate_receipt_json(incomplete)


def test_unknown_tool_category_value_is_rejected() -> None:
    poisoned = {**_VALID_PAYLOAD, "tool_category": "read /etc/passwd"}
    with pytest.raises(PrivacyViolationError, match="tool_category"):
        validate_receipt_json(poisoned)


def test_unknown_size_band_value_is_rejected() -> None:
    poisoned = {**_VALID_PAYLOAD, "argument_size": "1234 bytes exactly"}
    with pytest.raises(PrivacyViolationError, match="argument_size"):
        validate_receipt_json(poisoned)


def test_unknown_result_class_value_is_rejected() -> None:
    poisoned = {**_VALID_PAYLOAD, "result_class": "timeout after connecting to db.internal"}
    with pytest.raises(PrivacyViolationError, match="result_class"):
        validate_receipt_json(poisoned)


def test_unknown_adapter_value_is_rejected() -> None:
    poisoned = {**_VALID_PAYLOAD, "adapter": "cursor"}
    with pytest.raises(PrivacyViolationError, match="adapter"):
        validate_receipt_json(poisoned)


@pytest.mark.parametrize("banned", ["/etc/passwd", "C:\\Users\\name", "line1\nline2"])
def test_session_id_shaped_like_a_path_is_rejected(banned: str) -> None:
    poisoned = {**_VALID_PAYLOAD, "session_id": banned}
    with pytest.raises(PrivacyViolationError, match="session_id"):
        validate_receipt_json(poisoned)


def test_empty_session_id_is_rejected() -> None:
    poisoned = {**_VALID_PAYLOAD, "session_id": ""}
    with pytest.raises(PrivacyViolationError, match="session_id"):
        validate_receipt_json(poisoned)


def test_non_numeric_timestamp_is_rejected() -> None:
    poisoned = {**_VALID_PAYLOAD, "timestamp": "just now"}
    with pytest.raises(PrivacyViolationError, match="timestamp"):
        validate_receipt_json(poisoned)
