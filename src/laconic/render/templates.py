"""Byte-stable templates for structural, handle-provenanced trace facts."""

from __future__ import annotations

import json

from laconic.ledger import ObservationKind
from laconic.render.view import TraceEntry

_KIND_LABELS: dict[ObservationKind, str] = {
    ObservationKind.FILE: "read",
    ObservationKind.COMMAND: "ran",
    ObservationKind.SEARCH: "searched",
    ObservationKind.FETCH: "fetched",
    ObservationKind.OTHER: "observed",
}


def render(entries: tuple[TraceEntry, ...]) -> str:
    """Render structural trace entries into provenance-tagged lines.

    Each non-empty line is one claim derived entirely from a ledger record and
    ends in that record's handle. No model, wall-clock value, or mutable
    process state participates, so equal entries always render identically.
    """
    return "\n".join(_render_entry(entry) for entry in entries)


def _render_entry(entry: TraceEntry) -> str:
    record = entry.record
    subject = json.dumps(record.subject, ensure_ascii=True)
    label = _KIND_LABELS[record.kind]
    return (
        f"Turn {entry.turn}: {label} {subject} (result: {record.raw_chars} chars) [{record.handle}]"
    )
