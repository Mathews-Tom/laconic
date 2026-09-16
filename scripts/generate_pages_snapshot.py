#!/usr/bin/env python3
"""Freeze one private Laconic-development cohort and write its public aggregate."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from laconic.pages.snapshot import SnapshotError, generate_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--manifest-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("CI"):
        print(
            "pages snapshot generation failed: private_extraction_forbidden_in_ci",
            file=sys.stderr,
        )
        return 1
    try:
        result = generate_snapshot(
            repo_root=args.repo_root,
            manifest_dir=args.manifest_dir,
            output=args.output,
        )
    except SnapshotError as error:
        print(f"pages snapshot generation failed: {error.code}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "pages snapshot generation failed: unexpected_private_extraction_failure",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result.public_summary(), indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
