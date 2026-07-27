"""Observation and action codec: re-encoding at the tool boundary.

See ``docs/system-design.md`` §2.2–2.4. Everything under this package
transforms raw tool traffic into a compact, ledger-backed representation
without ever discarding recoverability.
"""

from __future__ import annotations
