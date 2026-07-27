#!/usr/bin/env python3
"""Measure where tokens actually go in real coding-agent sessions.

Laconic's founding premise is that human-facing explanatory prose is a large,
expensive fraction of a coding assistant's token spend. This script tests that
premise against real session logs instead of curated benchmark tasks.

It parses Claude Code / omp session JSONL transcripts and decomposes every
assistant turn into four channels:

    tool results  -- observations the agent reads back (Read, Bash, ...)
    tool_use args -- actions the agent emits (patches, commands, file writes)
    prose         -- human-facing explanatory text, fenced code removed
    user prompts  -- what the human typed

then weights the result by real per-model API pricing.

Usage:
    uv run python scripts/measure_session_composition.py [SESSION_DIR ...]

Defaults to ~/.claude/projects. Prints a channel breakdown, a cost breakdown,
and the headline number: human-facing prose as a share of total spend.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

# $/MTok (input, output). Cache write bills at 1.25x input, cache read at 0.10x.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-fable-5": (1.0, 5.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
DEFAULT_PRICE = (3.0, 15.0)
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

FENCED_CODE = re.compile(r"```.*?```", re.S)


@dataclass
class ModelUsage:
    turns: int = 0
    input_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output_tokens: int = 0

    def cost(self, model: str) -> tuple[float, float]:
        """Return (total_cost, output_cost) in USD."""
        in_price, out_price = PRICING.get(model, DEFAULT_PRICE)
        out_cost = self.output_tokens * out_price / 1e6
        total = (
            self.input_tokens * in_price / 1e6
            + self.cache_read * in_price * CACHE_READ_MULTIPLIER / 1e6
            + self.cache_write * in_price * CACHE_WRITE_MULTIPLIER / 1e6
            + out_cost
        )
        return total, out_cost


@dataclass
class Channels:
    """Character volume per communication channel."""

    prose: int = 0
    fenced_code_in_prose: int = 0
    tool_args: int = 0
    tool_results: int = 0
    user_prompts: int = 0
    prose_per_turn: list[int] = field(default_factory=list)
    result_chars_by_tool: collections.Counter[str] = field(
        default_factory=collections.Counter
    )
    calls_by_tool: collections.Counter[str] = field(default_factory=collections.Counter)


def iter_records(path: Path) -> Iterator[dict]:
    with path.open(errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def scan(paths: list[Path]) -> tuple[Channels, dict[str, ModelUsage]]:
    chan = Channels()
    usage: dict[str, ModelUsage] = collections.defaultdict(ModelUsage)
    # tool_use_id -> tool name, so tool_result volume can be attributed.
    tool_name_by_id: dict[str, str] = {}

    for path in paths:
        for rec in iter_records(path):
            message = rec.get("message") or {}
            kind = rec.get("type")

            if kind == "assistant":
                u = message.get("usage") or {}
                if not u:
                    continue
                model = message.get("model", "unknown")
                mu = usage[model]
                mu.turns += 1
                mu.input_tokens += u.get("input_tokens", 0)
                mu.cache_read += u.get("cache_read_input_tokens", 0)
                mu.cache_write += u.get("cache_creation_input_tokens", 0)
                mu.output_tokens += u.get("output_tokens", 0)

                turn_prose = 0
                for block in message.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text", "")
                        stripped = FENCED_CODE.sub("", text)
                        chan.fenced_code_in_prose += len(text) - len(stripped)
                        chan.prose += len(stripped)
                        turn_prose += len(stripped)
                    elif btype == "tool_use":
                        name = block.get("name", "unknown")
                        tool_name_by_id[block.get("id", "")] = name
                        chan.tool_args += len(json.dumps(block.get("input", {})))
                        chan.calls_by_tool[name] += 1
                chan.prose_per_turn.append(turn_prose)

            elif kind == "user":
                content = message.get("content")
                if isinstance(content, str):
                    chan.user_prompts += len(content)
                elif isinstance(content, list):
                    for block in content:
                        if (
                            not isinstance(block, dict)
                            or block.get("type") != "tool_result"
                        ):
                            continue
                        body = block.get("content")
                        text = body if isinstance(body, str) else json.dumps(body)
                        chan.tool_results += len(text)
                        name = tool_name_by_id.get(block.get("tool_use_id", ""), "?")
                        chan.result_chars_by_tool[name] += len(text)

    return chan, dict(usage)


def report(chan: Channels, usage: dict[str, ModelUsage]) -> None:
    total_cost = 0.0
    output_cost = 0.0
    print(f"{'model':22}{'turns':>8}{'out tok':>12}{'$out':>10}{'$total':>10}")
    for model, mu in sorted(usage.items(), key=lambda kv: -kv[1].output_tokens):
        tc, oc = mu.cost(model)
        total_cost += tc
        output_cost += oc
        print(f"{model:22}{mu.turns:>8}{mu.output_tokens:>12,}{oc:>10.2f}{tc:>10.2f}")

    channel_total = (
        chan.tool_results + chan.tool_args + chan.prose + chan.user_prompts
    )
    if not channel_total or not total_cost:
        print("\nNo usable session data found.", file=sys.stderr)
        return

    print("\nChannel volume (characters entering the context window):")
    for label, value in (
        ("tool results (observations)", chan.tool_results),
        ("tool_use args (actions)", chan.tool_args),
        ("assistant prose (human-facing)", chan.prose),
        ("human prompts", chan.user_prompts),
    ):
        print(f"  {label:34}{value:>13,}{100 * value / channel_total:>8.2f}%")

    emitted = chan.prose + chan.fenced_code_in_prose + chan.tool_args
    prose_share_of_output = chan.prose / emitted
    output_share_of_cost = output_cost / total_cost
    prose_share_of_cost = output_share_of_cost * prose_share_of_output

    print(f"\nTotal spend                          ${total_cost:>12,.2f}")
    print(f"Output share of spend                 {100 * output_share_of_cost:>12.2f}%")
    print(f"Prose share of emitted output         {100 * prose_share_of_output:>12.2f}%")
    print(f"HUMAN-FACING PROSE SHARE OF SPEND     {100 * prose_share_of_cost:>12.2f}%")
    for ratio in (0.44, 0.65, 0.90, 1.00):
        print(
            f"  prose compressed {int(ratio * 100):>3}% -> total saving "
            f"{100 * prose_share_of_cost * ratio:.2f}%"
        )

    per_turn = sorted(chan.prose_per_turn)
    if per_turn:
        n = len(per_turn)
        pct = lambda q: per_turn[min(n - 1, int(n * q))]  # noqa: E731
        zero = sum(1 for x in per_turn if x == 0)
        print(
            f"\nProse chars/turn: p50={pct(0.5)} p90={pct(0.9)} p99={pct(0.99)} "
            f"max={per_turn[-1]}"
        )
        print(f"Turns emitting zero prose: {100 * zero / n:.1f}%")

    print("\nTop tools by observation volume:")
    for name, chars in chan.result_chars_by_tool.most_common(8):
        calls = chan.calls_by_tool[name] or 1
        print(f"  {name:26}{chars:>12,} chars  calls={calls:<6} mean={chars // calls:,}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "roots",
        nargs="*",
        default=[str(Path.home() / ".claude" / "projects")],
        help="directories containing session JSONL transcripts",
    )
    args = parser.parse_args()

    paths = [p for root in args.roots for p in Path(root).rglob("*.jsonl")]
    if not paths:
        print(f"No .jsonl transcripts found under {args.roots}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Scanning {len(paths)} session transcripts...\n")
    report(*scan(paths))


if __name__ == "__main__":
    main()
