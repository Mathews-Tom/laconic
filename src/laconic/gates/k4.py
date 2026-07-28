"""K4: codec overhead in added input tokens per turn.

``docs/overview.md`` §6.3: "Codec overhead in added input tokens per
turn ... < 500," kill "above → Caveman's net-negative trap, reproduced."
Read (``.docs/DEVELOPMENT_PLAN_HISTORY.md`` H-25) as the codec's own
*structural* tax -- the outline header, span markers, and elision framing
:mod:`laconic.codec.observe` emits regardless of content size -- not a
net-cost figure (that is K1's job). Unlike K1/K2, K4 needs neither a
committed fixture nor a model: it runs the real encoder against every
observation in the corpus, live, on any build.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import cast

from laconic.codec.observe import ObservationCodec, subject_for
from laconic.gates.protocol import GateResult
from laconic.ledger import Ledger
from laconic.replay.corpus import JsonValue, iter_records
from laconic.replay.engine import find_baseline_transcripts

#: A widely-cited approximation for English/code text -- documented here
#: because K4 is explicitly a structural/format-tax gate, never a dollar
#: figure; M8's net-cost accounting stays strictly usage-based and never
#: uses a char-per-token estimate anywhere.
CHARS_PER_TOKEN = 4.0


def _tool_results_by_id(records: Sequence[dict[str, JsonValue]]) -> dict[str, str]:
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


def _overhead_tokens(path: Path, codec: ObservationCodec) -> list[tuple[float, bool]]:
    """Return one ``(overhead tokens, encoded a tool call)`` pair per
    assistant turn in ``path``.

    Overhead is ``0.0`` for a turn with no tool call, one whose result
    this transcript never recorded, or one the codec shrank; the real
    ``(encoded_chars - raw_chars) / CHARS_PER_TOKEN`` for one the codec
    made bigger. Every tool name is encoded, dispatched by
    :meth:`~laconic.codec.observe.ObservationCodec.encode` exactly as
    production does -- including one this build falls back on -- so K4
    measures the same population the codec actually touches, not a
    hand-picked subset of it.
    """
    records = cast(
        "list[dict[str, JsonValue]]",
        [record for _, record in iter_records(path) if record is not None],
    )
    results_by_id = _tool_results_by_id(records)
    overhead_per_turn: list[tuple[float, bool]] = []
    turn = 0
    for record in records:
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if not (usage := message.get("usage")) or not isinstance(usage, dict):
            continue
        turn_overhead = 0.0
        encoded_any = False
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            tool_input = block.get("input")
            tool_id = block.get("id")
            if not (
                isinstance(name, str) and isinstance(tool_input, dict) and isinstance(tool_id, str)
            ):
                continue
            raw = results_by_id.get(tool_id)
            if raw is None:
                continue
            subject = subject_for(tool_input)
            encoded = codec.encode(name, subject, raw, tool_input, turn=turn)
            encoded_any = True
            char_overhead = encoded.encoded_chars - encoded.raw_chars
            if char_overhead > 0:
                turn_overhead += char_overhead / CHARS_PER_TOKEN
        overhead_per_turn.append((turn_overhead, encoded_any))
        turn += 1
    return overhead_per_turn


def measure(paths: Sequence[Path]) -> GateResult:
    """Measure K4 across every baseline transcript under ``paths``.

    Requires no recorded-response fixture: K4 asks whether the codec's
    own output format ever costs more than the raw content it replaces,
    which is answerable from the baseline transcript and the real codec
    alone.

    The mean is over every assistant turn in the corpus -- including a
    turn with no tool call at all, which contributes ``0.0`` -- matching
    ``docs/overview.md`` §6.3's "added input tokens per turn" framing
    rather than "per observation." ``detail`` reports how many of those
    turns actually invoked a tool this codec encoded, so the denominator
    choice is never left for a reader to infer.
    """
    baselines = find_baseline_transcripts(paths)
    if not baselines:
        return GateResult.measured("K4", 0.0, detail="no baseline transcripts found")
    all_overhead: list[tuple[float, bool]] = []
    with Ledger(":memory:", "k4-gate") as ledger:
        codec = ObservationCodec(ledger)
        for baseline in baselines:
            all_overhead.extend(_overhead_tokens(baseline, codec))
    if not all_overhead:
        return GateResult.measured("K4", 0.0, detail="no assistant turns found")
    values = [value for value, _ in all_overhead]
    mean_overhead = sum(values) / len(values)
    encoded_turns = sum(1 for _, encoded_any in all_overhead if encoded_any)
    inflated = sum(1 for value in values if value > 0.0)
    detail = (
        f"{len(baselines)} session(s), {len(all_overhead)} assistant turn(s), "
        f"{encoded_turns} of which invoked a tool this codec encoded, "
        f"{inflated} turn(s) where encoding added chars over raw"
    )
    return GateResult.measured("K4", mean_overhead, detail=detail)
