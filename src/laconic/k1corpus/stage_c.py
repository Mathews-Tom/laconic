"""K1 Stage C batch-orchestration primitives.

This module consumes the frozen Stage B session manifest. It never changes the
H-59 lineage split or H-62 manifest, and it never supplies a fallback model.
The concrete provider client remains outside the package boundary.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from laconic.k1corpus.resolve import resolve_session_path
from laconic.k1corpus.stage_a import Provider
from laconic.k1corpus.stage_b import ManifestSet
from laconic.replay.engine import iter_turns

#: Private root for all Stage C mutable state and derived artifacts.
DEFAULT_STAGE_C_ROOT = Path(".laconic/k1/stage_c")

#: H-63/Stage C design §10.4's fixed client-work lineage exclusion. The values
#: are opaque lineage IDs only; the frozen Stage B manifest remains unchanged.
RETAILOGISTS_EXCLUDED_LINEAGES = frozenset(
    {
        "lineage:14cef12ef4a0d177",
        "lineage:f4b15c673cb2729c",
        "lineage:8b7d5e9ed2179312",
        "lineage:d53dafef331ca0bb",
        "lineage:6c49463ef1e57fab",
        "lineage:0375b1c9efb0a784",
        "lineage:79c4569dfaf38f13",
    }
)


class StageCManifestError(ValueError):
    """The frozen Stage B manifest cannot be consumed safely."""


class OriginalModelError(ValueError):
    """A resolved baseline does not name exactly one usable original model."""


@dataclass(frozen=True, slots=True)
class StageCManifestEntry:
    """One selected, body-free manifest row after Stage C filtering."""

    set: ManifestSet
    provider: Provider
    session_id: str
    project_lineage_id: str


@dataclass(frozen=True, slots=True)
class LoadedStageCManifest:
    """Selected executable entries and body-free exclusion accounting."""

    entries: tuple[StageCManifestEntry, ...]
    excluded_retailogists: tuple[StageCManifestEntry, ...]


@dataclass(frozen=True, slots=True)
class ResolvedStageCSession:
    """A manifest entry whose opaque identity resolved to a local baseline."""

    entry: StageCManifestEntry
    baseline: Path
    model: str


@dataclass(frozen=True, slots=True)
class BatchSessionResult:
    """One attempted-or-accounted session outcome for the batch core."""

    session_id: str
    outcome: str
    realized_cost_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class BatchSpend:
    """Batch-level realized-cost accounting, separate from per-session caps."""

    cap_usd: float
    realized_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.cap_usd <= 0:
            raise ValueError(f"batch spend cap must be positive, got {self.cap_usd}")
        if self.realized_usd < 0:
            raise ValueError(f"realized spend must not be negative, got {self.realized_usd}")

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.realized_usd)

    @property
    def exceeded(self) -> bool:
        return self.realized_usd > self.cap_usd

    def record(self, realized_cost_usd: float) -> BatchSpend:
        if realized_cost_usd < 0:
            raise ValueError(f"realized cost must not be negative, got {realized_cost_usd}")
        return BatchSpend(cap_usd=self.cap_usd, realized_usd=self.realized_usd + realized_cost_usd)


class SessionRunner(Protocol):
    """The M1-backed per-session boundary, injectable for zero-spend tests."""

    def run(self, session: ResolvedStageCSession, *, cost_cap_usd: float) -> float:
        """Run exactly one session and return its realized modeled cost."""
        ...


def load_stage_c_manifest(path: Path, *, selected_set: ManifestSet) -> LoadedStageCManifest:
    """Load a frozen manifest and apply the fixed Retailogists exclusion first."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StageCManifestError(f"cannot load session manifest {path}: {error}") from error
    if not isinstance(payload, list):
        raise StageCManifestError(f"session manifest {path} must be a JSON list")

    selected: list[StageCManifestEntry] = []
    excluded: list[StageCManifestEntry] = []
    for index, row in enumerate(payload):
        entry = _parse_manifest_entry(row, index=index)
        if entry.set is not selected_set:
            continue
        if entry.project_lineage_id in RETAILOGISTS_EXCLUDED_LINEAGES:
            excluded.append(entry)
        else:
            selected.append(entry)
    if not selected:
        raise StageCManifestError(
            f"session manifest {path} has no eligible {selected_set.value!r} sessions"
        )
    return LoadedStageCManifest(entries=tuple(selected), excluded_retailogists=tuple(excluded))


