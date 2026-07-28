#!/usr/bin/env python3
"""Generate committed, provenance-tagged `codec="on"` recorded-response
fixtures for every baseline transcript under `tests/corpus`.

`docs/system-design.md` §2.6 names `laconic.replay.engine.ReplayClient`
live capture as the intended way to produce these; this script is the
documented, deterministic stand-in `.docs/DEVELOPMENT_PLAN_HISTORY.md`
H-23/H-25 record for the fixture corpus, which is entirely synthetic and
has no live model to capture from.

Methodology (recorded in H-25): the fixture corpus's own token counters
are, by `tests/corpus/README.md`'s own design, set to production-like
magnitudes independent of the synthetic text's actual length -- *except*
for a handful of deliberately whale-shaped `Read` results (raw content
over `WHALE_CHAR_THRESHOLD`), whose `cache_creation_input_tokens` the
corpus generator visibly set close to 1:1 with the observation's raw char
count (confirmed by direct correlation: `cache_creation - baseline ≈
raw_chars` for every such turn, to within a few percent). Reducing *that*
turn's `cache_creation_input_tokens` by the exact same char delta the
real `laconic.codec.observe.ObservationCodec` measures, and carrying the
same token delta forward as a reduction to every later turn's
`cache_read_input_tokens` (the resident prefix that whale read joined is
now permanently smaller), is therefore the one place this script can
honestly derive a token change from the codec's real behaviour rather
than inventing a synthetic-on-synthetic estimate. Every other turn's
usage is copied unchanged.

The action sequence itself is never touched: this corpus models a codec
that behaves exactly as designed (no induced follow-up reads, full
structural action equivalence) -- `tests/corpus/README.md` documents this
as a known, deliberate limitation, not an unnoticed gap.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from laconic.codec.observe import ObservationCodec, subject_for  # noqa: E402
from laconic.ledger import Ledger  # noqa: E402
from laconic.replay.corpus import JsonValue  # noqa: E402
from laconic.replay.engine import RECORDED_RESPONSE_SUFFIX  # noqa: E402

CORPUS_DIR = REPO_ROOT / "tests" / "corpus"

#: A `Read`/`Bash`/`Grep`/`Glob` result over this many raw characters is
#: treated as a "whale" observation whose cache-write cost the corpus
#: generator visibly correlated with its own char length -- see the
#: module docstring.
WHALE_CHAR_THRESHOLD = 5_000

#: The fixture's own captured-at timestamp. Fixed rather than "now" so
#: regenerating the fixture from unchanged inputs reproduces byte-identical
#: output.
CAPTURED_AT = "2026-07-28T00:00:00Z"

FETCH_TOOLS = frozenset({"Read", "Bash", "Grep", "Glob"})


def _tool_results_by_id(records: list[dict[str, JsonValue]]) -> dict[str, str]:
    results: dict[str, str] = {}
    for record in records:
        if record.get("type") != "user":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            body = block.get("content")
            if isinstance(tool_use_id, str) and isinstance(body, str):
                results[tool_use_id] = body
    return results


def generate_fixture(baseline: Path) -> list[dict[str, JsonValue]]:
    """Return the codec="on" fixture records for `baseline`."""
    records = cast(
        "list[dict[str, JsonValue]]",
        [json.loads(line) for line in baseline.read_text().splitlines() if line.strip()],
    )
    results_by_id = _tool_results_by_id(records)

    fixture: list[dict[str, JsonValue]] = []
    cumulative_reduction = 0.0
    with Ledger(":memory:", "generate-replay-fixtures") as ledger:
        codec = ObservationCodec(ledger)
        turn = 0
        for record in records:
            if record.get("type") != "assistant":
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict) or not usage:
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue

            new_usage = dict(usage)
            for block in content:
                if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                    continue
                name = block.get("name")
                tool_input = block.get("input")
                tool_id = block.get("id")
                if not (isinstance(name, str) and isinstance(tool_input, dict)):
                    continue
                if name not in FETCH_TOOLS or not isinstance(tool_id, str):
                    continue
                raw = results_by_id.get(tool_id)
                if raw is None:
                    continue
                subject = subject_for(tool_input)
                encoded = codec.encode(name, subject, raw, tool_input, turn=turn)
                if encoded.raw_chars <= WHALE_CHAR_THRESHOLD:
                    continue
                token_delta = float(encoded.raw_chars - encoded.encoded_chars)
                cache_write = int(
                    cast(
                        int,
                        new_usage.get(
                            "cache_creation_input_tokens", usage["cache_creation_input_tokens"]
                        ),
                    )
                )
                new_usage["cache_creation_input_tokens"] = max(0, round(cache_write - token_delta))
                cumulative_reduction += token_delta

            cache_read = int(cast(int, usage.get("cache_read_input_tokens", 0)))
            new_usage["cache_read_input_tokens"] = max(0, round(cache_read - cumulative_reduction))

            fixture.append(
                {
                    "type": "assistant",
                    "provenance": {
                        "source": "recorded",
                        "model": message.get("model", "unknown"),
                        "captured_at": CAPTURED_AT,
                    },
                    "message": {
                        "role": "assistant",
                        "model": message.get("model", "unknown"),
                        "content": content,
                        "usage": new_usage,
                    },
                }
            )
            turn += 1
    return fixture


def main() -> int:
    baselines = sorted(CORPUS_DIR.glob("*.jsonl"))
    baselines = [path for path in baselines if not path.name.endswith(RECORDED_RESPONSE_SUFFIX)]
    if not baselines:
        print(f"no baseline transcripts found under {CORPUS_DIR}", file=sys.stderr)
        return 1
    for baseline in baselines:
        fixture = generate_fixture(baseline)
        destination = baseline.with_name(baseline.stem + RECORDED_RESPONSE_SUFFIX)
        destination.write_text("\n".join(json.dumps(record) for record in fixture) + "\n")
        print(f"wrote {destination.relative_to(REPO_ROOT)} ({len(fixture)} turns)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
