"""Independent privacy gate over anything the spend surface serializes.

Modelled on :mod:`laconic.beta.privacy`: an exact-key allowlist plus a shape
check per key, run as a second opinion rather than as the only opinion. The
builder in :mod:`laconic.spend.report` is already careful; this module exists
so that a field added there cannot reach disk without passing through a
deliberate decision here.

It also enforces something a key allowlist alone cannot: that the report
still carries its complete limitations block. A report that quietly dropped
its single-arm caveat would otherwise be perfectly content-free and read
exactly like a savings result.
"""

from __future__ import annotations

import math
from typing import Any, Final

from laconic import __version__
from laconic.spend.report import (
    ALLOWED_CODEC_KEYS,
    ALLOWED_COST_KEYS,
    ALLOWED_ESTIMATE_KEYS,
    ALLOWED_REPORT_KEYS,
    ALLOWED_SESSION_KEYS,
    ALLOWED_TOKEN_KEYS,
    ESTIMATE_BASIS,
    GENERATION_BASIS,
    LIMITATIONS,
    PRIVACY_STATUS,
    REPORT_SCHEMA_VERSION,
    SOURCE_FRESHNESS,
)

_HEX_64: Final = frozenset("0123456789abcdef")

#: The longest a provider's model identifier may plausibly be. A value longer
#: than this is not a model name, whatever else it is.
_MAX_MODEL_LENGTH: Final = 64

#: Report keys whose value is a token block.
_TOKEN_BLOCK_KEYS: Final = frozenset({"corpus_tokens", "matched_tokens"})

#: Report keys whose value is a cost block.
_COST_BLOCK_KEYS: Final = frozenset({"corpus_cost", "matched_cost", "host_reporting_cost"})

#: Report keys whose value is a share block, or ``None`` when there was no
#: spend to apportion.
_SHARE_BLOCK_KEYS: Final = frozenset({"corpus_shares", "matched_shares"})

#: Report keys whose value is a plain USD float.
_USD_KEYS: Final = frozenset({"corpus_host_cost_usd", "matched_host_cost_usd"})

#: Report keys whose value must be a bounded percentage.
_PERCENT_KEYS: Final = frozenset({"fallback_priced_cost_share_pct"})

#: Report keys whose value is a list of model or schema-key identifiers.
_IDENTIFIER_LIST_KEYS: Final = frozenset({"unpriced_models", "unknown_usage_keys"})

#: Report keys this module checks individually rather than by group.
_INLINE_CHECKED_KEYS: Final = frozenset(
    {
        "schema_version",
        "laconic_version",
        "generation_basis",
        "source_freshness",
        "privacy_status",
        "codec",
        "sessions",
        "limitations",
        "estimate",
    }
)

#: Everything else must be a non-negative integer. Derived by set difference,
#: not enumerated: a key added to ``ALLOWED_REPORT_KEYS`` and to no shape
#: group above lands here and fails loudly, rather than being allowlisted
#: into the artifact with no shape check at all. This mirrors
#: :mod:`laconic.beta.privacy`, whose whole point is that the gate's
#: completeness is enforced rather than coincidental.
_INT_KEYS: Final = ALLOWED_REPORT_KEYS - (
    _TOKEN_BLOCK_KEYS
    | _COST_BLOCK_KEYS
    | _SHARE_BLOCK_KEYS
    | _PERCENT_KEYS
    | _USD_KEYS
    | _IDENTIFIER_LIST_KEYS
    | _INLINE_CHECKED_KEYS
)

#: Per-session keys whose value must be a non-negative integer. Derived the
#: same way, so a new per-session field cannot escape a shape check either.
_SESSION_INLINE_CHECKED_KEYS: Final = frozenset(
    {"session_hash", "nested", "tokens", "modelled_cost_usd", "host_cost_usd"}
)
_SESSION_INT_KEYS: Final = ALLOWED_SESSION_KEYS - _SESSION_INLINE_CHECKED_KEYS


