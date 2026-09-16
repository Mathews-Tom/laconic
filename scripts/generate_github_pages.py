#!/usr/bin/env python3
"""Regenerate the deterministic public Laconic GitHub Pages tree."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from laconic.pages.site import SiteError, render_site

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = PROJECT_ROOT / "docs" / "pages" / "evidence" / "laconic-development.json"
DESTINATION = PROJECT_ROOT / "docs" / "pages"


def main() -> int:
    try:
        result = render_site(EVIDENCE, DESTINATION)
    except (OSError, SiteError) as error:
        print(f"GitHub Pages generation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result.public_summary(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
