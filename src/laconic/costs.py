"""Provider pricing and cache-aware session cost accounting.

Every number Laconic reports about spend flows through this module. The cache
multipliers are the reason the project exists: a cached prefix is re-billed on
every turn at ``CACHE_READ_MULTIPLIER`` of the input price, so residency, not
emission, is the meter (``docs/system-design.md`` §2.3).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

#: Cache writes bill at 1.25x the input price, cache reads at 0.10x.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

#: A cost split whose components miss 100% by more than this is an accounting
#: bug, not floating-point noise.
SHARE_TOLERANCE_PCT = 1e-9


class ZeroCostError(ValueError):
    """Raised when a cost split is requested for a corpus with no spend."""


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Provider list price in USD per million tokens."""

    input_per_mtok: float
    output_per_mtok: float


#: Published list prices. Unknown models fall back to ``DEFAULT_PRICE`` rather
#: than being dropped, so an unrecognised model still shows up in the bill.
PRICING: Mapping[str, ModelPrice] = {
    "claude-opus-4-8": ModelPrice(5.0, 25.0),
    "claude-opus-4-6": ModelPrice(5.0, 25.0),
    "claude-sonnet-5": ModelPrice(3.0, 15.0),
    "claude-sonnet-4-6": ModelPrice(3.0, 15.0),
    "claude-sonnet-4-5": ModelPrice(3.0, 15.0),
    "claude-fable-5": ModelPrice(1.0, 5.0),
    "claude-haiku-4-5": ModelPrice(1.0, 5.0),
}
DEFAULT_PRICE = ModelPrice(3.0, 15.0)


def price_for(model: str) -> ModelPrice:
    """Return the list price for ``model``, falling back to Sonnet pricing."""
    return PRICING.get(model, DEFAULT_PRICE)


@dataclass(frozen=True, slots=True)
class CostShares:
    """Percentage split of modelled spend. Components sum to 100."""

    uncached_input: float
    cache_read: float
    cache_write: float
    output: float

    def __post_init__(self) -> None:
        if abs(self.total - 100.0) > SHARE_TOLERANCE_PCT:
            raise ValueError(f"cost shares sum to {self.total}, not 100")

    @property
    def total(self) -> float:
        """Sum of the four shares; 100.0 up to floating-point error."""
        return self.uncached_input + self.cache_read + self.cache_write + self.output


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """USD spend split into the four components a provider actually bills."""

    uncached_input: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    output: float = 0.0

    @property
    def total(self) -> float:
        """Total modelled spend in USD."""
        return self.uncached_input + self.cache_read + self.cache_write + self.output

    def __add__(self, other: CostBreakdown) -> CostBreakdown:
        return CostBreakdown(
            uncached_input=self.uncached_input + other.uncached_input,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            output=self.output + other.output,
        )

    def shares(self) -> CostShares:
        """Return the percentage split of ``total``.

        Raises:
            ZeroCostError: if there is no spend to apportion. A zero total means
                the corpus carried no usage records; reporting 0.00% for every
                component would hide that instead of failing.
        """
        total = self.total
        if total <= 0.0:
            raise ZeroCostError("cannot apportion a zero total cost")
        return CostShares(
            uncached_input=100.0 * self.uncached_input / total,
            cache_read=100.0 * self.cache_read / total,
            cache_write=100.0 * self.cache_write / total,
            output=100.0 * self.output / total,
        )


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Token counters accumulated for a single model across a corpus."""

    turns: int = 0
    input_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output_tokens: int = 0

    def add_turn(
        self,
        *,
        input_tokens: int,
        cache_read: int,
        cache_write: int,
        output_tokens: int,
    ) -> ModelUsage:
        """Return a new usage record with one more turn folded in."""
        return replace(
            self,
            turns=self.turns + 1,
            input_tokens=self.input_tokens + input_tokens,
            cache_read=self.cache_read + cache_read,
            cache_write=self.cache_write + cache_write,
            output_tokens=self.output_tokens + output_tokens,
        )

    def cost(self, model: str) -> CostBreakdown:
        """Return the four-component USD cost of this usage under ``model``."""
        price = price_for(model)
        return CostBreakdown(
            uncached_input=self.input_tokens * price.input_per_mtok / 1e6,
            cache_read=(self.cache_read * price.input_per_mtok * CACHE_READ_MULTIPLIER / 1e6),
            cache_write=(self.cache_write * price.input_per_mtok * CACHE_WRITE_MULTIPLIER / 1e6),
            output=self.output_tokens * price.output_per_mtok / 1e6,
        )


def unpriced_models(usage: Mapping[str, ModelUsage]) -> list[str]:
    """Return the models in ``usage`` that were billed at ``DEFAULT_PRICE``.

    Pricing an unknown model at the fallback keeps it in the bill, but the
    figure is a guess. Callers report these so a guessed price is never
    mistaken for a published one.
    """
    return sorted(model for model in usage if model not in PRICING)


def session_cost(usage: Mapping[str, ModelUsage]) -> CostBreakdown:
    """Aggregate per-model usage into one cost breakdown."""
    total = CostBreakdown()
    for model, model_usage in usage.items():
        total = total + model_usage.cost(model)
    return total
