"""K1 Stage B — session-level manifest construction from H-59's frozen
lineage-level corpus design.

Governed by `.docs/K1_REPRESENTATIVE_CORPUS_PROTOCOL.md` § Stage B and
`.docs/K1_STAGE_B_MANIFEST_DESIGN.md`. This module never recomputes
H-59's frozen design/confirmatory lineage split -- it only checks
membership -- and never reads a transcript body, prompt, tool result,
title, or other free-text field: every content read is the identical
bounded `cwd`-only scan `laconic.k1corpus.providers` already performs
for Stage A.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from laconic.k1corpus.providers import enumerate_provider_with_meta
from laconic.k1corpus.stage_a import AUTHORIZED_ROOTS, FileMeta, Provider, SessionRecord, SourceRoot

#: Default location of H-59's frozen lineage-level decision.
DEFAULT_CORPUS_MANIFEST_PATH = Path(".laconic/k1/stage_b/corpus_manifest.json")

#: Default output location for this module's session-level manifest.
DEFAULT_SESSION_MANIFEST_PATH = Path(".laconic/k1/stage_b/session_manifest.json")


class ManifestSet(StrEnum):
    """Which of H-59's two frozen lineage sets a session belongs to."""

    DESIGN = "design"
    CONFIRMATORY = "confirmatory"


@dataclass(frozen=True, slots=True)
class FrozenCorpus:
    """H-59's frozen lineage-level decision, loaded from
    `corpus_manifest.json`. Every field here is read-only input; this
    module never recomputes the seeded split that produced
    `design_set`/`confirmatory_set`."""

    frozen_at: float
    time_window_days: int
    excluded_size_bands: frozenset[str]
    per_lineage_cap: int
    design_set: frozenset[str]
    confirmatory_set: frozenset[str]
    totals: dict[str, int]

    @classmethod
    def load(cls, path: Path = DEFAULT_CORPUS_MANIFEST_PATH) -> FrozenCorpus:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rules = payload["rules"]
        return cls(
            frozen_at=payload["frozen_at"],
            time_window_days=rules["time_window_days"],
            excluded_size_bands=frozenset(rules["size_band_exclusion"]),
            per_lineage_cap=rules["per_lineage_session_cap"],
            design_set=frozenset(payload["design_set"]),
            confirmatory_set=frozenset(payload["confirmatory_set"]),
            totals=dict(payload["totals"]),
        )


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One session selected into the session-level manifest, tagged with
    which of H-59's two frozen sets it belongs to."""

    set: ManifestSet
    record: SessionRecord

    def to_json(self) -> dict[str, str]:
        payload = self.record.to_json()
        payload["set"] = self.set.value
        return payload


class TotalsMismatchError(RuntimeError):
    """Raised when the rebuilt session-level totals disagree with H-59's
    frozen totals. This module never silently adjusts or ignores a
    mismatch -- it means either the real files changed since the freeze
    or a defect exists, and either way requires human review before any
    manifest is emitted."""


def _admitted_with_meta(
    *,
    home: Path | None,
    roots: tuple[SourceRoot, ...],
    anchor: float,
) -> list[tuple[SessionRecord, FileMeta]]:
    admitted: list[tuple[SessionRecord, FileMeta]] = []
    for provider in (Provider.CLAUDE_CODE, Provider.CODEX, Provider.OMP):
        provider_admitted, _exclusions = enumerate_provider_with_meta(
            provider, home=home, now=anchor, roots=roots
        )
        admitted.extend(provider_admitted)
    return admitted


def build_session_manifest(
    frozen: FrozenCorpus,
    *,
    home: Path | None = None,
    roots: tuple[SourceRoot, ...] = AUTHORIZED_ROOTS,
    as_of: float | None = None,
) -> tuple[ManifestEntry, ...]:
    """Re-derive the specific sessions belonging to H-59's frozen design
    and confirmatory lineages, apply the frozen per-lineage cap
    (most-recent-first, ties broken by `session_id`), and validate the
    result against ``frozen.totals`` before returning anything.

    Every age/band computation anchors to ``as_of`` (defaulting to
    ``frozen.frozen_at``, never wall-clock time), so the result is
    reproducible regardless of when this function actually runs.

    Raises `TotalsMismatchError` instead of returning a manifest on any
    disagreement with H-59's recorded totals.
    """
    anchor = as_of if as_of is not None else frozen.frozen_at
    admitted = _admitted_with_meta(home=home, roots=roots, anchor=anchor)

    eligible = [
        (record, meta)
        for record, meta in admitted
        if record.size_band.value not in frozen.excluded_size_bands
    ]

    by_lineage: dict[str, list[tuple[SessionRecord, FileMeta]]] = {}
    for record, meta in eligible:
        by_lineage.setdefault(record.project_lineage_id, []).append((record, meta))

    entries: list[ManifestEntry] = []
    design_count = 0
    confirmatory_count = 0
    design_lineages_seen: set[str] = set()
    confirmatory_lineages_seen: set[str] = set()

    for lineage_id, pairs in by_lineage.items():
        if lineage_id in frozen.design_set:
            manifest_set = ManifestSet.DESIGN
        elif lineage_id in frozen.confirmatory_set:
            manifest_set = ManifestSet.CONFIRMATORY
        else:
            continue  # not part of either frozen set: excluded, never guessed

        # Most-recent-first (smallest age_seconds first); ties broken by
        # session_id for a fully deterministic ordering.
        ordered = sorted(pairs, key=lambda pair: (pair[1].age_seconds, pair[0].session_id))
        capped = ordered[: frozen.per_lineage_cap]

        for record, _meta in capped:
            entries.append(ManifestEntry(set=manifest_set, record=record))

        if manifest_set is ManifestSet.DESIGN:
            design_count += len(capped)
            design_lineages_seen.add(lineage_id)
        else:
            confirmatory_count += len(capped)
            confirmatory_lineages_seen.add(lineage_id)

    _validate_totals(
        frozen,
        eligible_lineage_count=len(by_lineage),
        eligible_session_count=len(eligible),
        design_lineage_count=len(design_lineages_seen),
        design_session_count=design_count,
        confirmatory_lineage_count=len(confirmatory_lineages_seen),
        confirmatory_session_count=confirmatory_count,
    )
    return tuple(entries)


def _validate_totals(
    frozen: FrozenCorpus,
    *,
    eligible_lineage_count: int,
    eligible_session_count: int,
    design_lineage_count: int,
    design_session_count: int,
    confirmatory_lineage_count: int,
    confirmatory_session_count: int,
) -> None:
    observed = {
        "eligible_lineages": eligible_lineage_count,
        "eligible_sessions_precap": eligible_session_count,
        "design_lineages": design_lineage_count,
        "design_sessions_postcap": design_session_count,
        "confirmatory_lineages": confirmatory_lineage_count,
        "confirmatory_sessions_postcap": confirmatory_session_count,
    }
    mismatches = {
        key: (frozen.totals.get(key), value)
        for key, value in observed.items()
        if frozen.totals.get(key) != value
    }
    if mismatches:
        detail = ", ".join(
            f"{key}: frozen={frozen_value!r} observed={observed_value!r}"
            for key, (frozen_value, observed_value) in sorted(mismatches.items())
        )
        raise TotalsMismatchError(
            f"session-level totals disagree with H-59's frozen totals: {detail}"
        )
