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
from laconic.observe.audit import append_to_file
from laconic.replay.engine import CostCapExceededError, iter_turns

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


@dataclass(frozen=True, slots=True)
class CompletedSession:
    """One durable completion entry. It contains no transcript content or path."""

    session_id: str
    realized_cost_usd: float
    artifact_name: str

    def to_json(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "realized_cost_usd": self.realized_cost_usd,
            "artifact_name": self.artifact_name,
        }


@dataclass(frozen=True, slots=True)
class ChargedAttempt:
    """A partial replay whose modeled spend must constrain the next attempt."""

    session_id: str
    realized_cost_usd: float
    artifact_name: str

    def to_json(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "realized_cost_usd": self.realized_cost_usd,
            "artifact_name": self.artifact_name,
        }


class PartialSessionCostError(CostCapExceededError):
    """A capped partial replay whose already-charged cost is known exactly."""

    def __init__(self, *, realized_cost_usd: float, artifact_name: str) -> None:
        super().__init__(
            f"session cap exceeded after ${realized_cost_usd:.4f}; partial artifact {artifact_name}"
        )
        if realized_cost_usd < 0:
            raise ValueError("partial session cost must not be negative")
        if Path(artifact_name).name != artifact_name:
            raise ValueError("partial session artifact name must not contain a path")
        self.realized_cost_usd = realized_cost_usd
        self.artifact_name = artifact_name