class PrivacyViolationError(ValueError):
    """Raised when a serialized spend artifact carries a key or value this
    module cannot certify as content-free, or has lost its limitations."""


def _require_int(field: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PrivacyViolationError(f"{field} must be an integer")
    if value < 0:
        raise PrivacyViolationError(f"{field} must not be negative")
    return value


def _require_float(field: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PrivacyViolationError(f"{field} must be a number")
    result = float(value)
    # json.loads accepts bare NaN and Infinity, and json.dumps writes them
    # back as literal tokens that are not JSON. Negative spend is invalid too.
    if not math.isfinite(result):
        raise PrivacyViolationError(f"{field} must be finite")
    if result < 0:
        raise PrivacyViolationError(f"{field} must not be negative")
    return result


def _require_percentage(field: str, value: Any) -> float:
    result = _require_float(field, value)
    if result > 100 and not math.isclose(result, 100, abs_tol=1e-6):
        raise PrivacyViolationError(f"{field} must not exceed 100")
    return result


def _require_exact_keys(field: str, value: Any, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PrivacyViolationError(f"{field} must be an object")
    extra = set(value) - allowed
    if extra:
        raise PrivacyViolationError(f"unallowlisted {field} key(s): {sorted(extra)}")
    missing = allowed - set(value)
    if missing:
        raise PrivacyViolationError(f"missing {field} key(s): {sorted(missing)}")
    return value


def _require_identifier(field: str, value: Any) -> None:
    """Accept a provider model name or a host usage key; refuse a path.

    A real identifier is a short printable token. ``~anthropic/claude-opus-latest``
    is one and contains a slash, so a bare slash cannot be the test; an
    absolute path, a parent traversal, a backslash, or embedded whitespace
    cannot be one.
    """
    if not isinstance(value, str) or not value:
        raise PrivacyViolationError(f"{field} entries must be non-empty strings")
    if len(value) > _MAX_MODEL_LENGTH:
        raise PrivacyViolationError(f"{field} entry is too long to be an identifier")
    if any(character.isspace() for character in value):
        raise PrivacyViolationError(f"{field} entry contains whitespace")
    if not value.isprintable():
        raise PrivacyViolationError(f"{field} entry is not printable")
    if value.startswith(("/", ".", "~/")) or "\\" in value or ".." in value:
        raise PrivacyViolationError(f"{field} entry looks like a filesystem path")


def _validate_cost_block(field: str, value: Any, *, optional: bool) -> None:
    if value is None:
        if not optional:
            raise PrivacyViolationError(f"{field} must not be null")
        return
    block = _require_exact_keys(field, value, ALLOWED_COST_KEYS)
    for key in sorted(ALLOWED_COST_KEYS):
        _require_float(f"{field}.{key}", block[key])


def _validate_share_block(field: str, value: Any) -> None:
    if value is None:
        return
    block = _require_exact_keys(field, value, ALLOWED_COST_KEYS)
    for key in sorted(ALLOWED_COST_KEYS):
        _require_percentage(f"{field}.{key}", block[key])


def validate_session_json(payload: Any) -> None:
    """Raise unless one per-session entry is exactly a content-free row."""
    entry = _require_exact_keys("session", payload, ALLOWED_SESSION_KEYS)
    digest = entry["session_hash"]
    if not isinstance(digest, str) or len(digest) != 64 or set(digest) - _HEX_64:
        raise PrivacyViolationError(
            "session_hash must be a 64-character lowercase hex digest, never a session id"
        )
    if not isinstance(entry["nested"], bool):
        raise PrivacyViolationError("session.nested must be a bool")
    for key in sorted(_SESSION_INT_KEYS):
        _require_int(f"session.{key}", entry[key])
    for key in ("modelled_cost_usd", "host_cost_usd"):
        _require_float(f"session.{key}", entry[key])
    tokens = _require_exact_keys("session.tokens", entry["tokens"], ALLOWED_TOKEN_KEYS)
    for key in sorted(ALLOWED_TOKEN_KEYS):
        _require_int(f"session.tokens.{key}", tokens[key])


def _validate_estimate(value: Any) -> None:
    """Raise unless the avoided-cost block is a labelled, content-free model.

    ``None`` is allowed: a corpus with no cached tokens or no removed
    characters cannot support the model, and saying so is honest. Otherwise
    every field is required and typed. A modelled dollar figure that loses
    the word saying it is modelled is exactly the artifact this gate exists
    to stop -- it would validate cleanly and read as a measured saving.
    """
    if value is None:
        return
    block = _require_exact_keys("estimate", value, ALLOWED_ESTIMATE_KEYS)
    if block["basis"] != ESTIMATE_BASIS:
        raise PrivacyViolationError(
            f"estimate.basis must be {ESTIMATE_BASIS!r}, so a modelled figure "
            "can never be serialized as a measured one"
        )
    _require_int("estimate.chars_avoided", block["chars_avoided"])
    for name in sorted(ALLOWED_ESTIMATE_KEYS - {"basis", "chars_avoided"}):
        validator = _require_percentage if name.endswith("_pct") else _require_float
        validator(f"estimate.{name}", block[name])
    if block["avoided_cost_usd_low"] > block["avoided_cost_usd_high"]:
        raise PrivacyViolationError("estimate band is inverted")


def validate_report_json(payload: Any) -> None:
    """Raise unless ``payload`` is exactly a content-free spend report.

    Checks the allowlist, every value's shape, the immutable provenance
    labels, and the complete ordered limitation vocabulary.
    """
    report = _require_exact_keys("report", payload, ALLOWED_REPORT_KEYS)

    for key in sorted(_INT_KEYS):
        _require_int(key, report[key])
    for key in sorted(_USD_KEYS):
        _require_float(key, report[key])
    for key in sorted(_PERCENT_KEYS):
        _require_percentage(key, report[key])
    for key in sorted(_TOKEN_BLOCK_KEYS):
        block = _require_exact_keys(key, report[key], ALLOWED_TOKEN_KEYS)
        for name in sorted(ALLOWED_TOKEN_KEYS):
            _require_int(f"{key}.{name}", block[name])
    for key in sorted(_COST_BLOCK_KEYS):
        _validate_cost_block(key, report[key], optional=False)
    for key in sorted(_SHARE_BLOCK_KEYS):
        _validate_share_block(key, report[key])

    _validate_estimate(report["estimate"])

    codec = _require_exact_keys("codec", report["codec"], ALLOWED_CODEC_KEYS)
    for name in sorted(ALLOWED_CODEC_KEYS):
        _require_int(f"codec.{name}", codec[name])

    if report["schema_version"] != REPORT_SCHEMA_VERSION:
        raise PrivacyViolationError("unsupported spend report schema")
    for field, expected in (
        ("generation_basis", GENERATION_BASIS),
        ("source_freshness", SOURCE_FRESHNESS),
        ("privacy_status", PRIVACY_STATUS),
    ):
        if report[field] != expected:
            raise PrivacyViolationError(f"{field} must be {expected!r}")

    version = report["laconic_version"]
    if version != __version__:
        raise PrivacyViolationError("laconic_version must match the running package")

    for key in sorted(_IDENTIFIER_LIST_KEYS):
        values = report[key]
        if not isinstance(values, list):
            raise PrivacyViolationError(f"{key} must be a list")
        for value in values:
            _require_identifier(key, value)

    sessions = report["sessions"]
    if not isinstance(sessions, list):
        raise PrivacyViolationError("sessions must be a list")
    for entry in sessions:
        validate_session_json(entry)

    limitations = report["limitations"]
    if not isinstance(limitations, list) or tuple(limitations) != LIMITATIONS:
        raise PrivacyViolationError(
            "limitations must be exactly the closed vocabulary, in order; a report "
            "missing its single-arm caveat reads as a savings result"
        )
