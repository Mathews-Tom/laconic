"""Pre-registered gates (`docs/overview.md` §6.3, `docs/system-design.md`
§4): K1-K5, turned into an executable, CI-enforced verdict.

Every gate reads its target and kill-condition boundary from
:data:`laconic.gates.thresholds.THRESHOLDS`, the single declared source --
no gate module restates a threshold number of its own. K1, K2, K4, and K5
are automated; K3 is human-subject and always reports
:attr:`~laconic.gates.protocol.GateVerdict.MANUAL` rather than being
silently omitted.
"""

from __future__ import annotations
