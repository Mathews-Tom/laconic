"""Laconic Observe: automatic measurement surface (`docs/observe-design.md`).

This package holds the M1 compatibility-spike artifacts only: client
adapter contract models and per-client synthetic event normalization. It
installs no hook, reads no real client configuration or event, and emits
no agent-visible output. See `docs/observe-design.md` and
`.docs/LACONIC_OBSERVE_DEVELOPMENT_PLAN.md` for the governing design and
milestone sequence.
"""

from __future__ import annotations
