#!/usr/bin/env python3
"""Compatibility shim for ``laconic measure``.

The four-channel measurement this script used to implement now lives in the
package (``laconic.replay.corpus`` and ``laconic.costs``) and is exposed as
``laconic measure``. The script is kept because ``docs/overview.md`` §2 cites it
as the way to reproduce the founding measurement; it delegates so that both
paths always print the same thing.

Usage:
    uv run python scripts/measure_session_composition.py [SESSION_DIR ...]
"""

from __future__ import annotations

import sys

from laconic.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["measure", *sys.argv[1:]]))
