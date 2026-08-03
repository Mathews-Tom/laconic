"""human-bug-catch human-study harness: instrumentation and materials only.

``docs/system-design.md`` §4.1 states the protocol in full: within-subjects,
counterbalanced, matched task pairs, pre-registered analysis with the
equivalence margin fixed before any data exists. This package builds that
instrumentation. Recruiting or running human participants is a manual gate
outside this plan (``DEVELOPMENT_PLAN.md`` §6 milestone's In/Out of scope row).
"""

from __future__ import annotations
