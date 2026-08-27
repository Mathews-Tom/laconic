"""M3 PR-3: local Observe status and report views.

Reads only the local hash-chained audit log
(:func:`laconic.observe.audit.read_chain`). Never contacts a provider,
never reads a real client configuration, never changes codec or K1
state -- these views exist to answer "is Observe collecting anything,
and does it look intact," nothing more.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from laconic.observe.audit import AuditIntegrityError, read_chain, verify_chain


@dataclass(frozen=True, slots=True)
class ObserveStatus:
    """A quick local health check: does the audit file exist, how many
    receipts does it hold, and does its hash chain still verify."""

    path: Path
    exists: bool
    entry_count: int
    chain_valid: bool
    integrity_error: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "exists": self.exists,
            "entry_count": self.entry_count,
            "chain_valid": self.chain_valid,
            "integrity_error": self.integrity_error,
        }


def compute_status(path: Path) -> ObserveStatus:
    """Compute :class:`ObserveStatus` for the audit file at ``path``. A
    missing file is a valid, empty status, not an error."""
    chain = read_chain(path)
    chain_valid = True
    integrity_error: str | None = None
    try:
        verify_chain(chain)
    except AuditIntegrityError as error:
        chain_valid = False
        integrity_error = str(error)
    return ObserveStatus(
        path=path,
        exists=path.exists(),
        entry_count=len(chain),
        chain_valid=chain_valid,
        integrity_error=integrity_error,
    )


@dataclass(frozen=True, slots=True)
class ObserveReport:
    """A breakdown of every receipt in the audit log by each allowlisted
    dimension -- adapter, tool category, result class, and size bands.
    Every count is a whole receipt; no field here is derived from
    anything but the receipts' own already-content-free fields."""

    path: Path
    entry_count: int
    by_adapter: dict[str, int]
    by_tool_category: dict[str, int]
    by_result_class: dict[str, int]
    by_argument_size: dict[str, int]
    by_result_size: dict[str, int]

    def to_json(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "entry_count": self.entry_count,
            "by_adapter": self.by_adapter,
            "by_tool_category": self.by_tool_category,
            "by_result_class": self.by_result_class,
            "by_argument_size": self.by_argument_size,
            "by_result_size": self.by_result_size,
        }


def compute_report(path: Path) -> ObserveReport:
    """Compute :class:`ObserveReport` for the audit file at ``path``. A
    missing file reports zero entries in every bucket."""
    chain = read_chain(path)
    by_adapter: Counter[str] = Counter()
    by_tool_category: Counter[str] = Counter()
    by_result_class: Counter[str] = Counter()
    by_argument_size: Counter[str] = Counter()
    by_result_size: Counter[str] = Counter()
    for entry in chain:
        receipt = entry.receipt
        by_adapter[str(receipt.get("adapter"))] += 1
        by_tool_category[str(receipt.get("tool_category"))] += 1
        by_result_class[str(receipt.get("result_class"))] += 1
        by_argument_size[str(receipt.get("argument_size"))] += 1
        by_result_size[str(receipt.get("result_size"))] += 1
    return ObserveReport(
        path=path,
        entry_count=len(chain),
        by_adapter=dict(by_adapter),
        by_tool_category=dict(by_tool_category),
        by_result_class=dict(by_result_class),
        by_argument_size=dict(by_argument_size),
        by_result_size=dict(by_result_size),
    )
