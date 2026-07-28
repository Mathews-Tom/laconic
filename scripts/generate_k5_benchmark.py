#!/usr/bin/env python3
"""Generate the committed K5 response fixture for `tests/corpus`.

See `laconic.gates.k5`'s module docstring and
`.docs/DEVELOPMENT_PLAN_HISTORY.md` H-25 for why this is a synthesized,
not live-captured, fixture: `generate_synthetic_responses` answers every
extracted item correctly under both conditions, matching what a
correctly-behaving codec should produce.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from laconic.gates.k5 import (  # noqa: E402
    extract_items,
    generate_synthetic_responses,
    responses_path_for,
    write_responses,
)

CORPUS_DIR = REPO_ROOT / "tests" / "corpus"


def main() -> int:
    items = extract_items([CORPUS_DIR])
    if not items:
        print(f"no K5 benchmark items found under {CORPUS_DIR}", file=sys.stderr)
        return 1
    responses = generate_synthetic_responses(items, model="claude-sonnet-5")
    destination = responses_path_for([CORPUS_DIR])
    write_responses(destination, responses)
    relative = destination.relative_to(REPO_ROOT)
    print(f"wrote {relative} ({len(items)} items, {len(responses)} responses)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
