"""reasoning-accuracy: exact-match reasoning benchmark, codec on vs off.

``docs/overview.md`` §6.3: "Exact-match reasoning benchmark, codec on vs
off ... within 2pp," kill "beyond → format tax confirmed on our stack."
Per ``.docs/DEVELOPMENT_PLAN_HISTORY.md`` H-25: items are exact-match
questions with a single correct answer, drawn deterministically from
content the corpus already contains; reasoning-accuracy compares the model's answer
*accuracy* under the codec-on condition against codec-off, not against
each other turn for turn -- the gate asks whether compression measurably
changes how well the model reasons about what it was shown, matching
human-bug-catch's "within Npp" framing one section up.

``docs/system-design.md`` §2.6 names milestone's ``ReplayClient`` live capture
as the intended source for a real reasoning-accuracy response set; :class:`ReasoningClient` and
:func:`capture_live_responses` are that path for this gate, mirroring
``laconic.replay.engine.ReplayClient``. CI, and :func:`measure`, only
ever read a *committed* response fixture -- there is no live-mode
argument anywhere in this module's public API, so "CI must ... reject
live mode" holds structurally for reasoning-accuracy, not by a runtime check that could
be bypassed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from laconic.gates.protocol import GateResult
from laconic.replay.corpus import JsonValue, iter_records
from laconic.replay.engine import Provenance, find_baseline_transcripts, now_iso

#: `def <name>_<N>(value: int) -> int:\n    return value + <N>` -- the
#: synthetic function shape every `tests/corpus/*.jsonl` Read/Bash/Grep
#: result already contains, per `tests/corpus/README.md`.
_FUNCTION_PATTERN = re.compile(r"def (\w+_\d+)\(value: int\) -> int:\n\s+return value \+ (\d+)")

#: The benchmark stays small and deterministic: the first this many
#: distinct function names found, in file-then-position order.
DEFAULT_ITEM_LIMIT = 50


class ReasoningAccuracyFixtureError(ValueError):
    """Raised when a committed reasoning-accuracy response fixture is malformed or does
    not cover every extracted item for both conditions."""


@dataclass(frozen=True, slots=True)
class ReasoningItem:
    """One exact-match reasoning question with a single correct answer."""

    item_id: str
    question: str
    expected_answer: str


@dataclass(frozen=True, slots=True)
class ReasoningResponse:
    """One model answer to one :class:`ReasoningItem`, under one condition."""

    item_id: str
    condition: Literal["off", "on"]
    answer: str
    provenance: Provenance


def extract_items(
    paths: Sequence[Path], *, limit: int = DEFAULT_ITEM_LIMIT
) -> tuple[ReasoningItem, ...]:
    """Deterministically derive up to ``limit`` benchmark items from the
    corpus's own tool-result content.

    Requires no hand-authoring and regenerates automatically if the
    corpus changes: an item is "what integer does `<name>` add to its
    argument?", answerable exactly from the function's own visible body.
    """
    items: list[ReasoningItem] = []
    seen: set[str] = set()
    for baseline in find_baseline_transcripts(paths):
        for _, record in iter_records(baseline):
            if record is None or record.get("type") != "user":
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
                body = block.get("content")
                if not isinstance(body, str):
                    continue
                for match in _FUNCTION_PATTERN.finditer(body):
                    name, addend = match.group(1), match.group(2)
                    if name in seen:
                        continue
                    seen.add(name)
                    items.append(
                        ReasoningItem(
                            item_id=name,
                            question=f"What integer does `{name}` add to its argument?",
                            expected_answer=addend,
                        )
                    )
                    if len(items) >= limit:
                        return tuple(items)
    return tuple(items)


def accuracy(
    items: Sequence[ReasoningItem],
    responses: Sequence[ReasoningResponse],
    *,
    condition: Literal["off", "on"],
) -> float:
    """Fraction of ``items`` (0-100) whose ``condition`` response's
    ``answer`` exactly matches ``expected_answer``.

    Raises :class:`ReasoningAccuracyFixtureError` when a response set is missing an
    item's answer for ``condition`` entirely -- silently scoring an
    unanswered item as wrong would conflate "the model got this wrong"
    with "this item was never asked," which are different findings.
    """
    by_item = {
        response.item_id: response for response in responses if response.condition == condition
    }
    missing = [item.item_id for item in items if item.item_id not in by_item]
    if missing:
        raise ReasoningAccuracyFixtureError(
            f"no {condition!r}-condition response for item(s): {', '.join(missing[:5])}"
            + (f" (+{len(missing) - 5} more)" if len(missing) > 5 else "")
        )
    if not items:
        return 100.0
    correct = sum(1 for item in items if by_item[item.item_id].answer == item.expected_answer)
    return 100 * correct / len(items)


def load_responses(path: Path) -> tuple[ReasoningResponse, ...]:
    """Load a committed, provenance-tagged reasoning-accuracy response fixture.

    Raises :class:`ReasoningAccuracyFixtureError` for a missing file, an unparseable or
    non-object line, a record with no `provenance` block, or a duplicate
    ``(item_id, condition)`` pair -- matching
    `laconic.replay.engine.load_recorded_response`'s posture that an
    unprovenanced, absent, malformed, or conflicting fixture record
    cannot silently stand in for a real one.
    """
    if not path.is_file():
        raise ReasoningAccuracyFixtureError(
            f"no committed reasoning-accuracy response fixture at {path}"
        )
    responses: list[ReasoningResponse] = []
    seen: set[tuple[str, str]] = set()
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as error:
            raise ReasoningAccuracyFixtureError(
                f"{path}:{line_number}: not valid JSON: {error}"
            ) from error
        if not isinstance(parsed, dict):
            raise ReasoningAccuracyFixtureError(
                f"{path}:{line_number}: malformed reasoning-accuracy response record"
            )
        record = cast("dict[str, JsonValue]", parsed)
        item_id = record.get("item_id")
        condition = record.get("condition")
        answer = record.get("answer")
        provenance = Provenance.from_record(record.get("provenance"))
        valid_condition = isinstance(condition, str) and condition in ("off", "on")
        if not (isinstance(item_id, str) and valid_condition and isinstance(answer, str)):
            raise ReasoningAccuracyFixtureError(
                f"{path}:{line_number}: malformed reasoning-accuracy response record"
            )
        if (item_id, condition) in seen:
            raise ReasoningAccuracyFixtureError(
                f"{path}:{line_number}: duplicate response for item {item_id!r}, "
                f"condition {condition!r}"
            )
        condition_literal = cast("Literal['off', 'on']", condition)
        seen.add((item_id, condition_literal))
        if provenance is None:
            raise ReasoningAccuracyFixtureError(
                f"{path}:{line_number}: response carries no `provenance` block"
            )
        responses.append(
            ReasoningResponse(
                item_id=item_id,
                condition=condition_literal,
                answer=answer,
                provenance=provenance,
            )
        )
    return tuple(responses)


def responses_path_for(paths: Sequence[Path]) -> Path:
    """Return the corpus-wide reasoning-accuracy response fixture path."""
    if len(paths) != 1:
        raise ReasoningAccuracyFixtureError("reasoning-accuracy requires exactly one corpus root")
    return Path(paths[0]) / "reasoning_accuracy_responses.ndjson"


def measure(paths: Sequence[Path]) -> GateResult:
    """Measure reasoning-accuracy across the corpus under ``paths``.

    Reads only a committed response fixture -- see the module docstring
    for why this gate has no live-mode argument at all.
    """
    items = extract_items(paths)
    if not items:
        return GateResult.measured(
            "reasoning-accuracy", 0.0, detail="no benchmark items found in the corpus"
        )
    fixture = responses_path_for(paths)
    responses = load_responses(fixture)
    off_accuracy = accuracy(items, responses, condition="off")
    on_accuracy = accuracy(items, responses, condition="on")
    delta_pp = abs(on_accuracy - off_accuracy)
    detail = (
        f"{len(items)} item(s): codec-off accuracy {off_accuracy:.2f}%, "
        f"codec-on accuracy {on_accuracy:.2f}%"
    )
    return GateResult.measured("reasoning-accuracy", delta_pp, detail=detail)


class ReasoningClient(Protocol):
    """A real model client for live reasoning-accuracy capture.

    No concrete implementation ships, matching
    ``laconic.replay.engine.ReplayClient``'s precedent -- live capture is
    opt-in tooling a caller runs separately from ``laconic research gates``, never
    a code path the gate itself can reach.
    """

    def answer(self, *, item: ReasoningItem, context: str, model: str) -> str:
        """Return the model's answer to ``item.question`` given
        ``context`` -- the raw or codec-encoded source text the item's
        function definition was drawn from."""
        ...


def capture_live_responses(
    items: Sequence[ReasoningItem],
    *,
    contexts: dict[str, tuple[str, str]],
    client: ReasoningClient,
    model: str,
    run_id: str | None = None,
) -> tuple[ReasoningResponse, ...]:
    """Capture a real reasoning-accuracy response set via ``client``, one call per item
    per condition.

    ``contexts`` maps an item id to its ``(raw, encoded)`` source text.
    Every response is tagged ``provenance.source == "live"`` so a
    captured fixture is never mistaken for :func:`generate_synthetic_responses`'s
    output once committed.
    """
    captured_at = now_iso()
    responses: list[ReasoningResponse] = []
    conditions: tuple[tuple[Literal["off", "on"], str], ...]
    for item in items:
        raw_context, encoded_context = contexts[item.item_id]
        conditions = (("off", raw_context), ("on", encoded_context))
        for condition, context in conditions:
            answer = client.answer(item=item, context=context, model=model)
            provenance = Provenance(
                source="live", model=model, captured_at=captured_at, run_id=run_id
            )
            responses.append(
                ReasoningResponse(
                    item_id=item.item_id, condition=condition, answer=answer, provenance=provenance
                )
            )
    return tuple(responses)


def generate_synthetic_responses(
    items: Sequence[ReasoningItem], *, model: str
) -> tuple[ReasoningResponse, ...]:
    """Derive a committed, synthesized (not live-captured) reasoning-accuracy response
    set matching a correctly-behaving codec: every item answered
    correctly under both conditions, so the shipped fixture reports the
    accuracy delta a working codec should produce (0.00pp) rather than
    fabricating a number no capture ever measured.

    Matches the same "synthesized, not live-captured" precedent
    ``.docs/DEVELOPMENT_PLAN_HISTORY.md`` H-23/H-25 record for net-cost/action-equivalence's
    corpus fixtures. A negative control proving reasoning-accuracy can detect a real
    accuracy gap perturbs one answer in a scratch response set built
    directly in ``tests/test_gates.py``, never this fixture.
    """
    captured_at = "2026-07-28T00:00:00Z"
    provenance = Provenance(source="recorded", model=model, captured_at=captured_at)
    responses: list[ReasoningResponse] = []
    conditions: tuple[Literal["off", "on"], Literal["off", "on"]] = ("off", "on")
    for item in items:
        for condition in conditions:
            responses.append(
                ReasoningResponse(
                    item_id=item.item_id,
                    condition=condition,
                    answer=item.expected_answer,
                    provenance=provenance,
                )
            )
    return tuple(responses)


def write_responses(path: Path, responses: Sequence[ReasoningResponse]) -> None:
    """Write ``responses`` as a committed reasoning-accuracy response fixture."""
    lines = [
        json.dumps(
            {
                "item_id": response.item_id,
                "condition": response.condition,
                "answer": response.answer,
                "provenance": response.provenance.to_record(),
            }
        )
        for response in responses
    ]
    path.write_text("\n".join(lines) + "\n")
