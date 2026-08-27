"""Defense-in-depth privacy validator for Observe receipts.

:class:`~laconic.observe.receipt.Receipt`'s dataclass shape already limits
what can be constructed, but a future field added to it without updating
this allowlist should fail loudly rather than silently start persisting
an unreviewed field. :func:`validate_receipt_json` is the gate every
serialized receipt must clear before :mod:`laconic.observe.audit` appends
it -- it inspects only the exact key set and each field's own enum
membership, never receipt content, since a receipt carries no content to
inspect.
"""

from __future__ import annotations

from typing import Any

from laconic.observe.contracts import ClientId
from laconic.observe.receipt import ResultClass, SizeBand, ToolCategory

#: The exact key set a serialized receipt may ever contain. An extra key
#: -- a typo, a future field added without updating this allowlist, an
#: injected key -- fails closed; so does a missing one.
ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "adapter",
        "adapter_schema_version",
        "session_id",
        "tool_category",
        "argument_size",
        "result_size",
        "result_class",
        "timestamp",
    }
)

_ALLOWED_ADAPTERS = frozenset(member.value for member in ClientId)
_ALLOWED_TOOL_CATEGORIES = frozenset(member.value for member in ToolCategory)
_ALLOWED_SIZE_BANDS = frozenset(member.value for member in SizeBand)
_ALLOWED_RESULT_CLASSES = frozenset(member.value for member in ResultClass)

#: Substrings that must never appear inside `session_id` -- catches a
#: receipt accidentally built from a path or transcript reference instead
#: of an opaque identifier.
_PROHIBITED_SESSION_ID_SUBSTRINGS = ("/", "\\", "\n")


class PrivacyViolationError(ValueError):
    """Raised when a serialized receipt carries a key or value this
    module cannot certify as content-free."""


def validate_receipt_json(payload: dict[str, Any]) -> None:
    """Raise :class:`PrivacyViolationError` unless ``payload`` has exactly
    :data:`ALLOWED_KEYS` and every enum-valued field is a real, known
    member -- never free text."""
    extra_keys = set(payload) - ALLOWED_KEYS
    if extra_keys:
        raise PrivacyViolationError(f"unallowlisted receipt key(s): {sorted(extra_keys)}")
    missing_keys = ALLOWED_KEYS - set(payload)
    if missing_keys:
        raise PrivacyViolationError(f"missing receipt key(s): {sorted(missing_keys)}")

    if payload["adapter"] not in _ALLOWED_ADAPTERS:
        raise PrivacyViolationError(f"adapter not a known client: {payload['adapter']!r}")
    if payload["tool_category"] not in _ALLOWED_TOOL_CATEGORIES:
        raise PrivacyViolationError(
            f"tool_category not a known category: {payload['tool_category']!r}"
        )
    if payload["argument_size"] not in _ALLOWED_SIZE_BANDS:
        raise PrivacyViolationError(f"argument_size not a known band: {payload['argument_size']!r}")
    if payload["result_size"] not in _ALLOWED_SIZE_BANDS:
        raise PrivacyViolationError(f"result_size not a known band: {payload['result_size']!r}")
    if payload["result_class"] not in _ALLOWED_RESULT_CLASSES:
        raise PrivacyViolationError(f"result_class not a known class: {payload['result_class']!r}")

    session_id = payload["session_id"]
    if not isinstance(session_id, str) or not session_id:
        raise PrivacyViolationError("session_id must be a non-empty string")
    for banned in _PROHIBITED_SESSION_ID_SUBSTRINGS:
        if banned in session_id:
            raise PrivacyViolationError(
                f"session_id contains a path/content-shaped substring: {banned!r}"
            )

    if not isinstance(payload["schema_version"], int):
        raise PrivacyViolationError("schema_version must be an int")
    if not isinstance(payload["adapter_schema_version"], int):
        raise PrivacyViolationError("adapter_schema_version must be an int")
    if not isinstance(payload["timestamp"], int | float):
        raise PrivacyViolationError("timestamp must be a number")
