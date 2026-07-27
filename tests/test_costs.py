"""Cost model behaviour: pricing lookup, cache multipliers, cost splits."""

from __future__ import annotations

import pytest

from laconic.costs import (
    DEFAULT_PRICE,
    PRICING,
    CostBreakdown,
    CostShares,
    ModelUsage,
    ZeroCostError,
    price_for,
    session_cost,
    unpriced_models,
)


def test_known_model_uses_its_own_price() -> None:
    assert price_for("claude-opus-4-8") == PRICING["claude-opus-4-8"]


def test_unknown_model_falls_back_to_default_price() -> None:
    assert price_for("some-model-we-have-never-seen") == DEFAULT_PRICE


def test_cache_read_is_billed_at_a_tenth_of_uncached_input() -> None:
    uncached = ModelUsage(input_tokens=1_000_000).cost("claude-sonnet-5")
    cached = ModelUsage(cache_read=1_000_000).cost("claude-sonnet-5")
    assert cached.cache_read == pytest.approx(uncached.uncached_input * 0.10)


def test_cache_write_is_billed_above_uncached_input() -> None:
    uncached = ModelUsage(input_tokens=1_000_000).cost("claude-sonnet-5")
    written = ModelUsage(cache_write=1_000_000).cost("claude-sonnet-5")
    assert written.cache_write == pytest.approx(uncached.uncached_input * 1.25)


def test_cache_components_use_the_models_own_input_price() -> None:
    """A model priced above the fallback must not be billed at the fallback."""
    cost = ModelUsage(cache_read=1_000_000, cache_write=1_000_000).cost("claude-opus-4-8")
    assert cost.cache_read == pytest.approx(0.5)
    assert cost.cache_write == pytest.approx(6.25)


def test_cost_components_use_the_right_price_axis() -> None:
    cost = ModelUsage(input_tokens=1_000_000, output_tokens=1_000_000).cost("claude-opus-4-8")
    assert cost.uncached_input == pytest.approx(5.0)
    assert cost.output == pytest.approx(25.0)
    assert cost.total == pytest.approx(30.0)


def test_add_turn_accumulates_without_mutating() -> None:
    first = ModelUsage()
    second = first.add_turn(input_tokens=10, cache_read=20, cache_write=30, output_tokens=40)
    third = second.add_turn(input_tokens=1, cache_read=2, cache_write=3, output_tokens=4)
    assert first == ModelUsage()
    assert second.turns == 1
    assert third == ModelUsage(
        turns=2, input_tokens=11, cache_read=22, cache_write=33, output_tokens=44
    )


def test_shares_sum_to_one_hundred_percent() -> None:
    usage = ModelUsage(
        input_tokens=1_234, cache_read=987_654, cache_write=54_321, output_tokens=6_789
    )
    shares = usage.cost("claude-sonnet-5").shares()
    assert shares.total == pytest.approx(100.0, abs=1e-9)


def test_shares_reproduce_the_documented_cost_ordering() -> None:
    """Cache reads dominate a realistic session; output is a minority slice."""
    usage = ModelUsage(
        input_tokens=50_000,
        cache_read=28_000_000,
        cache_write=1_000_000,
        output_tokens=150_000,
    )
    shares = usage.cost("claude-sonnet-5").shares()
    assert shares.cache_read > shares.cache_write > shares.output
    assert shares.output > shares.uncached_input
    assert shares.total == pytest.approx(100.0, abs=1e-9)


def test_zero_spend_raises_instead_of_reporting_a_meaningless_split() -> None:
    with pytest.raises(ZeroCostError):
        CostBreakdown().shares()


def test_session_cost_sums_across_models_at_their_own_prices() -> None:
    usage = {
        "claude-opus-4-8": ModelUsage(output_tokens=1_000_000),
        "claude-haiku-4-5": ModelUsage(output_tokens=1_000_000),
    }
    assert session_cost(usage).output == pytest.approx(30.0)


def test_session_cost_of_no_usage_is_zero() -> None:
    assert session_cost({}).total == 0.0


def test_unknown_models_are_reported_as_guessed_prices() -> None:
    usage = {
        "claude-sonnet-5": ModelUsage(turns=1),
        "some-model-we-have-never-seen": ModelUsage(turns=1),
        "unknown": ModelUsage(turns=1),
    }
    assert unpriced_models(usage) == ["some-model-we-have-never-seen", "unknown"]


def test_shares_that_do_not_sum_to_one_hundred_are_rejected() -> None:
    with pytest.raises(ValueError, match="not 100"):
        CostShares(uncached_input=1.0, cache_read=2.0, cache_write=3.0, output=4.0)
