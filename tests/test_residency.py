"""Residency manager: break-even arithmetic, append-only default, gated
compaction with audit logging, and dry-run preview. ``docs/system-design.md``
§2.3, §5.1, §9.4 and ``docs/overview.md`` §3.3.
"""

from __future__ import annotations

import pytest

from laconic.residency import breakeven_turns

# --- Break-even arithmetic --------------------------------------------------


@pytest.mark.parametrize(
    ("prefix_after", "delta", "expected"),
    [
        (40_000, 60_000, 8.3),
        (60_000, 40_000, 18.8),
        (80_000, 20_000, 50.0),
    ],
)
def test_breakeven_reproduces_the_documented_table(
    prefix_after: int, delta: int, expected: float
) -> None:
    """``docs/system-design.md`` §2.3's three-row table, exactly."""
    assert round(breakeven_turns(prefix_after, delta), 1) == expected


def test_breakeven_reduces_to_the_published_closed_form() -> None:
    """12.5 * prefix_after / delta at the current cache pricing."""
    assert breakeven_turns(60_000, 40_000) == pytest.approx(12.5 * 60_000 / 40_000)


@pytest.mark.parametrize("delta", [0, -1, -60_000])
def test_breakeven_of_a_non_shrinking_rewrite_is_infinite(delta: int) -> None:
    """A rewrite that does not shrink the prefix never pays for itself."""
    assert breakeven_turns(40_000, delta) == float("inf")


def test_breakeven_scales_linearly_with_prefix_after() -> None:
    assert breakeven_turns(80_000, 40_000) == pytest.approx(2 * breakeven_turns(40_000, 40_000))


def test_breakeven_scales_inversely_with_delta() -> None:
    assert breakeven_turns(40_000, 80_000) == pytest.approx(breakeven_turns(40_000, 40_000) / 2)
