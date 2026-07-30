"""Deterministic, lineage-safe K1 redesign and holdout splitting."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from typing import Final

from laconic.k1.manifest import Candidate, Manifest, ManifestError, Split

_REDESIGN: Final[Split] = "redesign"
_HOLDOUT: Final[Split] = "holdout"


class SplitError(ManifestError):
    """Raised when a candidate population cannot support a safe frozen split."""


@dataclass(frozen=True, slots=True)
class SplitPolicy:
    """The declared split ratio and deterministic assignment seed."""

    holdout_fraction: float = 0.2
    seed: str = "laconic-k1-v1"

    def __post_init__(self) -> None:
        if not 0 < self.holdout_fraction < 0.5:
            raise SplitError("holdout_fraction must be greater than 0 and less than 0.5")
        if not self.seed.strip():
            raise SplitError("split seed must not be empty")


def freeze_split(manifest: Manifest, policy: SplitPolicy = SplitPolicy()) -> Manifest:
    """Assign every unassigned candidate to a deterministic, leakage-safe split."""
    if any(candidate.split != "unassigned" for candidate in manifest.candidates):
        raise SplitError("freeze_split requires every candidate to have split='unassigned'")
    components = _lineage_components(manifest)
    _require_representable_strata(components)
    assignments = _assign_components(components, policy)
    frozen = Manifest(
        tuple(
            replace(candidate, split=assignments[candidate.candidate_id])
            for candidate in manifest.candidates
        )
    )
    validate_frozen_split(frozen)
    return frozen


def validate_frozen_split(manifest: Manifest) -> None:
    """Reject an unfrozen, leaking, or non-representational candidate split."""
    project_splits: dict[str, set[Split]] = defaultdict(set)
    lineage_splits: dict[str, set[Split]] = defaultdict(set)
    stratum_splits: dict[str, set[Split]] = defaultdict(set)
    for candidate in manifest.candidates:
        if candidate.split not in {_REDESIGN, _HOLDOUT}:
            raise SplitError(
                f"candidate {candidate.candidate_id} has unfrozen split {candidate.split!r}"
            )
        project_splits[candidate.project].add(candidate.split)
        lineage_splits[candidate.lineage].add(candidate.split)
        stratum_splits[candidate.selection_stratum].add(candidate.split)
    _require_co_located("project", project_splits)
    _require_co_located("lineage", lineage_splits)
    for stratum, splits in sorted(stratum_splits.items()):
        if splits != {_REDESIGN, _HOLDOUT}:
            raise SplitError(
                f"selection stratum {stratum!r} is not represented in both redesign and holdout"
            )


def _lineage_components(manifest: Manifest) -> tuple[tuple[Candidate, ...], ...]:
    candidates = {candidate.candidate_id: candidate for candidate in manifest.candidates}
    adjacency: dict[str, set[str]] = {candidate_id: set() for candidate_id in candidates}
    by_project: dict[str, list[str]] = defaultdict(list)
    by_lineage: dict[str, list[str]] = defaultdict(list)
    for candidate in manifest.candidates:
        by_project[candidate.project].append(candidate.candidate_id)
        by_lineage[candidate.lineage].append(candidate.candidate_id)
    for group in (*by_project.values(), *by_lineage.values()):
        first, *rest = group
        for candidate_id in rest:
            adjacency[first].add(candidate_id)
            adjacency[candidate_id].add(first)
    components: list[tuple[Candidate, ...]] = []
    unseen = set(candidates)
    while unseen:
        root = min(unseen)
        stack = [root]
        member_ids: list[str] = []
        while stack:
            candidate_id = stack.pop()
            if candidate_id not in unseen:
                continue
            unseen.remove(candidate_id)
            member_ids.append(candidate_id)
            stack.extend(sorted(adjacency[candidate_id], reverse=True))
        components.append(tuple(candidates[candidate_id] for candidate_id in sorted(member_ids)))
    return tuple(sorted(components, key=lambda component: component[0].candidate_id))


def _require_representable_strata(components: tuple[tuple[Candidate, ...], ...]) -> None:
    components_by_stratum: dict[str, int] = defaultdict(int)
    for component in components:
        for stratum in {candidate.selection_stratum for candidate in component}:
            components_by_stratum[stratum] += 1
    unsupported = sorted(
        stratum for stratum, component_count in components_by_stratum.items() if component_count < 2
    )
    if unsupported:
        raise SplitError(
            "cannot create a leakage-safe stratified split; fewer than two lineage components "
            f"for {', '.join(repr(stratum) for stratum in unsupported)}"
        )


def _assign_components(
    components: tuple[tuple[Candidate, ...], ...], policy: SplitPolicy
) -> dict[str, Split]:
    total_by_stratum: Counter[str] = Counter(
        candidate.selection_stratum for component in components for candidate in component
    )
    targets = {
        stratum: min(max(round(total * policy.holdout_fraction), 1), total - 1)
        for stratum, total in total_by_stratum.items()
    }
    remaining_components: Counter[str] = Counter(
        stratum
        for component in components
        for stratum in {candidate.selection_stratum for candidate in component}
    )
    holdout_by_stratum: Counter[str] = Counter()
    assignments: dict[str, Split] = {}
    ordered = sorted(components, key=lambda component: _component_key(component, policy.seed))
    for component in ordered:
        component_counts = Counter(candidate.selection_stratum for candidate in component)
        for stratum in component_counts:
            remaining_components[stratum] -= 1
        assign_holdout = _choose_holdout(
            component_counts,
            total_by_stratum,
            targets,
            remaining_components,
            holdout_by_stratum,
        )
        split = _HOLDOUT if assign_holdout else _REDESIGN
        if assign_holdout:
            holdout_by_stratum.update(component_counts)
        for candidate in component:
            assignments[candidate.candidate_id] = split
    return assignments


def _component_key(component: tuple[Candidate, ...], seed: str) -> bytes:
    material = "\x00".join((seed, *(candidate.candidate_id for candidate in component)))
    return hashlib.sha256(material.encode("utf-8")).digest()


def _choose_holdout(
    component_counts: Counter[str],
    totals: Counter[str],
    targets: dict[str, int],
    remaining_components: Counter[str],
    current_holdout: Counter[str],
) -> bool:
    must_holdout = any(
        remaining_components[stratum] == 0 and current_holdout[stratum] == 0
        for stratum in component_counts
    )
    must_redesign = any(
        current_holdout[stratum] + component_counts[stratum] >= totals[stratum]
        for stratum in component_counts
    )
    if must_holdout and must_redesign:
        raise SplitError("lineage components cannot satisfy both redesign and holdout coverage")
    if must_holdout:
        return True
    if must_redesign:
        return False
    current_distance = sum(
        (current_holdout[stratum] - targets[stratum]) ** 2 for stratum in component_counts
    )
    holdout_distance = sum(
        (current_holdout[stratum] + component_counts[stratum] - targets[stratum]) ** 2
        for stratum in component_counts
    )
    return holdout_distance < current_distance


def _require_co_located(label: str, split_sets: dict[str, set[Split]]) -> None:
    leaking = sorted(name for name, splits in split_sets.items() if len(splits) != 1)
    if leaking:
        raise SplitError(
            f"{label} crosses split boundary: {', '.join(repr(name) for name in leaking)}"
        )
