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

from dataclasses import dataclass

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


_APPEND_ONLY_REASON = "append-only mode: compaction is opt-in and not enabled"


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

    Defaults, and for now is limited, to append-only: every decision
    declines, and none ever mutates an existing prefix entry in the ledger.
    ``docs/overview.md`` §3.3: "Append-only encoding is the default... new
    observations are compact, history is never rewritten, and the prompt
    cache is preserved perfectly."
    """

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def evaluate_compaction(
        self,
        turn: int,
        prefix_before: int,
        prefix_after: int,
        projected_turns: int | None,
    ) -> CompactionDecision:
        """Decide whether compacting ``prefix_before`` to ``prefix_after``
        would pay off. Always declines: append-only is the only policy this
        manager implements, and it never touches the ledger it was built
        against — declining is not something that needs a write to prove.
        """
        if prefix_before < 0 or prefix_after < 0:
            raise ValueError(
                f"prefix sizes must not be negative: {prefix_before=}, {prefix_after=}"
            )
        if turn < 0:
            raise ValueError(f"turn must not be negative: {turn}")
        breakeven = breakeven_turns(prefix_after, prefix_before - prefix_after)
        return CompactionDecision(
            turn=turn,
            prefix_before=prefix_before,
            prefix_after=prefix_after,
            breakeven_turns=breakeven,
            projected_turns=projected_turns,
            accepted=False,
            reason=_APPEND_ONLY_REASON,
        )
