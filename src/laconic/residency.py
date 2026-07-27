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

from laconic.costs import CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER


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
