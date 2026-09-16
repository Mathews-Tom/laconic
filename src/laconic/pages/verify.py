"""Offline drift verifier for committed Laconic GitHub Pages output."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

from laconic.pages.audit import VerificationError, audit_site
from laconic.pages.site import load_evidence, render_site

__all__ = ["VerificationError", "verify_site"]


def verify_site(
    evidence_path: Path,
    site: Path,
    *,
    forbidden_markers: Sequence[str] = (),
) -> dict[str, object]:
    """Audit, regenerate elsewhere, and compare every committed byte."""

    evidence = load_evidence(evidence_path)
    actual_files = audit_site(
        evidence,
        site,
        forbidden_markers=forbidden_markers,
    )
    expected = site.with_name(f".{site.name}.verify")
    if expected.is_symlink():
        raise VerificationError("verification destination must not be a symlink")
    if expected.exists():
        shutil.rmtree(expected)
    try:
        render_site(evidence_path, expected)
        for relative in actual_files:
            if (site / relative).read_bytes() != (expected / relative).read_bytes():
                raise VerificationError(f"generated drift: {relative}")
    finally:
        if expected.exists():
            shutil.rmtree(expected)
    return {
        "status": "verified",
        "json_sha256": evidence.sha256,
        "files": list(actual_files),
        "provider_calls": 0,
    }
