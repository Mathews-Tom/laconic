"""Deterministic public demonstration built by Laconic's real file codec."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from laconic.codec.observe import ObservationCodec
from laconic.ledger import Ledger
from laconic.runtime.engine import recovery_envelope
from laconic.runtime.references import RuntimeReference

_DEMO_SESSION = "pages-demo"
_DEMO_SUBJECT = "demo/recovery.py"
_DEMO_SPAN = "1-12"


def _demo_source() -> str:
    header = '''"""A deterministic recovery ledger used by the public Laconic demo."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RecoveryEntry:
    key: str
    value: str
'''
    functions = []
    for index in range(1, 13):
        functions.append(
            f'''\ndef checkpoint_{index}(entry: RecoveryEntry) -> str:
    """Validate and return checkpoint {index} without changing its content."""
    if not entry.key:
        raise ValueError("checkpoint keys must not be empty")
    prefix = "checkpoint-{index}:"
    payload = entry.value
    return prefix + payload
'''
        )
    return header + "".join(functions)


@dataclass(frozen=True, slots=True)
class DemoResult:
    """The stable, public projection of one real codec and recovery run."""

    subject: str
    envelope: str
    outline: str
    reference: str
    range_reference: str
    range_preview: str
    raw_chars: int
    visible_chars: int
    reduction_pct: float
    recovery_sha256: str


def build_demo() -> DemoResult:
    """Encode, strictly-size-check, and exactly recover one public fixture."""

    raw = _demo_source()
    ledger = Ledger(":memory:", _DEMO_SESSION)
    try:
        record = ObservationCodec(ledger).encode(
            "Read",
            _DEMO_SUBJECT,
            raw,
            {},
            turn=1,
        )
        reference = str(RuntimeReference.from_ledger_reference(_DEMO_SESSION, record.handle))
        envelope = recovery_envelope(reference, record.encoded)
        if len(envelope) >= len(raw):
            raise RuntimeError("public demo envelope is not strictly smaller")
        recovered = ledger.expand(record.handle)
        if recovered != raw:
            raise RuntimeError("public demo full recovery mismatch")
        range_reference = f"{reference}:{_DEMO_SPAN}"
        range_preview = ledger.expand(f"{record.handle}:{_DEMO_SPAN}")
        expected_range = "\n".join(raw.split("\n")[:12])
        if range_preview != expected_range:
            raise RuntimeError("public demo range recovery mismatch")
    finally:
        ledger.close()

    return DemoResult(
        subject=_DEMO_SUBJECT,
        envelope=envelope,
        outline=record.encoded,
        reference=reference,
        range_reference=range_reference,
        range_preview=range_preview,
        raw_chars=len(raw),
        visible_chars=len(envelope),
        reduction_pct=round(100.0 * (len(raw) - len(envelope)) / len(raw), 2),
        recovery_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )
