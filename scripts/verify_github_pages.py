#!/usr/bin/env python3
"""Verify deterministic public Laconic GitHub Pages output offline."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from laconic.pages.site import SiteError
from laconic.pages.verify import VerificationError, verify_site

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = PROJECT_ROOT / "docs" / "pages" / "evidence" / "laconic-development.json"
SITE = PROJECT_ROOT / "docs" / "pages"


def main() -> int:
    try:
        result = verify_site(EVIDENCE, SITE)
    except (OSError, SiteError, VerificationError) as error:
        print(f"GitHub Pages verification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
