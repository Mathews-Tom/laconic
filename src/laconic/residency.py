"""Residency manager: what stays resident in the prompt prefix.

``docs/system-design.md`` §2.3 names the rationale: tool-result residency is
~38.1% of spend (``docs/overview.md`` §2.1), because a cached prefix is
re-billed on every turn it sits resident, whether or not it is still in
focus. The naive fix — rewriting history to shrink it — can cost more than
it saves, because a prompt cache is prefix-matched: a rewrite invalidates
everything after the change and forces a full cache write on the next turn.

Append-only encoding is therefore the default (``docs/overview.md`` §3.3):
new observations arrive compact, history is never rewritten, and the cache
stays perfectly intact. Compaction — accepting one cache write now to shrink
what is billed as a cache read on every later turn — is opt-in and gated on
whether the arithmetic actually pays off before it is ever offered as a
default (``docs/system-design.md`` §9.4).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from statistics import median

from laconic.costs import CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER
from laconic.ledger import Ledger


def breakeven_turns(prefix_after: int, delta: int) -> float:
    """Turns of continued session needed before compaction pays for itself.

    Compacting a prefix from ``prefix_after + delta`` down to
    ``prefix_after`` costs one cache write of ``prefix_after`` tokens, billed
    at ``CACHE_WRITE_MULTIPLIER``, and saves ``delta`` tokens of cache-read
    billing, at ``CACHE_READ_MULTIPLIER``, on every subsequent turn. Dividing
    the one-time cost by the per-turn saving gives the number of turns the
    session must continue for compaction to have been worth it — reduces to
    ``12.5 * prefix_after / delta`` at the published pricing
    (``docs/system-design.md`` §2.3).

    A non-positive ``delta`` means the rewrite would not shrink the prefix at
    all, so there is no saving to break even against: this returns infinity
    rather than a negative or zero-division result, and every caller's
    break-even comparison then declines automatically.
    """
    if delta <= 0:
        return float("inf")
    write_cost = CACHE_WRITE_MULTIPLIER * prefix_after
    saving_per_turn = CACHE_READ_MULTIPLIER * delta
    return write_cost / saving_per_turn


class ResidencyMode(StrEnum):
    """The residency policy a manager enforces.

    ``docs/system-design.md`` §5.2's ``[residency] mode`` config knob names
    exactly these two values.
    """

    APPEND_ONLY = "append_only"
    COMPACT = "compact"


_APPEND_ONLY_REASON = "append-only mode: compaction is opt-in and not enabled"
_NO_ESTIMATE_REASON = "no session-length estimate available"


def estimate_remaining_turns(current_turn: int, session_lengths: Sequence[int]) -> int | None:
    """Project how many turns the current session has left.

    Estimated from the running turn count and the observed distribution of
    session lengths in the local corpus (``docs/system-design.md`` §2.3):
    the median of ``session_lengths`` minus ``current_turn``, floored to an
    integer (a fractional median never rounds up into over-projecting the
    session). Returns ``None`` rather than a guess when there is no
    distribution to draw from, or the current turn has already reached the
    median session length — a compaction decision without a real estimate
    must decline, not assume the session will continue.
    """
    if current_turn < 0:
        raise ValueError(f"current_turn must not be negative: {current_turn}")
    if any(length <= 0 for length in session_lengths):
        raise ValueError(f"session lengths must be positive: {session_lengths!r}")
    if not session_lengths:
        return None
    remaining = median(session_lengths) - current_turn
    return int(remaining) if remaining > 0 else None


@dataclass(frozen=True, slots=True)
class CompactionDecision:
    """The verdict for one turn-boundary residency decision.

    Every decision this module reaches is one of these, whether accepted or
    declined: the arithmetic behind a compaction that ran, or one that was
    correctly turned down, must stay reconstructible later
    (``docs/system-design.md`` §9.4).
    """

    turn: int
    prefix_before: int
    prefix_after: int
    breakeven_turns: float
    projected_turns: int | None
    accepted: bool
    reason: str

    @property
    def delta(self) -> int:
        return self.prefix_before - self.prefix_after


class ResidencyManager:
    """Decides whether the resident prefix should be compacted.

    Defaults to ``ResidencyMode.APPEND_ONLY``, in which every decision
    declines unconditionally. In neither mode does this manager mutate an
    existing prefix entry in the ledger: accepting a compaction only
    records that it was justified. Actually applying one — marking the
    underlying observations non-resident and rewriting what a live session
    serves — is a separate, later action; ``DEVELOPMENT_PLAN.md`` §6 M7
    names it explicitly out of scope, owned by M12. Every decision this
    manager reaches, accepted or declined, is written to the ledger's
    ``compactions`` table before it is returned, so the audit trail covers
    every verdict rather than only the ones that ran.
    """

    def __init__(self, ledger: Ledger, mode: ResidencyMode = ResidencyMode.APPEND_ONLY) -> None:
        self._ledger = ledger
        self._mode = mode

    @property
    def mode(self) -> ResidencyMode:
        return self._mode

    def evaluate_compaction(
        self,
        turn: int,
        prefix_before: int,
        prefix_after: int,
        projected_turns: int | None,
    ) -> CompactionDecision:
        """Decide whether compacting ``prefix_before`` to ``prefix_after``
        would pay off, and permanently log the verdict.

        ``append_only`` mode declines unconditionally. ``compact`` mode
        declines whenever ``projected_turns`` is ``None`` — a decision
        without a session-length estimate must decline, never guess — and
        otherwise accepts only once the projected remaining turns clear
        ``breakeven_turns``.
        """
        decision = self._decide(turn, prefix_before, prefix_after, projected_turns)
        self._ledger.record_compaction(
            turn=decision.turn,
            prefix_before=decision.prefix_before,
            prefix_after=decision.prefix_after,
            breakeven_turns=decision.breakeven_turns,
            projected_turns=decision.projected_turns,
            accepted=decision.accepted,
            reason=decision.reason,
        )
        return decision

    def _decide(
        self,
        turn: int,
        prefix_before: int,
        prefix_after: int,
        projected_turns: int | None,
    ) -> CompactionDecision:
        if prefix_before < 0 or prefix_after < 0:
            raise ValueError(
                f"prefix sizes must not be negative: {prefix_before=}, {prefix_after=}"
            )
        if prefix_after > prefix_before:
            raise ValueError(
                "prefix_after must not exceed prefix_before — that is growth, not "
                f"compaction: {prefix_before=}, {prefix_after=}"
            )
        if turn < 0:
            raise ValueError(f"turn must not be negative: {turn}")
        delta = prefix_before - prefix_after
        breakeven = breakeven_turns(prefix_after, delta)
        accepted, reason = self._verdict(breakeven, projected_turns)
        return CompactionDecision(
            turn=turn,
            prefix_before=prefix_before,
            prefix_after=prefix_after,
            breakeven_turns=breakeven,
            projected_turns=projected_turns,
            accepted=accepted,
            reason=reason,
        )

    def _verdict(self, breakeven: float, projected_turns: int | None) -> tuple[bool, str]:
        if self._mode is ResidencyMode.APPEND_ONLY:
            return False, _APPEND_ONLY_REASON
        if projected_turns is None:
            return False, _NO_ESTIMATE_REASON
        # ``> 0`` excludes a degenerate zero-turn projection from "clearing"
        # a zero-cost break-even (a free rewrite still saves nothing over a
        # session that has no turns left to amortize it across).
        if projected_turns > 0 and projected_turns >= breakeven:
            return True, (
                f"projected {projected_turns} turns remaining clears the "
                f"{breakeven:.1f}-turn break-even"
            )
        return False, (
            f"projected {projected_turns} turns remaining is below the "
            f"{breakeven:.1f}-turn break-even"
        )
