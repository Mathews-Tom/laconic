"""Strict live-usage normalization and decimal pricing for K1 paired replay."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from laconic.k1.paired_config import PairedReplayConfigError, PriceTable, UsageMapping

_MILLION = Decimal("1000000")


@dataclass(frozen=True, slots=True)
class BillableResponseUsage:
    """One live response's complete provider-normalized token counters."""

    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        for field_name, value in (
            ("input_tokens", self.input_tokens),
            ("cache_read_tokens", self.cache_read_tokens),
            ("cache_write_tokens", self.cache_write_tokens),
            ("output_tokens", self.output_tokens),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PairedReplayConfigError(f"{field_name} must be a non-negative integer")


def normalize_usage(
    native_usage: Mapping[str, object], mapping: UsageMapping
) -> BillableResponseUsage:
    """Map an explicit native response usage object into all billable fields.

    The input field must exclude the separately declared cache categories. Each
    configured field is mandatory, and undeclared native fields terminate the run
    rather than becoming free usage.
    """
    declared_fields = {
        mapping.input_field,
        mapping.cache_read_field,
        mapping.cache_write_field,
        mapping.output_field,
    }
    unexpected_fields = set(native_usage) - declared_fields
    if unexpected_fields:
        raise PairedReplayConfigError(
            f"native usage contains undeclared fields: {sorted(unexpected_fields)!r}"
        )
    return BillableResponseUsage(
        input_tokens=_counter(native_usage, mapping.input_field),
        cache_read_tokens=_counter(native_usage, mapping.cache_read_field),
        cache_write_tokens=_counter(native_usage, mapping.cache_write_field),
        output_tokens=_counter(native_usage, mapping.output_field),
    )


def cost_usage(usage: BillableResponseUsage, pricing: PriceTable) -> Decimal:
    """Return exact USD cost for cache-exclusive normalized input counters."""
    return (
        Decimal(usage.input_tokens) * Decimal(pricing.input_per_mtok)
        + Decimal(usage.cache_read_tokens) * Decimal(pricing.cache_read_per_mtok)
        + Decimal(usage.cache_write_tokens) * Decimal(pricing.cache_write_per_mtok)
        + Decimal(usage.output_tokens) * Decimal(pricing.output_per_mtok)
    ) / _MILLION


def _counter(native_usage: Mapping[str, object], field_name: str) -> int:
    try:
        value = native_usage[field_name]
    except KeyError as error:
        raise PairedReplayConfigError(
            f"native usage is missing configured field {field_name!r}"
        ) from error
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PairedReplayConfigError(
            f"native usage field {field_name!r} must be a non-negative integer"
        )
    return value
