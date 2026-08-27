"""Stage A stop-condition evaluation and ledger serialization.

Governed by `.docs/K1_REPRESENTATIVE_CORPUS_PROTOCOL.md` § Stage A stop
conditions and `.docs/K1_STAGE_A_DESIGN.md` §§ 8-9. This module performs
no scan itself -- it consumes the `SessionRecord`s and exclusion
counters `laconic.k1corpus.providers.enumerate_provider` already
produced, and never writes a real source-root path to the ledger (only
the two opaque `root_a`/`root_b` labels).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from laconic.k1corpus.providers import enumerate_provider
from laconic.k1corpus.stage_a import (
    AUTHORIZED_ROOTS,
    ExclusionReason,
    Provider,
    SessionRecord,
    SourceRoot,
)

#: Default local, gitignored, mode-restricted output location, mirroring
#: the existing `.laconic/k1/` local-state convention.
DEFAULT_LEDGER_PATH = Path(".laconic/k1/stage_a/ledger.json")

#: The three providers H-53 authorizes. The intended deployment claim
#: this study would eventually support spans all three, so stop
#: condition 2 checks admitted coverage against this full set.
AUTHORIZED_PROVIDERS: tuple[Provider, ...] = (Provider.CLAUDE_CODE, Provider.CODEX, Provider.OMP)

#: Minimum distinct project lineages required to proceed (stop condition 1).
MINIMUM_DISTINCT_LINEAGES = 3


class Disposition(StrEnum):
    """Stage A's own recommendation -- never an authorization. A
    `PROCEED_TO_STAGE_B_REQUEST` disposition means no stop condition
    fired; it is not itself a Stage B authorization, which remains a
    separate, explicit owner decision."""

    PROCEED_TO_STAGE_B_REQUEST = "proceed_to_stage_b_request"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class StopConditionResult:
    """One Stage A stop-condition check and whether it fired."""

    name: str
    fired: bool
    detail: str


def evaluate_stop_conditions(records: list[SessionRecord]) -> tuple[StopConditionResult, ...]:
    """Evaluate the protocol's four Stage A stop conditions against
    ``records`` (already-admitted, closed, in-scope sessions only)."""
    lineages = {r.project_lineage_id for r in records}
    providers_seen = {r.provider for r in records}

    lineage_detail = (
        f"{len(lineages)} distinct project lineage(s) observed; "
        f"minimum {MINIMUM_DISTINCT_LINEAGES} required."
    )
    provider_detail = (
        f"{len(providers_seen)} of {len(AUTHORIZED_PROVIDERS)} "
        "authorized provider(s) contributed an admitted session."
    )
    feasibility_detail = f"{len(records)} closed, in-scope session(s) admitted."
    ambiguity_detail = (
        "Stage A performs no directory-name inference and admits a session only on an "
        "explicit, unambiguous cwd match; review the exclusion breakdown for any count "
        "that suggests a project's self-owned status could not be classified confidently."
    )

    return (
        StopConditionResult(
            name="distinct_lineage_count",
            fired=len(lineages) < MINIMUM_DISTINCT_LINEAGES,
            detail=lineage_detail,
        ),
        StopConditionResult(
            name="single_provider_surface",
            fired=len(providers_seen) <= 1,
            detail=provider_detail,
        ),
        StopConditionResult(
            name="no_closed_sessions",
            fired=len(records) == 0,
            detail=feasibility_detail,
        ),
        StopConditionResult(
            name="ambiguous_association",
            fired=False,
            detail=ambiguity_detail,
        ),
    )


def compute_disposition(conditions: tuple[StopConditionResult, ...]) -> Disposition:
    """`STOP` if any condition fired, else `PROCEED_TO_STAGE_B_REQUEST`."""
    if any(condition.fired for condition in conditions):
        return Disposition.STOP
    return Disposition.PROCEED_TO_STAGE_B_REQUEST


@dataclass(frozen=True, slots=True)
class StageAReport:
    """The complete Stage A scan result: admitted records, exclusion
    counts by provider and reason, stop-condition evaluation, and the
    resulting disposition."""

    scanned_at: float
    records: tuple[SessionRecord, ...]
    exclusions: dict[str, dict[str, int]]
    conditions: tuple[StopConditionResult, ...]
    disposition: Disposition

    def to_json(self) -> dict[str, object]:
        return {
            "scanned_at": self.scanned_at,
            "roots": [root.label for root in AUTHORIZED_ROOTS],
            "providers": [provider.value for provider in AUTHORIZED_PROVIDERS],
            "admitted_count": len(self.records),
            "records": [record.to_json() for record in self.records],
            "exclusions": self.exclusions,
            "stop_conditions": [
                {"name": c.name, "fired": c.fired, "detail": c.detail} for c in self.conditions
            ],
            "disposition": self.disposition.value,
        }


def build_report(
    scanned_at: float,
    per_provider: dict[Provider, tuple[list[SessionRecord], Counter[ExclusionReason]]],
) -> StageAReport:
    """Combine each provider's `enumerate_provider` result into one
    `StageAReport`."""
    all_records = [record for records, _ in per_provider.values() for record in records]
    exclusions_by_provider = {
        provider.value: {reason.value: count for reason, count in counter.items()}
        for provider, (_, counter) in per_provider.items()
    }
    conditions = evaluate_stop_conditions(all_records)
    return StageAReport(
        scanned_at=scanned_at,
        records=tuple(all_records),
        exclusions=exclusions_by_provider,
        conditions=conditions,
        disposition=compute_disposition(conditions),
    )


def scan_all_providers(
    *,
    now: float | None = None,
    home: Path | None = None,
    roots: tuple[SourceRoot, ...] = AUTHORIZED_ROOTS,
) -> StageAReport:
    """Run `enumerate_provider` for every authorized provider against
    the real local storage roots and build the combined report.

    ``home``/``roots`` exist only so tests can exercise the full
    pipeline against a synthetic filesystem; the CLI never overrides
    them, so production behavior always uses the real `Path.home()`
    and the two roots authorized in H-53.
    """
    scan_time = now if now is not None else time.time()
    per_provider = {
        provider: enumerate_provider(provider, home=home, now=scan_time, roots=roots)
        for provider in AUTHORIZED_PROVIDERS
    }
    return build_report(scan_time, per_provider)


def write_ledger(report: StageAReport, path: Path = DEFAULT_LEDGER_PATH) -> None:
    """Atomically write ``report`` to ``path`` under a mode-restricted
    directory (`0o700`) and file (`0o600`), mirroring
    `laconic.observe.installer`'s atomic temp-file-plus-rename pattern."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".stage_a_ledger_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report.to_json(), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
