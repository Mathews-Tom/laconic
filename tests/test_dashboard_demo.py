"""The committed public dashboard demo remains reproducible and synthetic."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_committed_dashboard_demo_verifies_end_to_end() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/verify_dashboard_demo.py"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "deterministic, synthetic, private-value-free, and current" in result.stdout
