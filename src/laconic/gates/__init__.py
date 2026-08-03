"""Executable, CI-enforced product evaluation criteria.

Every criterion reads its target and kill-condition boundary from
:data:`laconic.gates.thresholds.THRESHOLDS`, the single declared source.
Net cost, action equivalence, codec overhead, and reasoning accuracy are
automated; human bug catch remains a manual assessment.
"""

from __future__ import annotations
