"""Local, hash-chained Observe audit log.

Each entry commits to the previous entry's hash, so a local tamper or gap
breaks the chain visibly (:func:`verify_chain`). This is a local
integrity aid only, not a distributed or remote WORM guarantee --
`docs/grounding.md` and `.docs/K1_REPRESENTATIVE_CORPUS_PROTOCOL.md`
explicitly keep provenance/WORM infrastructure out of Observe's scope, and
nothing here writes to, or reads from, anything but a local file.

Every entry wraps an already privacy-validated receipt
(:func:`laconic.observe.privacy.validate_receipt_json`); this module never
inspects receipt content itself and stores exactly what it is given.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Sentinel previous-hash for the first entry in a chain.
GENESIS_HASH = "0" * 64

#: Default project-scoped audit file location, mirroring the existing
#: gitignored `.laconic/k1/` local-state convention.
DEFAULT_AUDIT_PATH = Path(".laconic/observe/audit.jsonl")


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One hash-chained audit record wrapping a receipt payload."""

    sequence: int
    receipt: dict[str, Any]
    previous_hash: str
    entry_hash: str

    def to_json(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "receipt": self.receipt,
            "previous_hash": self.previous_hash,
            "entry_hash": self.entry_hash,
        }


def _entry_hash(sequence: int, receipt: dict[str, Any], previous_hash: str) -> str:
    canonical = json.dumps(
        {"sequence": sequence, "receipt": receipt, "previous_hash": previous_hash},
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def append(chain: tuple[AuditEntry, ...], receipt: dict[str, Any]) -> tuple[AuditEntry, ...]:
    """Append one receipt to an in-memory chain, returning the extended chain."""
    sequence = len(chain)
    previous_hash = chain[-1].entry_hash if chain else GENESIS_HASH
    entry_hash = _entry_hash(sequence, receipt, previous_hash)
    entry = AuditEntry(
        sequence=sequence, receipt=receipt, previous_hash=previous_hash, entry_hash=entry_hash
    )
    return (*chain, entry)


class AuditIntegrityError(ValueError):
    """Raised when a chain's stored hashes do not reproduce from its content."""


def verify_chain(chain: tuple[AuditEntry, ...]) -> None:
    """Raise :class:`AuditIntegrityError` unless every entry's stored hash
    matches its recomputed hash and every ``previous_hash`` matches the
    prior entry's ``entry_hash``."""
    expected_previous = GENESIS_HASH
    for entry in chain:
        if entry.previous_hash != expected_previous:
            raise AuditIntegrityError(
                f"entry {entry.sequence}: previous_hash mismatch "
                f"(expected {expected_previous!r}, got {entry.previous_hash!r})"
            )
        recomputed = _entry_hash(entry.sequence, entry.receipt, entry.previous_hash)
        if recomputed != entry.entry_hash:
            raise AuditIntegrityError(f"entry {entry.sequence}: entry_hash does not match content")
        expected_previous = entry.entry_hash


def read_chain(path: Path) -> tuple[AuditEntry, ...]:
    """Read an audit file back into a chain. A missing file is an empty
    chain, not an error -- there is nothing to audit yet."""
    if not path.exists():
        return ()
    entries = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            entries.append(
                AuditEntry(
                    sequence=row["sequence"],
                    receipt=row["receipt"],
                    previous_hash=row["previous_hash"],
                    entry_hash=row["entry_hash"],
                )
            )
    return tuple(entries)


def append_to_file(path: Path, receipt: dict[str, Any]) -> AuditEntry:
    """Append one receipt to the on-disk chain at ``path``, creating the
    file and its parent directory if this is the first entry.

    Reads the existing chain to determine the next ``previous_hash``, so
    each call reflects every prior call against the same file -- across
    process boundaries, since each hook invocation is a fresh subprocess.
    """
    chain = read_chain(path)
    extended = append(chain, receipt)
    entry = extended[-1]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry.to_json(), sort_keys=True))
        handle.write("\n")
    return entry