class StageCLedger:
    """Mode-restricted, atomically persisted completion and partial-spend state."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._charges: list[ChargedAttempt] = []
        self._completed = self._load()

    @property
    def completed(self) -> dict[str, CompletedSession]:
        return dict(self._completed)

    @property
    def charges(self) -> tuple[ChargedAttempt, ...]:
        return tuple(self._charges)

    @property
    def realized_cost_usd(self) -> float:
        return sum(item.realized_cost_usd for item in self._completed.values()) + sum(
            item.realized_cost_usd for item in self._charges
        )

    def record_completed(self, completed: CompletedSession) -> None:
        if completed.session_id in self._completed:
            raise ValueError(f"session already completed: {completed.session_id}")
        self._completed[completed.session_id] = completed
        self._write()

    def record_charge(self, charge: ChargedAttempt) -> None:
        self._charges.append(charge)
        self._write()

    def _load(self) -> dict[str, CompletedSession]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StageCManifestError(
                f"cannot load Stage C ledger {self._path}: {error}"
            ) from error
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise StageCManifestError(f"Stage C ledger {self._path} has an unsupported schema")
        raw_completed = payload.get("completed")
        raw_charges = payload.get("charges", [])
        if not isinstance(raw_completed, list) or not isinstance(raw_charges, list):
            raise StageCManifestError(f"Stage C ledger {self._path} has malformed entries")
        completed: dict[str, CompletedSession] = {}
        for index, row in enumerate(raw_completed):
            if not isinstance(row, dict):
                raise StageCManifestError(f"Stage C ledger completion {index} is not an object")
            parsed = _parse_completed(row, index=index)
            if parsed.session_id in completed:
                raise StageCManifestError(
                    f"Stage C ledger {self._path} repeats {parsed.session_id!r}"
                )
            completed[parsed.session_id] = parsed
        charges: list[ChargedAttempt] = []
        for index, row in enumerate(raw_charges):
            if not isinstance(row, dict):
                raise StageCManifestError(f"Stage C ledger charge {index} is not an object")
            charge = _parse_charge(row, index=index)
            charges.append(charge)
        self._charges = charges
        return completed

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.parent.chmod(0o700)
        payload = {
            "version": 1,
            "completed": [
                completion.to_json() for _, completion in sorted(self._completed.items())
            ],
            "charges": [charge.to_json() for charge in self._charges],
        }
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            temporary.chmod(0o600)
            temporary.replace(self._path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _parse_completed(row: dict[object, object], *, index: int) -> CompletedSession:
    session_id = row.get("session_id")
    cost = row.get("realized_cost_usd")
    artifact_name = row.get("artifact_name")
    if not isinstance(session_id, str) or not isinstance(cost, (int, float)):
        raise StageCManifestError(f"Stage C ledger completion {index} has malformed identity/cost")
    if not isinstance(artifact_name, str) or Path(artifact_name).name != artifact_name:
        raise StageCManifestError(f"Stage C ledger completion {index} has unsafe artifact name")
    if cost < 0:
        raise StageCManifestError(f"Stage C ledger completion {index} has negative cost")
    return CompletedSession(
        session_id=session_id,
        realized_cost_usd=float(cost),
        artifact_name=artifact_name,
    )


def _parse_charge(row: dict[object, object], *, index: int) -> ChargedAttempt:
    completed = _parse_completed(row, index=index)
    return ChargedAttempt(
        session_id=completed.session_id,
        realized_cost_usd=completed.realized_cost_usd,
        artifact_name=completed.artifact_name,
    )


@dataclass(frozen=True, slots=True)
class SessionExecution:
    """The body-free summary a completed session returns to orchestration."""

    realized_cost_usd: float
    artifact_name: str
    turn_count: int
    induced_turn_count: int

    def __post_init__(self) -> None:
        if self.realized_cost_usd < 0:
            raise ValueError("session realized cost must not be negative")
        if Path(self.artifact_name).name != self.artifact_name:
            raise ValueError("session artifact name must not contain a path")
        if self.turn_count < 0 or self.induced_turn_count < 0:
            raise ValueError("session turn counts must not be negative")


class DurableSessionRunner(Protocol):
    """The completion-producing per-session boundary used by the local ledger."""

    def run(self, session: ResolvedStageCSession, *, cost_cap_usd: float) -> SessionExecution:
        """Run one session and return only its content-free result summary."""
        ...


@dataclass(frozen=True, slots=True)
class StageCAuditReceipt:
    """Allowlisted, content-free receipt for one Stage C orchestration outcome."""

    session_id: str
    set: str
    lineage_id: str
    provider: str
    model: str | None
    outcome: str
    realized_cost_usd: float
    turn_count: int
    induced_turn_count: int
    artifact_name: str | None
    error_class: str | None

    def to_json(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "set": self.set,
            "lineage_id": self.lineage_id,
            "provider": self.provider,
            "model": self.model,
            "outcome": self.outcome,
            "realized_cost_usd": self.realized_cost_usd,
            "turn_count": self.turn_count,
            "induced_turn_count": self.induced_turn_count,
            "artifact_name": self.artifact_name,
            "error_class": self.error_class,
        }


class StageCAudit:
    """Hash-chained local audit wrapper with a closed, content-free schema."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, receipt: StageCAuditReceipt) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.parent.chmod(0o700)
        append_to_file(self._path, receipt.to_json())
        self._path.chmod(0o600)


