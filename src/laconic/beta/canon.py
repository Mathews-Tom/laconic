"""Deterministic canonical JSON and SHA-256 fingerprints for M18 evidence.

Every hash an M18 artifact binds to -- a frozen manifest, a receipt's own
schema contract, a rendered report's freshness check -- must reproduce
identically across machines and Python versions given the same logical
content. ``json.dumps`` with sorted keys, compact separators, and
ASCII-only output is exactly the encoding `laconic.observe.audit` already
uses for its hash-chained entries; reusing it here keeps one canonical-JSON
convention across the repository instead of a second, subtly different one.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(payload: Any) -> str:
    """Render ``payload`` as byte-stable JSON text.

    Sorted keys make field order irrelevant; compact separators and
    ``ensure_ascii`` make the output independent of locale-default
    whitespace and of whether a caller's JSON library escapes non-ASCII
    characters.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_sha256(payload: Any) -> str:
    """Return the SHA-256 hex digest of ``payload``'s canonical JSON."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
