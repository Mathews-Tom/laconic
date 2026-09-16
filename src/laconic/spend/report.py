"""Render local spend composition, with its limits attached to it.

Everything this module serializes is a count, a dollar figure, a percentage,
a model identifier, or a hash. No session id, path, repository name, prompt,
tool argument, tool result, or file content reaches an artifact, and
:mod:`laconic.spend.privacy` re-checks that independently before anything is
written.

The rendering is deterministic: the same corpus renders byte-identically
twice. There is no generation timestamp, deliberately -- a timestamp would
make the artifact impossible to diff and would be the only thing in it that
is not a measurement.

**This report contains no savings figure and cannot be turned into one.**
Every session it reads ran with the codec enabled. A savings number requires
a comparison against the same work done without it, and no such observation
exists anywhere in this data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from laconic import __version__
from laconic.costs import CostBreakdown, CostShares, ZeroCostError
from laconic.spend.join import Composition, SessionComposition, codec_activity

#: Bumped whenever a report field is added, removed, or reinterpreted.
REPORT_SCHEMA_VERSION: Final = 3

#: The default output directory, relative to the working directory. Git-ignored:
#: a spend report is local evidence about the owner's own work, not a repository
#: artifact, even though it carries no content.
DEFAULT_OUTPUT_DIR: Final = Path(".laconic/spend")

#: Closed provenance labels rendered by every evidence surface.
GENERATION_BASIS: Final = "local_read_only_aggregate_snapshot"
SOURCE_FRESHNESS: Final = "scanned_at_generation"
PRIVACY_STATUS: Final = "exact_key_validated"

#: The closed vocabulary of limitations. Every report carries all of them, in
#: this order. They are a fixed set rather than free text so that
#: `laconic.spend.privacy` can verify the report still says what it must:
#: a report that dropped its single-arm caveat would otherwise validate
#: cleanly and read like a savings result.
LIMITATIONS: Final = (
    "single_arm_corpus_every_session_ran_with_the_codec_enabled",
    "no_counterfactual_exists_so_no_savings_figure_can_be_derived",
    "character_reduction_is_not_token_reduction",
    "cost_is_modelled_from_token_counters_never_billed_by_a_provider",
    "cache_writes_are_priced_at_one_rate_although_lifetimes_bill_differently",
    "sessions_are_not_controlled_units_and_are_not_comparable",
    "a_ledger_only_proves_the_codec_ran_not_that_it_covered_the_session",
    "committed_k1_fixture_8_41_pct_still_bounds_general_savings_claims",
    "host_reported_cost_covers_only_hosts_that_report_one",
    "avoided_cost_is_modelled_from_assumptions_and_is_not_a_measurement",
    "avoided_cost_assumes_removed_text_would_have_been_cached_and_re_read",
)

#: How many characters of tool output a token carries. Code and structured
#: output tokenize denser than prose and this corpus is both, so the
#: estimate is reported as a range and never as a point.
CHARS_PER_TOKEN_BOUNDS: Final = (3.0, 4.0)

#: What fraction of the corpus-average cache re-read count to credit the
#: codec's removed tokens with, at the low and high ends of the band.
#:
#: The corpus average is ``cache_read / cache_write`` over every cached
#: token, and it is dominated by content written on the first turn -- the
#: system prompt and tool definitions -- which is then re-read on every
#: turn that follows. The codec removes tool *output*, which arrives later
#: in a session and is therefore re-read fewer times than that average.
#: Crediting removed tokens with the full corpus average would overstate
#: the estimate in one direction, and this term is roughly 85% of the
#: modelled per-token price, so the overstatement would dominate.
#:
#: The low end credits half the corpus average, which is what a tool result
#: arriving at a uniformly random point in a session would see. The high
#: end credits the full average, the most favourable reading. The truth is
#: between them and is not measured here, so both ends are carried into the
#: band rather than one being presented as exact.
REREAD_CREDIT_BOUNDS: Final = (0.5, 1.0)

#: Exactly the keys the avoided-cost estimate block may carry.
ALLOWED_ESTIMATE_KEYS: Final = frozenset(
    {
        "basis",
        "chars_avoided",
        "chars_per_token_high",
        "chars_per_token_low",
        "tokens_removed_low",
        "tokens_removed_high",
        "cache_reread_multiplier",
        "reread_credited_low",
        "reread_credited_high",
        "effective_cache_write_usd_per_token",
        "effective_cache_read_usd_per_token",
        "avoided_cost_usd_low",
        "avoided_cost_usd_high",
        "avoided_share_pct_low",
        "avoided_share_pct_high",
        "denominator_usd",
        "fallback_priced_cost_share_pct",
    }
)

#: The value of the estimate block's ``basis`` field. A constant, and
#: verified by the privacy gate, so an estimate can never be serialized
#: without the word that says it is not a measurement.
ESTIMATE_BASIS: Final = "modelled_not_measured"

#: Above this share of the estimate's own denominator coming from models
#: with no published list price, the dollar figures stop being quotable.
#: The per-token rates are divided out of that same cost, so the band is
#: then built on prices nobody published. The *share* survives, because the
#: same error sits in numerator and denominator and largely cancels.
FALLBACK_SHARE_QUOTABLE_MAX_PCT: Final = 25.0

#: Exactly the keys a serialized report may carry.
ALLOWED_REPORT_KEYS: Final = frozenset(
    {
        "schema_version",
        "laconic_version",
        "generation_basis",
        "source_freshness",
        "privacy_status",
        "sessions_with_spend",
        "root_sessions",
        "nested_sessions",
        "sessions_without_priced_turns",
        "priced_sessions_without_host_cost",
        "priced_turns",
        "matched_sessions",
        "unmatched_spend_sessions",
        "unmatched_ledger_sessions",
        "damaged_ledgers",
        "corpus_tokens",
        "corpus_cost",
        "corpus_shares",
        "corpus_host_cost_usd",
        # The modelled figure `corpus_host_cost_usd` is actually comparable
        # with: the same sessions, priced two ways. `corpus_cost` covers
        # sessions the host total structurally cannot.
        "host_reporting_cost",
        "matched_tokens",
        "matched_cost",
        "matched_shares",
        "matched_host_cost_usd",
        "matched_sessions_without_host_cost",
        "codec",
        "codec_active_sessions_without_priced_turns",
        "unpriced_models",
        "fallback_priced_cost_share_pct",
        "unknown_usage_keys",
        "sessions",
        "estimate",
        "limitations",
    }
)

#: Exactly the keys each per-session entry may carry.
ALLOWED_SESSION_KEYS: Final = frozenset(
    {
        "session_hash",
        "nested",
        "turns",
        "tokens",
        "modelled_cost_usd",
        "host_cost_usd",
        "eligible",
        "emitted",
        "raw_chars",
        "visible_chars",
        "chars_avoided",
        "full_expansions",
        "span_expansions",
    }
)

#: Exactly the keys a token block may carry.
ALLOWED_TOKEN_KEYS: Final = frozenset({"uncached_input", "cache_read", "cache_write", "output"})

#: Exactly the keys a cost or share block may carry.
ALLOWED_COST_KEYS: Final = frozenset(
    {"uncached_input", "cache_read", "cache_write", "output", "total"}
)

#: Exactly the keys the codec block may carry.
ALLOWED_CODEC_KEYS: Final = frozenset(
    {
        "sessions",
        "eligible",
        "emitted",
        "pass_through",
        "raw_chars",
        "visible_chars",
        "chars_avoided",
        "full_expansions",
        "span_expansions",
    }
)


def session_hash(session_id: str) -> str:
    """Return the stable, content-free identifier a report uses for a session.

    A real session id names a live file in the owner's home directory and is
    never serialized. The digest keeps rows distinguishable and stable across
    runs without being a locator.
    """
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _tokens(sessions: tuple[SessionComposition, ...], composition: Composition) -> dict[str, int]:
    counters = composition.usage(sessions)
    return {
        "uncached_input": sum(model.input_tokens for model in counters.values()),
        "cache_read": sum(model.cache_read for model in counters.values()),
        "cache_write": sum(model.cache_write for model in counters.values()),
        "output": sum(model.output_tokens for model in counters.values()),
    }


def _cost(breakdown: CostBreakdown) -> dict[str, float]:
    return {
        "uncached_input": breakdown.uncached_input,
        "cache_read": breakdown.cache_read,
        "cache_write": breakdown.cache_write,
        "output": breakdown.output,
        "total": breakdown.total,
    }


def _shares(breakdown: CostBreakdown) -> dict[str, float] | None:
    """Return the percentage split, or ``None`` when there is nothing to split.

    :meth:`laconic.costs.CostBreakdown.shares` raises rather than reporting
    0.00% four times over an empty corpus, which would hide the emptiness.
    A report over no spend says so by carrying no shares at all.
    """
    try:
        split: CostShares = breakdown.shares()
    except ZeroCostError:
        return None
    return {
        "uncached_input": split.uncached_input,
        "cache_read": split.cache_read,
        "cache_write": split.cache_write,
        "output": split.output,
        "total": split.total,
    }


def _session_entry(session: SessionComposition) -> dict[str, Any]:
    decisions = session.decisions
    assert decisions is not None  # only matched sessions are serialized
    return {
        "session_hash": session_hash(session.session_id),
        "nested": session.nested,
        "turns": session.turns,
        "tokens": {
            "uncached_input": sum(model.input_tokens for model in session.usage.values()),
            "cache_read": sum(model.cache_read for model in session.usage.values()),
            "cache_write": sum(model.cache_write for model in session.usage.values()),
            "output": sum(model.output_tokens for model in session.usage.values()),
        },
        "modelled_cost_usd": session.modelled_cost.total,
        "host_cost_usd": session.host_cost_usd,
        "eligible": decisions.eligible,
        "emitted": decisions.emitted,
        "raw_chars": decisions.raw_chars,
        "visible_chars": decisions.visible_chars,
        "chars_avoided": decisions.chars_avoided,
        "full_expansions": decisions.full_expansions,
        "span_expansions": decisions.span_expansions,
    }


@dataclass(frozen=True, slots=True)
class SpendReport:
    """A rendered composition report and the payload it was rendered from."""

    payload: dict[str, Any]

    def to_json(self) -> str:
        """Serialize deterministically: sorted keys, stable separators.

        ``allow_nan=False`` because Python's default emits a bare ``NaN`` or
        ``Infinity`` token, which is not JSON and which no strict downstream
        parser accepts. A non-finite value should never reach here -- the
        loader and the privacy gate both refuse one -- so this raises rather
        than writing an unparseable artifact.
        """
        return json.dumps(self.payload, indent=2, sort_keys=True, allow_nan=False) + "\n"

    @property
    def sha256(self) -> str:
        """Digest the canonical JSON semantic source used by derived renderers."""
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


def _estimate(
    tokens: dict[str, int],
    cost: dict[str, float],
    chars_avoided: int,
    fallback_share_pct: float,
) -> dict[str, Any] | None:
    """Model the provider cost the codec's removed characters avoided.

    Returns ``None`` when the corpus cannot support the model rather than
    returning zeros, because a zero here would read as "the codec saved
    nothing" when the truth is "this corpus cannot say".

    The model is deliberately self-calibrating: the per-token prices and
    the re-read multiplier are divided out of this corpus's own measured
    tokens and cost, not taken from a price table or a remembered
    constant. What remains unmeasured is characters per
    token and how much of the corpus's re-read rate those tokens would
    really have seen. Both are carried into the band.

    The mechanism it prices is the one that makes tool-boundary removal
    worth more than its character count suggests. Text removed before it
    enters the context is written to the prompt cache once and then never
    re-read, and this corpus re-reads each cached token
    ``cache_read / cache_write`` times on average. Removing a character
    once therefore avoids one write plus that many reads.
    """
    cache_write_tokens = tokens["cache_write"]
    cache_read_tokens = tokens["cache_read"]
    denominator = cost["total"]
    if chars_avoided <= 0 or cache_write_tokens <= 0 or denominator <= 0:
        return None
    write_rate = cost["cache_write"] / cache_write_tokens
    read_rate = cost["cache_read"] / cache_read_tokens if cache_read_tokens else 0.0
    multiplier = cache_read_tokens / cache_write_tokens
    low_credit, high_credit = REREAD_CREDIT_BOUNDS
    credited_low = multiplier * low_credit
    credited_high = multiplier * high_credit
    low_chars, high_chars = CHARS_PER_TOKEN_BOUNDS
    # Denser tokens mean fewer of them, so the *upper* characters-per-token
    # bound produces the *lower* cost. Both uncertainties are carried into
    # the same band: holding the re-read term exact while banding only the
    # token count would imply a precision the dominant input does not have.
    tokens_high = chars_avoided / low_chars
    tokens_low = chars_avoided / high_chars
    cost_low = tokens_low * (write_rate + credited_low * read_rate)
    cost_high = tokens_high * (write_rate + credited_high * read_rate)
    return {
        "basis": ESTIMATE_BASIS,
        "chars_avoided": chars_avoided,
        "chars_per_token_low": low_chars,
        "chars_per_token_high": high_chars,
        "tokens_removed_low": round(tokens_low, 6),
        "tokens_removed_high": round(tokens_high, 6),
        "cache_reread_multiplier": round(multiplier, 6),
        "reread_credited_low": round(credited_low, 6),
        "reread_credited_high": round(credited_high, 6),
        "effective_cache_write_usd_per_token": round(write_rate, 12),
        "effective_cache_read_usd_per_token": round(read_rate, 12),
        "avoided_cost_usd_low": round(cost_low, 6),
        "avoided_cost_usd_high": round(cost_high, 6),
        "avoided_share_pct_low": round(100.0 * cost_low / denominator, 6),
        "avoided_share_pct_high": round(100.0 * cost_high / denominator, 6),
        "denominator_usd": round(denominator, 6),
        "fallback_priced_cost_share_pct": round(fallback_share_pct, 6),
    }


def build_report(composition: Composition) -> SpendReport:
    """Turn a join into the content-free payload a report serializes."""
    matched = composition.matched
    priced = composition.priced
    host_reporting = composition.host_reporting
    activity = codec_activity(composition)
    matched_tokens = _tokens(matched, composition)
    matched_cost = _cost(composition.modelled_cost(matched))
    payload: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "laconic_version": __version__,
        "generation_basis": GENERATION_BASIS,
        "source_freshness": SOURCE_FRESHNESS,
        "privacy_status": PRIVACY_STATUS,
        "sessions_with_spend": len(priced),
        "root_sessions": sum(1 for entry in priced if not entry.nested),
        "nested_sessions": sum(1 for entry in priced if entry.nested),
        "sessions_without_priced_turns": composition.sessions_without_priced_turns,
        # Claude Code reports token counters but no per-turn cost, so the
        # host-reported total covers only part of the corpus. Counting the
        # gap keeps a partial sum from reading as a whole one.
        "priced_sessions_without_host_cost": sum(
            1 for entry in priced if not entry.reports_host_cost
        ),
        "codec_active_sessions_without_priced_turns": sum(
            1 for entry in matched if not entry.turns
        ),
        "priced_turns": sum(entry.turns for entry in priced),
        "matched_sessions": len(matched),
        "unmatched_spend_sessions": len(composition.unmatched_spend_sessions),
        "unmatched_ledger_sessions": len(composition.unmatched_ledger_sessions),
        "damaged_ledgers": composition.damaged_ledgers,
        "corpus_tokens": _tokens(composition.sessions, composition),
        "corpus_cost": _cost(composition.modelled_cost()),
        "corpus_shares": _shares(composition.modelled_cost()),
        "corpus_host_cost_usd": composition.host_cost_usd(),
        # `corpus_cost` and `corpus_host_cost_usd` do not cover the same
        # sessions, so their difference is not a pricing disagreement. This
        # is the modelled total for exactly the sessions the host priced,
        # and it is the only one of the two that `corpus_host_cost_usd` can
        # be read against.
        "host_reporting_cost": _cost(composition.modelled_cost(host_reporting)),
        "matched_tokens": matched_tokens,
        "matched_cost": matched_cost,
        "matched_shares": _shares(composition.modelled_cost(matched)),
        "matched_host_cost_usd": composition.host_cost_usd(matched),
        # Zero on this corpus, and not guaranteed to stay zero: the codec
        # installs into Claude Code too. Non-zero means `matched_cost` and
        # `matched_host_cost_usd` have stopped covering the same sessions.
        "matched_sessions_without_host_cost": sum(
            1 for entry in matched if entry.turns and not entry.reports_host_cost
        ),
        "codec": {
            "sessions": activity.sessions,
            "eligible": activity.eligible,
            "emitted": activity.emitted,
            "pass_through": activity.pass_through,
            "raw_chars": activity.raw_chars,
            "visible_chars": activity.visible_chars,
            "chars_avoided": activity.chars_avoided,
            "full_expansions": activity.full_expansions,
            "span_expansions": activity.span_expansions,
        },
        "unpriced_models": composition.unpriced_models,
        "unknown_usage_keys": sorted(composition.unknown_usage_keys),
        "sessions": [
            _session_entry(session)
            for session in sorted(matched, key=lambda entry: session_hash(entry.session_id))
        ],
        "limitations": list(LIMITATIONS),
        # Always present, `None` when the corpus cannot support the model.
        # An absent key would make the schema optional; a zero would read as
        # "the codec saved nothing" rather than "this corpus cannot say".
        "fallback_priced_cost_share_pct": round(
            100.0 * composition.fallback_priced_cost_share(), 6
        ),
        "estimate": _estimate(
            matched_tokens,
            matched_cost,
            activity.chars_avoided,
            100.0 * composition.fallback_priced_cost_share(matched),
        ),
    }
    return SpendReport(payload=payload)


_LIMITATION_PROSE: Final = {
    "host_reported_cost_covers_only_hosts_that_report_one": (
        "The host-reported total covers only sessions whose host records a "
        "per-turn cost. Claude Code records token counters but no cost, so its "
        "sessions contribute tokens and modelled cost while contributing nothing "
        "to that total."
    ),
    "avoided_cost_is_modelled_from_assumptions_and_is_not_a_measurement": (
        "The avoided-cost estimate is a model, not a measurement. It rests on "
        "unmeasured assumptions -- characters per token, how much of the corpus's "
        "cache re-read rate removed tokens would really have seen, and that the "
        "removed text would have been cached at all -- and is reported as a band "
        "because of them. No session was ever run without the codec to check it "
        "against."
    ),
    "avoided_cost_assumes_removed_text_would_have_been_cached_and_re_read": (
        "The estimate assumes removed text would have been written to the prompt "
        "cache once and re-read at the corpus's own measured rate. Text removed from "
        "a turn that was never followed by another turn would have been re-read "
        "fewer times, and is overcounted by that assumption."
    ),
    "single_arm_corpus_every_session_ran_with_the_codec_enabled": (
        "Single-arm corpus. Every session measured here ran with the codec enabled."
    ),
    "no_counterfactual_exists_so_no_savings_figure_can_be_derived": (
        "No counterfactual exists in this data. Nothing here is a savings figure, "
        "and no arithmetic over these numbers can produce one."
    ),
    "character_reduction_is_not_token_reduction": (
        "Characters avoided is a character count. It is not tokens, and the "
        "relationship between the two is not measured here."
    ),
    "cost_is_modelled_from_token_counters_never_billed_by_a_provider": (
        "Both dollar figures are modelled from token counters. Providers return "
        "counters, not prices: the host figure is OMP's own price table, the "
        "Laconic figure is laconic.costs. Neither is a bill."
    ),
    "cache_writes_are_priced_at_one_rate_although_lifetimes_bill_differently": (
        "Every cache write is priced at one rate per model. Anthropic bills a "
        "one-hour cache write at twice the input price where a five-minute write "
        "bills at 1.25x, and OMP's transcripts mark which is which: on the "
        "development corpus 65.4% of cache-write tokens are one-hour. This "
        "under-prices, never over-prices, so it can only make a modelled figure "
        "smaller than the provider's. It is not corrected here because the host's "
        "own accounting cannot adjudicate it -- OMP charged the higher rate on "
        "only 14.7% of the tokens it had itself marked one-hour, so matching the "
        "host would mean pricing 85% of them against its own lower figure."
    ),
    "sessions_are_not_controlled_units_and_are_not_comparable": (
        "A session is not a controlled unit. Sessions differ in length, "
        "repository, task, and model, so per-session figures do not compare."
    ),
    "a_ledger_only_proves_the_codec_ran_not_that_it_covered_the_session": (
        "A matched session is one that has a ledger, not one the codec covered "
        "end to end. A long session that the extension only joined partway "
        "through contributes all of its spend and almost none of its codec "
        "activity; compare each row's turns against its eligible count."
    ),
    "committed_k1_fixture_8_41_pct_still_bounds_general_savings_claims": (
        "The committed K1 fixture's 8.41% remains the only bound on a general "
        "savings claim. This report does not move it."
    ),
}


def limitation_prose(name: str) -> str:
    """Return the fixed human-readable text for a closed limitation identifier."""
    return _LIMITATION_PROSE[name]


def _usd(value: float) -> str:
    return f"${value:,.2f}"


def _pct(value: float) -> str:
    return f"{value:.2f}%"


def _sessions(count: int) -> str:
    """Render a session count with a noun that agrees with it."""
    return f"{count} priced session" if count == 1 else f"{count} priced sessions"


def _split_lines(title: str, cost: dict[str, float], shares: dict[str, float] | None) -> list[str]:
    lines = [f"### {title}", "", "| Component | USD | Share |", "| --- | ---: | ---: |"]
    for key, label in (
        ("uncached_input", "Uncached input"),
        ("cache_read", "Cache read"),
        ("cache_write", "Cache write"),
        ("output", "Output"),
    ):
        share = "n/a" if shares is None else _pct(shares[key])
        lines.append(f"| {label} | {_usd(cost[key])} | {share} |")
    lines.append(f"| **Total** | **{_usd(cost['total'])}** | |")
    lines.append("")
    return lines


def _estimate_lines(estimate: dict[str, Any] | None, host_cost: float) -> list[str]:
    """Render the avoided-cost band, or say plainly why there is none."""
    if estimate is None:
        return [
            "",
            "## Modelled cost avoided",
            "",
            "Not estimated: this corpus has no removed characters or no cached "
            "tokens to price them against. That is not a zero; it is a corpus "
            "that cannot answer the question.",
            "",
        ]
    share = float(estimate["fallback_priced_cost_share_pct"])
    quotable = share <= FALLBACK_SHARE_QUOTABLE_MAX_PCT
    lines = [
        "",
        "## Modelled cost avoided",
        "",
        "**A model, not a measurement.** No session ran without the codec, so "
        "this is what the removed characters *would have* cost, not an observed "
        "saving. It is a band because more than one input is assumed.",
        "",
    ]
    if not quotable:
        lines += [
            f"> **Do not quote the dollar figures.** {share:.1f}% of the cost these "
            "are derived from comes from models with no published list price, "
            "billed at a fallback rate. The per-token prices below are divided out "
            "of that same cost, so the dollars inherit the error. The *share* is "
            "far more robust, because the same error sits in both the numerator "
            "and the denominator and largely cancels. Run "
            "`laconic research spend report` and check the unpriced-model list "
            "below.",
            "",
        ]
    return lines + [
        f"- **{_usd(estimate['avoided_cost_usd_low'])} to "
        f"{_usd(estimate['avoided_cost_usd_high'])}** avoided, against a modelled "
        f"{_usd(estimate['denominator_usd'])} for the same sessions",
        f"- **{_pct(estimate['avoided_share_pct_low'])} to "
        f"{_pct(estimate['avoided_share_pct_high'])}** of that bill",
        "",
        "How it is derived:",
        "",
        f"- Measured: {estimate['chars_avoided']:,} characters never entered the context.",
        f"- Assumed: {estimate['chars_per_token_low']:g} to "
        f"{estimate['chars_per_token_high']:g} characters per token, giving "
        f"{estimate['tokens_removed_low']:,.0f} to "
        f"{estimate['tokens_removed_high']:,.0f} tokens.",
        f"- Measured: each cached token in this corpus was re-read "
        f"{estimate['cache_reread_multiplier']:,.1f} times. Text removed at the "
        "tool boundary is written to the cache once and re-read never, so removal "
        "compounds across a session instead of saving once.",
        f"- Assumed: removed tokens see {estimate['reread_credited_low']:,.1f} to "
        f"{estimate['reread_credited_high']:,.1f} of those re-reads. The corpus "
        "average is dominated by first-turn content re-read every turn; tool "
        "output arrives later and is re-read less, so the low end credits what a "
        "result arriving at a random point in a session would see. This term is "
        "most of the price, which is why it is banded rather than held exact.",
        "- Assumed: the removed text would have been cached at all, and priced at "
        "this corpus's single modelled cache-write rate. A provider's minimum "
        "cacheable size and its separate cache lifetimes are not modelled.",
        "- Measured: the per-token cache-write and cache-read prices are divided "
        "out of this corpus's own cost and tokens, not taken from a price table. "
        "That makes the rates a blend of whatever models this corpus actually "
        "used, weighted by how much each was used.",
        "- Inherited: those per-token costs come from `laconic.costs`, which prices "
        "a model from a published list price and falls back to Sonnet rates for "
        "any model it does not know. A corpus with unpriced models (listed below, "
        "when present) carries that error into both the estimate and its "
        "denominator, so the *share* is more robust than the dollar figure.",
        "",
        "The denominator is the modelled cost of the same sessions, so both sides "
        "of the percentage come from one pricing model. Comparing a modelled "
        "saving against a host-reported bill would mix two, and the two do not "
        "agree: this corpus's host-reported total for the same sessions is "
        f"{_usd(host_cost)}, against the modelled "
        f"{_usd(estimate['denominator_usd'])}. Read the share, not the dollars.",
        "",
    ]


def render_markdown(report: SpendReport) -> str:
    """Render the report deterministically as Markdown."""
    payload = report.payload
    codec = payload["codec"]
    lines: list[str] = [
        "# Local spend composition",
        "",
        "Composition of the owner's own model spend, joined to what the runtime",
        "codec did in those same sessions. **This report makes no savings claim",
        "and contains no savings figure.**",
        "",
        f"Generated by laconic {payload['laconic_version']}, report schema "
        f"v{payload['schema_version']}. Canonical JSON SHA-256: `{report.sha256}`.",
        f"Basis: `{payload['generation_basis']}`. Freshness: "
        f"`{payload['source_freshness']}`. Privacy: `{payload['privacy_status']}`.",
        "",
        "## Limitations",
        "",
    ]
    lines += [f"- {limitation_prose(name)}" for name in payload["limitations"]]
    lines += [
        "",
        "## Corpus",
        "",
        f"- Sessions with priced turns: {payload['sessions_with_spend']} "
        f"({payload['root_sessions']} root, {payload['nested_sessions']} subagent)",
        f"- Priced assistant turns: {payload['priced_turns']}",
        f"- Transcripts with no priced turn: {payload['sessions_without_priced_turns']}"
        f" (of which {payload['codec_active_sessions_without_priced_turns']} still recorded "
        f"codec activity)",
        "",
        "## Join",
        "",
        f"- Sessions with both spend and a runtime ledger: {payload['matched_sessions']}",
        f"- Sessions with spend but no ledger: {payload['unmatched_spend_sessions']}",
        f"- Ledgers with no matching transcript: {payload['unmatched_ledger_sessions']}",
        f"- Ledgers with an unreadable schema: {payload['damaged_ledgers']}",
        "",
        "## Where the money went",
        "",
    ]
    lines += _split_lines(
        "Whole corpus, every scanned session (laconic.costs)",
        payload["corpus_cost"],
        payload["corpus_shares"],
    )
    priced_sessions = payload["sessions_with_spend"]
    without_host = payload["priced_sessions_without_host_cost"]
    with_host = priced_sessions - without_host
    if without_host:
        lines += [
            "**Do not read the total above against the host's.** The two cover "
            f"different sessions: {without_host} of the "
            f"{_sessions(priced_sessions)} run on a host that records token "
            "counters and no cost, so they contribute modelled dollars and nothing "
            "the host total can match.",
            "",
            "| Covering | Modelled (laconic.costs) | Host-reported |",
            "| --- | ---: | ---: |",
            f"| The {_sessions(with_host)} the host priced | "
            f"{_usd(payload['host_reporting_cost']['total'])} | "
            f"{_usd(payload['corpus_host_cost_usd'])} |",
            f"| All {_sessions(priced_sessions)} | "
            f"{_usd(payload['corpus_cost']['total'])} | not available |",
            "",
            "Only the first row is a comparison. The second is the corpus-wide "
            "denominator, and the host has no figure to put beside it.",
            "",
        ]
    else:
        # Saying "do not compare these" above a table showing one set priced
        # twice would be a false instruction, and a reader who checked it
        # against the table would be right to stop trusting the next caveat.
        lines += [
            f"Host-reported total for the same turns: "
            f"{_usd(payload['corpus_host_cost_usd'])}. Every priced session here "
            "runs on a host that records a per-turn cost, so that figure and the "
            "modelled total above cover the same sessions and compare directly.",
            "",
        ]
    lines += _split_lines(
        "Sessions the codec was active in (laconic.costs)",
        payload["matched_cost"],
        payload["matched_shares"],
    )
    lines += [
        f"Host-reported total for the same turns: {_usd(payload['matched_host_cost_usd'])}, "
        f"covering all but {payload['matched_sessions_without_host_cost']} of these "
        "sessions. Both figures above cover the same sessions when that count is zero.",
        "",
        "## What the codec did in those sessions",
        "",
        f"- Sessions: {codec['sessions']}",
        f"- Eligible observations: {codec['eligible']}",
        f"- Replaced: {codec['emitted']}; passed through: {codec['pass_through']}",
        f"- Characters raw: {codec['raw_chars']:,}; visible: {codec['visible_chars']:,}; "
        f"avoided: {codec['chars_avoided']:,}",
        f"- Expansions: {codec['full_expansions']} full, {codec['span_expansions']} span",
        "",
        "Counted over every session with a runtime ledger, including any that "
        "recorded no billable turn.",
    ]
    lines += _estimate_lines(payload["estimate"], payload["matched_host_cost_usd"])
    if payload["unpriced_models"]:
        lines += [
            "## Models with no published list price",
            "",
            "Billed at the laconic.costs fallback rate, so the Laconic figure and "
            "the host's own figure necessarily differ for them.",
            "",
        ]
        lines += [f"- `{model}`" for model in payload["unpriced_models"]]
        lines.append("")
    if payload["unknown_usage_keys"]:
        lines += [
            "## Host usage keys this reader does not model",
            "",
        ]
        lines += [f"- `{key}`" for key in payload["unknown_usage_keys"]]
        lines.append("")
    if payload["sessions"]:
        lines += [
            "## Joined sessions",
            "",
            "| Session | Turns | Modelled USD | Host USD | Eligible | Replaced | Chars avoided |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for entry in payload["sessions"]:
            lines.append(
                f"| `{entry['session_hash'][:12]}` | {entry['turns']} | "
                f"{_usd(entry['modelled_cost_usd'])} | {_usd(entry['host_cost_usd'])} | "
                f"{entry['eligible']} | {entry['emitted']} | {entry['chars_avoided']:,} |"
            )
        lines.append("")
    return "\n".join(lines)