def run_resumable_batch(
    entries: Sequence[StageCManifestEntry],
    *,
    spend_cap_usd: float,
    runner: DurableSessionRunner,
    ledger: StageCLedger,
    audit: StageCAudit,
    resolver: Callable[[Provider, str], Path | None] = resolve_session_path,
    model_resolver: Callable[[Path], str] = resolve_original_model,
    after_completion: Callable[[CompletedSession], None] | None = None,
) -> tuple[BatchSessionResult, ...]:
    """Run a batch with durable completion before advancing to the next session.

    A ``KeyboardInterrupt`` from ``after_completion`` deliberately propagates:
    tests use it to model process death after the first completed session has
    reached persistent state. On restart that ID is skipped before resolution
    or client construction, preventing a second completed-session spend.
    """
    spend = BatchSpend(cap_usd=spend_cap_usd, realized_usd=ledger.realized_cost_usd)
    results: list[BatchSessionResult] = []
    for entry in entries:
        existing = ledger.completed.get(entry.session_id)
        if existing is not None:
            results.append(
                BatchSessionResult(
                    session_id=entry.session_id,
                    outcome="skipped_completed",
                    realized_cost_usd=existing.realized_cost_usd,
                )
            )
            continue
        if spend.remaining_usd <= 0:
            results.append(
                BatchSessionResult(session_id=entry.session_id, outcome="batch_cap_exceeded")
            )
            break
        baseline = resolver(entry.provider, entry.session_id)
        if baseline is None:
            _audit_outcome(audit, entry, outcome="resolve_failed")
            results.append(
                BatchSessionResult(session_id=entry.session_id, outcome="resolve_failed")
            )
            continue
        try:
            model = model_resolver(baseline)
        except OriginalModelError as error:
            _audit_outcome(audit, entry, outcome="model_unresolved", error=error)
            results.append(
                BatchSessionResult(session_id=entry.session_id, outcome="model_unresolved")
            )
            continue
        resolved = ResolvedStageCSession(entry=entry, baseline=baseline, model=model)
        try:
            execution = runner.run(resolved, cost_cap_usd=spend.remaining_usd)
        except PartialSessionCostError as error:
            ledger.record_charge(
                ChargedAttempt(
                    session_id=entry.session_id,
                    realized_cost_usd=error.realized_cost_usd,
                    artifact_name=error.artifact_name,
                )
            )
            spend = spend.record(error.realized_cost_usd)
            _audit_outcome(
                audit,
                entry,
                model=model,
                outcome="cost_cap_exceeded",
                error=error,
                realized_cost_usd=error.realized_cost_usd,
                artifact_name=error.artifact_name,
            )
            results.append(
                BatchSessionResult(
                    session_id=entry.session_id,
                    outcome="cost_cap_exceeded",
                    realized_cost_usd=error.realized_cost_usd,
                )
            )
            break
        except CostCapExceededError as error:
            _audit_outcome(audit, entry, model=model, outcome="cost_cap_exceeded", error=error)
            results.append(
                BatchSessionResult(session_id=entry.session_id, outcome="cost_cap_exceeded")
            )
            break
        except Exception as error:
            _audit_outcome(audit, entry, model=model, outcome="client_error", error=error)
            results.append(BatchSessionResult(session_id=entry.session_id, outcome="client_error"))
            continue
        completed = CompletedSession(
            session_id=entry.session_id,
            realized_cost_usd=execution.realized_cost_usd,
            artifact_name=execution.artifact_name,
        )
        ledger.record_completed(completed)
        _audit_outcome(audit, entry, model=model, outcome="completed", execution=execution)
        if after_completion is not None:
            after_completion(completed)
        spend = spend.record(execution.realized_cost_usd)
        results.append(
            BatchSessionResult(
                session_id=entry.session_id,
                outcome="completed",
                realized_cost_usd=execution.realized_cost_usd,
            )
        )
        if spend.remaining_usd <= 0:
            break
    return tuple(results)


def _audit_outcome(
    audit: StageCAudit,
    entry: StageCManifestEntry,
    *,
    outcome: str,
    model: str | None = None,
    execution: SessionExecution | None = None,
    error: Exception | None = None,
    realized_cost_usd: float = 0.0,
    artifact_name: str | None = None,
) -> None:
    audit.append(
        StageCAuditReceipt(
            session_id=entry.session_id,
            set=entry.set.value,
            lineage_id=entry.project_lineage_id,
            provider=entry.provider.value,
            model=model,
            outcome=outcome,
            realized_cost_usd=(
                realized_cost_usd if execution is None else execution.realized_cost_usd
            ),
            turn_count=0 if execution is None else execution.turn_count,
            induced_turn_count=0 if execution is None else execution.induced_turn_count,
            artifact_name=artifact_name if execution is None else execution.artifact_name,
            error_class=None if error is None else type(error).__name__,
        )
    )