def _parse_manifest_entry(row: object, *, index: int) -> StageCManifestEntry:
    if not isinstance(row, dict):
        raise StageCManifestError(f"session manifest row {index} is not an object")
    mapping = cast(dict[str, object], row)
    try:
        set_name = mapping["set"]
        provider_name = mapping["provider"]
        session_id = mapping["session_id"]
        lineage_id = mapping["project_lineage_id"]
    except KeyError as error:
        raise StageCManifestError(
            f"session manifest row {index} is missing {error.args[0]!r}"
        ) from error
    if not isinstance(set_name, str) or not isinstance(provider_name, str):
        raise StageCManifestError(f"session manifest row {index} has non-string set/provider")
    if not isinstance(session_id, str) or not isinstance(lineage_id, str):
        raise StageCManifestError(f"session manifest row {index} has non-string opaque identity")
    try:
        return StageCManifestEntry(
            set=ManifestSet(set_name),
            provider=Provider(provider_name),
            session_id=session_id,
            project_lineage_id=lineage_id,
        )
    except ValueError as error:
        raise StageCManifestError(
            f"session manifest row {index} has invalid set/provider"
        ) from error


def resolve_original_model(baseline: Path) -> str:
    """Return the sole usage-backed model in a baseline or refuse to guess."""
    models = {turn.usage.model for turn in iter_turns(baseline) if turn.usage is not None}
    models.discard("unknown")
    if len(models) != 1:
        description = "none" if not models else ", ".join(sorted(models))
        raise OriginalModelError(
            f"{baseline}: expected exactly one original model, found {description}"
        )
    return next(iter(models))


def resolve_session(
    entry: StageCManifestEntry,
    *,
    resolver: Callable[[Provider, str], Path | None] = resolve_session_path,
    model_resolver: Callable[[Path], str] = resolve_original_model,
) -> ResolvedStageCSession | None:
    """Resolve a selected entry. Missing paths are accounted, never guessed."""
    baseline = resolver(entry.provider, entry.session_id)
    if baseline is None:
        return None
    return ResolvedStageCSession(entry=entry, baseline=baseline, model=model_resolver(baseline))


def run_untracked_batch(
    entries: Sequence[StageCManifestEntry],
    *,
    spend_cap_usd: float,
    runner: SessionRunner,
    resolver: Callable[[Provider, str], Path | None] = resolve_session_path,
    model_resolver: Callable[[Path], str] = resolve_original_model,
) -> tuple[BatchSessionResult, ...]:
    """Run entries until a realized-cost overage stops the next session.

    This is the deterministic, state-free core. PR-2 adds durable completion
    state and audit hooks around it; neither a missing path nor an unresolved
    model reaches ``runner``.
    """
    spend = BatchSpend(cap_usd=spend_cap_usd)
    results: list[BatchSessionResult] = []
    for entry in entries:
        resolved_path = resolver(entry.provider, entry.session_id)
        if resolved_path is None:
            results.append(
                BatchSessionResult(session_id=entry.session_id, outcome="resolve_failed")
            )
            continue
        try:
            model = model_resolver(resolved_path)
        except OriginalModelError:
            results.append(
                BatchSessionResult(session_id=entry.session_id, outcome="model_unresolved")
            )
            continue
        session = ResolvedStageCSession(entry=entry, baseline=resolved_path, model=model)
        realized_cost = runner.run(session, cost_cap_usd=spend.remaining_usd)
        spend = spend.record(realized_cost)
        results.append(
            BatchSessionResult(
                session_id=entry.session_id,
                outcome="completed",
                realized_cost_usd=realized_cost,
            )
        )
        if spend.exceeded:
            results.append(
                BatchSessionResult(session_id=entry.session_id, outcome="batch_cap_exceeded")
            )
            break
    return tuple(results)
