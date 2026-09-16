#!/usr/bin/env python3
"""Verify the committed dashboard demo is deterministic, synthetic, and current."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Final

from generate_dashboard_demo import (
    ARTIFACT_NAMES,
    DEFAULT_FIXTURE,
    DEFAULT_OUTPUT,
    artifact_bytes,
    generate,
)

from laconic.spend.privacy import validate_report_json

_VISIBLE_ARTIFACTS: Final = frozenset(
    {"spend-composition.md", "spend-composition.html", "dashboard-preview.svg"}
)
_FORBIDDEN_CLAIMS: Final = (
    "cost savings",
    "measured savings",
    "proven savings",
    "savings achieved",
    "tokens saved",
)
_PRIVATE_MARKERS: Final = ("/Users/", "C:\\Users\\", "BEGIN OPENSSH PRIVATE KEY")


def verify() -> None:
    first = artifact_bytes(DEFAULT_FIXTURE)
    second = artifact_bytes(DEFAULT_FIXTURE)
    if first != second:
        raise ValueError("two fixture generations were not byte-identical")

    source = json.loads(DEFAULT_FIXTURE.read_text())
    session_id = source["session"]["session_id"]
    report = json.loads(first["spend-composition.json"])
    validate_report_json(report)
    basis = report["estimate"]["basis"]
    if basis != "modelled_not_measured":
        raise ValueError("synthetic report lost its modelled-not-measured basis")
    digest = hashlib.sha256(first["spend-composition.json"]).hexdigest()

    for name in ARTIFACT_NAMES:
        committed = (DEFAULT_OUTPUT / name).read_bytes()
        if committed != first[name]:
            raise ValueError(
                f"{DEFAULT_OUTPUT / name} has drifted; run scripts/generate_dashboard_demo.py"
            )

    for name in _VISIBLE_ARTIFACTS:
        text = first[name].decode().lower()
        if "synthetic fixture" not in text:
            raise ValueError(f"{name} is missing the visible synthetic fixture marker")
        if "modelled_not_measured" not in text:
            raise ValueError(f"{name} is missing the modelled-not-measured basis")
        if digest not in text:
            raise ValueError(f"{name} is missing the canonical JSON digest")

    public_text = "\n".join(value.decode(errors="strict") for value in first.values())
    lowered = public_text.lower()
    for claim in _FORBIDDEN_CLAIMS:
        if claim in lowered:
            raise ValueError(f"public demo contains forbidden claim: {claim!r}")
    for marker in _PRIVATE_MARKERS:
        if marker in public_text:
            raise ValueError(f"public demo contains private marker: {marker!r}")
    if session_id in public_text or '"session_id"' in public_text:
        raise ValueError("public demo contains the fixture's raw session identity")

    with tempfile.TemporaryDirectory() as external_dir:
        external_fixture = Path(external_dir) / "evidence-ledger.json"
        external_fixture.write_bytes(DEFAULT_FIXTURE.read_bytes())
        try:
            artifact_bytes(external_fixture)
        except ValueError as error:
            if "fixture source must be under" not in str(error):
                raise
        else:
            raise ValueError("generator accepted a fixture outside docs/demo/fixtures")

    with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
        generate(DEFAULT_FIXTURE, Path(first_dir))
        generate(DEFAULT_FIXTURE, Path(second_dir))
        for name in ARTIFACT_NAMES:
            first_path = Path(first_dir) / name
            second_path = Path(second_dir) / name
            if first_path.read_bytes() != second_path.read_bytes():
                raise ValueError(f"generated {name} changed between filesystem runs")


def main() -> None:
    verify()
    print("dashboard demo: deterministic, synthetic, private-value-free, and current")


if __name__ == "__main__":
    main()
