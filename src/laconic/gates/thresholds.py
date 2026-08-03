"""The single declared threshold source every gate reads from.

``docs/overview.md`` §6.3 and ``docs/system-design.md`` §4 both state the
same five target/kill numbers; this module is the one place they are
encoded as data, so no gate module can drift from the published table by
restating a number of its own. ``docs/system-design.md`` §4's risk framing
is the reason this indirection exists: "gates that are green because they
are weak are worse than no gates," and a threshold silently loosened in
one call site while the docs still advertise the tighter one is exactly
that failure mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class GateThreshold:
    """One gate's target and kill-condition boundary.

    ``direction`` names which way "better" points. Net cost and action
    equivalence pass at or above their targets. Codec overhead and reasoning
    accuracy pass at or below their targets. The latter two use the target as
    their kill boundary; the former two have a reported non-kill failure zone.
    """

    gate: str
    description: str
    unit: str
    direction: Literal["at_least", "at_most"]
    target: float
    kill: float

    def __post_init__(self) -> None:
        if self.direction == "at_least" and self.kill > self.target:
            raise ValueError(
                f"{self.gate}: kill ({self.kill}) must not exceed target ({self.target}) "
                "for an at_least gate"
            )
        if self.direction == "at_most" and self.kill < self.target:
            raise ValueError(
                f"{self.gate}: kill ({self.kill}) must not be below target ({self.target}) "
                "for an at_most gate"
            )


#: ``docs/overview.md`` §6.3, verbatim. human-bug-catch carries no automated threshold --
#: it is human-subject and always reports MANUAL regardless of any number
#: here -- so it is intentionally absent from this table.
THRESHOLDS: dict[str, GateThreshold] = {
    "net-cost": GateThreshold(
        gate="net-cost",
        description=(
            "Session-level net cost reduction on replayed real traces, "
            "including follow-up reads the codec induces"
        ),
        unit="%",
        direction="at_least",
        target=25.0,
        kill=15.0,
    ),
    "action-equivalence": GateThreshold(
        gate="action-equivalence",
        description="Action equivalence, compressed vs raw observation",
        unit="%",
        direction="at_least",
        target=95.0,
        kill=90.0,
    ),
    "codec-overhead": GateThreshold(
        gate="codec-overhead",
        description="Codec overhead in added input tokens per turn",
        unit="tokens",
        direction="at_most",
        target=500.0,
        kill=500.0,
    ),
    "reasoning-accuracy": GateThreshold(
        gate="reasoning-accuracy",
        description="Exact-match reasoning benchmark, codec on vs off",
        unit="pp",
        direction="at_most",
        target=2.0,
        kill=2.0,
    ),
}
