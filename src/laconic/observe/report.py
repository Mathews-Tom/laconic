"""M1 deliverable: the explicit GO/NO-GO event matrix across every client.

Draws only on the two clients' :class:`~laconic.observe.contracts.AdapterContract`
values; adding a third client's spike result requires no change here beyond
listing its contract in :data:`_CONTRACTS`.
"""

from __future__ import annotations

from dataclasses import dataclass

from laconic.observe.claude_code import CLAUDE_CODE_CONTRACT
from laconic.observe.contracts import AdapterContract, ObserveEventKind
from laconic.observe.omp import OMP_CONTRACT

_CONTRACTS: tuple[AdapterContract, ...] = (CLAUDE_CODE_CONTRACT, OMP_CONTRACT)
_REQUIRED_EVENTS = (ObserveEventKind.TOOL_RESULT_SUCCESS, ObserveEventKind.SESSION_CLOSE)


@dataclass(frozen=True, slots=True)
class ClientVerdict:
    """One client's M1 compatibility verdict, with the reason cited."""

    contract: AdapterContract
    verdict: str
    """``"GO"`` or ``"NO-GO"``."""

    reason: str


def build_matrix() -> tuple[ClientVerdict, ...]:
    """Return one :class:`ClientVerdict` per known client contract."""
    verdicts: list[ClientVerdict] = []
    for contract in _CONTRACTS:
        if contract.go():
            verdicts.append(
                ClientVerdict(
                    contract=contract, verdict="GO", reason="both required events verified"
                )
            )
        else:
            missing = [
                event.value for event in _REQUIRED_EVENTS if event not in contract.supported_events
            ]
            verdicts.append(
                ClientVerdict(contract=contract, verdict="NO-GO", reason=f"missing: {missing}")
            )
    return tuple(verdicts)
