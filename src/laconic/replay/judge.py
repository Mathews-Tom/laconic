"""Opt-in semantic equivalence judge: a model-backed fallback for
structural near-misses, sampled and off by default.

``docs/system-design.md`` §2.6: structural comparison
(:mod:`laconic.replay.equivalence`) "falls back to a model judge for
semantic near-misses, with the judge's verdicts sampled and hand-audited."
Its §5.2 names a ``judge_model`` setting but, per ``DEVELOPMENT_PLAN.md``
§2's ``> GAP:``, specifies no cost budget, sampling rate, or offline mode
-- this module is that resolution: the judge is off by default
(:attr:`JudgeConfig.enabled` defaults to ``False``), every sampled verdict
is recorded in :attr:`Judge.audits` rather than only trusted, and a hard
per-run budget caps how many judge calls one replay can make regardless
of how many structural divergences it finds.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from laconic.replay.engine import RecordedAction
from laconic.replay.equivalence import EquivalenceVerdict, StructuralComparison


class JudgeVerdict(StrEnum):
    """A semantic judge's own verdict, kept distinct from
    :class:`~laconic.replay.equivalence.EquivalenceVerdict` -- a judge
    verdict is never applied directly, only used to potentially overturn a
    structural one, and the two enums existing separately keeps that
    one-directional relationship visible at the type level."""

    EQUIVALENT = "equivalent"
    DIVERGENT = "divergent"


class JudgeClient(Protocol):
    """A real semantic-equivalence model client.

    No default ships, matching :class:`~laconic.replay.engine.ReplayClient`
    -- the judge is opt-in by design and this repository's dependencies
    stay minimal. A caller wires a concrete client, typically the same
    provider SDK wrapper ``~/.laconic/config.toml``'s ``judge_model``
    setting already names.
    """

    def judge(
        self, *, recorded: RecordedAction, proposed: RecordedAction, model: str
    ) -> tuple[JudgeVerdict, str]:
        """Return a verdict and the model's stated reasoning for it."""
        ...


class JudgeConfigError(ValueError):
    """Raised when :class:`JudgeConfig` is constructed with ``enabled=True``
    but any of ``model``, ``sample_rate``, ``budget``, or ``client`` is
    missing or out of its required range."""


@dataclass(frozen=True, slots=True)
class JudgeConfig:
    """Explicit opt-in for the semantic judge.

    Every field defaults to the off/zero position; the judge only ever
    runs when a caller sets all four deliberately, and ``__post_init__``
    refuses to construct an ``enabled=True`` config missing any one of
    them -- resolving the §2 ``> GAP:`` as a constructor invariant rather
    than a convention a caller could forget.
    """

    enabled: bool = False
    model: str = ""
    sample_rate: float = 0.0
    budget: int = 0
    client: JudgeClient | None = None

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        if not self.model:
            raise JudgeConfigError("judge is enabled but no model identifier is configured")
        if not 0.0 < self.sample_rate <= 1.0:
            raise JudgeConfigError(f"judge sample_rate must be in (0, 1], got {self.sample_rate}")
        if self.budget <= 0:
            raise JudgeConfigError(f"judge budget must be positive when enabled, got {self.budget}")
        if self.client is None:
            raise JudgeConfigError("judge is enabled but no client is configured")


@dataclass(frozen=True, slots=True)
class JudgeAudit:
    """One sampled judge call, recorded for the hand-audit
    ``docs/system-design.md`` §2.6 requires -- a judge verdict is never
    trusted silently, only ever alongside exactly what it saw and said."""

    recorded: RecordedAction
    proposed: RecordedAction
    verdict: JudgeVerdict
    reasoning: str


@dataclass(slots=True)
class Judge:
    """Applies :class:`JudgeConfig` to a stream of structurally-divergent
    comparisons: a seeded sample subset, capped at ``config.budget``
    calls, every one recorded in :attr:`audits` regardless of its verdict.
    """

    config: JudgeConfig
    seed: int = 0
    _rng: random.Random = field(init=False, repr=False)
    _spent: int = field(default=0, init=False)
    _audits: list[JudgeAudit] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    @property
    def audits(self) -> tuple[JudgeAudit, ...]:
        return tuple(self._audits)

    @property
    def calls_spent(self) -> int:
        return self._spent

    def review(
        self,
        comparison: StructuralComparison,
        *,
        recorded: RecordedAction,
        proposed: RecordedAction,
    ) -> StructuralComparison:
        """Return ``comparison`` unchanged unless the judge is enabled,
        this comparison is a structural divergence, the sample roll hits,
        and budget remains -- in every other case the structural verdict
        stands, since the judge is a fallback, never a replacement.

        A judge call that itself finds the pair divergent leaves
        ``comparison`` unchanged too; only an EQUIVALENT judge verdict
        overturns a structural DIVERGENT, and the overturn is recorded in
        the returned :class:`~laconic.replay.equivalence.StructuralComparison`'s
        ``reason`` alongside the judge's own reasoning.
        """
        if comparison.verdict is EquivalenceVerdict.EQUIVALENT:
            return comparison
        if not self.config.enabled or self._spent >= self.config.budget:
            return comparison
        if self._rng.random() >= self.config.sample_rate:
            return comparison
        client = self.config.client
        if client is None:
            raise JudgeConfigError("judge is enabled but no client is configured")
        self._spent += 1
        verdict, reasoning = client.judge(
            recorded=recorded, proposed=proposed, model=self.config.model
        )
        self._audits.append(
            JudgeAudit(recorded=recorded, proposed=proposed, verdict=verdict, reasoning=reasoning)
        )
        if verdict is JudgeVerdict.EQUIVALENT:
            return StructuralComparison(
                EquivalenceVerdict.EQUIVALENT,
                f"structural divergence overturned by judge: {reasoning}",
            )
        return comparison
