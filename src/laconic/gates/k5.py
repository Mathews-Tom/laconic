"""K5: exact-match reasoning benchmark, codec on vs off.

``docs/overview.md`` §6.3: "Exact-match reasoning benchmark, codec on vs
off ... within 2pp," kill "beyond → format tax confirmed on our stack."
Per ``.docs/DEVELOPMENT_PLAN_HISTORY.md`` H-25: items are exact-match
questions with a single correct answer, drawn deterministically from
content the corpus already contains; K5 compares the model's answer
*accuracy* under the codec-on condition against codec-off, not against
each other turn for turn -- the gate asks whether compression measurably
changes how well the model reasons about what it was shown, matching
K3's "within Npp" framing one section up.

``docs/system-design.md`` §2.6 names M8's ``ReplayClient`` live capture
as the intended source for a real K5 response set; :class:`K5Client` and
:func:`capture_live_responses` are that path for this gate, mirroring
``laconic.replay.engine.ReplayClient``. CI, and :func:`measure`, only
ever read a *committed* response fixture -- there is no live-mode
argument anywhere in this module's public API, so "CI must ... reject
live mode" holds structurally for K5, not by a runtime check that could
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


class K5FixtureError(ValueError):
    """Raised when a committed K5 response fixture is malformed or does
    not cover every extracted item for both conditions."""


@dataclass(frozen=True, slots=True)
class K5Item:
    """One exact-match reasoning question with a single correct answer."""

    item_id: str
    question: str
    expected_answer: str


@dataclass(frozen=True, slots=True)
class K5Response:
    """One model answer to one :class:`K5Item`, under one condition."""

    item_id: str
    condition: Literal["off", "on"]
    answer: str
    provenance: Provenance


def extract_items(paths: Sequence[Path], *, limit: int = DEFAULT_ITEM_LIMIT) -> tuple[K5Item, ...]:
    """Deterministically derive up to ``limit`` benchmark items from the
    corpus's own tool-result content.

    Requires no hand-authoring and regenerates automatically if the
    corpus changes: an item is "what integer does `<name>` add to its
    argument?", answerable exactly from the function's own visible body.
    """
    items: list[K5Item] = []
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
                        K5Item(
                            item_id=name,
                            question=f"What integer does `{name}` add to its argument?",
                            expected_answer=addend,
                        )
                    )
                    if len(items) >= limit:
                        return tuple(items)
    return tuple(items)


def accuracy(
    items: Sequence[K5Item], responses: Sequence[K5Response], *, condition: Literal["off", "on"]
) -> float:
    """Fraction of ``items`` (0-100) whose ``condition`` response's
    ``answer`` exactly matches ``expected_answer``.

    Raises :class:`K5FixtureError` when a response set is missing an
    item's answer for ``condition`` entirely -- silently scoring an
    unanswered item as wrong would conflate "the model got this wrong"
    with "this item was never asked," which are different findings.
    """
    by_item = {
        response.item_id: response for response in responses if response.condition == condition
    }
    missing = [item.item_id for item in items if item.item_id not in by_item]
    if missing:
        raise K5FixtureError(
            f"no {condition!r}-condition response for item(s): {', '.join(missing[:5])}"
            + (f" (+{len(missing) - 5} more)" if len(missing) > 5 else "")
        )
    if not items:
        return 100.0
    correct = sum(1 for item in items if by_item[item.item_id].answer == item.expected_answer)
    return 100 * correct / len(items)


def load_responses(path: Path) -> tuple[K5Response, ...]:
    """Load a committed, provenance-tagged K5 response fixture.

    Raises :class:`K5FixtureError` for a missing file, an unparseable or
    non-object line, a record with no `provenance` block, or a duplicate
    ``(item_id, condition)`` pair -- matching
    `laconic.replay.engine.load_recorded_response`'s posture that an
    unprovenanced, absent, malformed, or conflicting fixture record
    cannot silently stand in for a real one.
    """
    if not path.is_file():
        raise K5FixtureError(f"no committed K5 response fixture at {path}")
    responses: list[K5Response] = []
    seen: set[tuple[str, str]] = set()
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as error:
            raise K5FixtureError(f"{path}:{line_number}: not valid JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise K5FixtureError(f"{path}:{line_number}: malformed K5 response record")
        record = cast("dict[str, JsonValue]", parsed)
        item_id = record.get("item_id")
        condition = record.get("condition")
        answer = record.get("answer")
        provenance = Provenance.from_record(record.get("provenance"))
        valid_condition = isinstance(condition, str) and condition in ("off", "on")
        if not (isinstance(item_id, str) and valid_condition and isinstance(answer, str)):
            raise K5FixtureError(f"{path}:{line_number}: malformed K5 response record")
        if (item_id, condition) in seen:
            raise K5FixtureError(
                f"{path}:{line_number}: duplicate response for item {item_id!r}, "
                f"condition {condition!r}"
            )
        condition_literal = cast("Literal['off', 'on']", condition)
        seen.add((item_id, condition_literal))
        if provenance is None:
            raise K5FixtureError(f"{path}:{line_number}: response carries no `provenance` block")
        responses.append(
            K5Response(
                item_id=item_id,
                condition=condition_literal,
                answer=answer,
                provenance=provenance,
            )
        )
    return tuple(responses)


def responses_path_for(paths: Sequence[Path]) -> Path:
    """The committed K5 response fixture path for a corpus root.

    One fixture per corpus root, named ``k5_responses.ndjson`` alongside
    the transcripts -- the benchmark items are corpus-wide, not
    per-session, so there is one response set, not one per baseline.
    The extension is deliberately not ``.jsonl``:
    ``laconic.replay.corpus.TRANSCRIPT_GLOB`` matches every ``*.jsonl``
    file in a corpus tree, and this fixture is not a session
    transcript -- ``.ndjson`` keeps it out of every corpus scan (`laconic
    measure`, K1, K2, K4, and this module's own :func:`extract_items`)
    without any of them needing to know K5's fixture exists.
    """
    if len(paths) != 1:
        raise K5FixtureError("K5 requires exactly one corpus root")
    return Path(paths[0]) / "k5_responses.ndjson"


def measure(paths: Sequence[Path]) -> GateResult:
    """Measure K5 across the corpus under ``paths``.

    Reads only a committed response fixture -- see the module docstring
    for why this gate has no live-mode argument at all.
    """
    items = extract_items(paths)
    if not items:
        return GateResult.measured("K5", 0.0, detail="no benchmark items found in the corpus")
    fixture = responses_path_for(paths)
    responses = load_responses(fixture)
    off_accuracy = accuracy(items, responses, condition="off")
    on_accuracy = accuracy(items, responses, condition="on")
    delta_pp = abs(on_accuracy - off_accuracy)
    detail = (
        f"{len(items)} item(s): codec-off accuracy {off_accuracy:.2f}%, "
        f"codec-on accuracy {on_accuracy:.2f}%"
    )
    return GateResult.measured("K5", delta_pp, detail=detail)


class K5Client(Protocol):
    """A real model client for live K5 capture.

    No concrete implementation ships, matching
    ``laconic.replay.engine.ReplayClient``'s precedent -- live capture is
    opt-in tooling a caller runs separately from ``laconic gates``, never
    a code path the gate itself can reach.
    """

    def answer(self, *, item: K5Item, context: str, model: str) -> str:
        """Return the model's answer to ``item.question`` given
        ``context`` -- the raw or codec-encoded source text the item's
        function definition was drawn from."""
        ...


def capture_live_responses(
    items: Sequence[K5Item],
    *,
    contexts: dict[str, tuple[str, str]],
    client: K5Client,
    model: str,
    run_id: str | None = None,
) -> tuple[K5Response, ...]:
    """Capture a real K5 response set via ``client``, one call per item
    per condition.

    ``contexts`` maps an item id to its ``(raw, encoded)`` source text.
    Every response is tagged ``provenance.source == "live"`` so a
    captured fixture is never mistaken for :func:`generate_synthetic_responses`'s
    output once committed.
    """
    captured_at = now_iso()
    responses: list[K5Response] = []
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
                K5Response(
                    item_id=item.item_id, condition=condition, answer=answer, provenance=provenance
                )
            )
    return tuple(responses)


def generate_synthetic_responses(items: Sequence[K5Item], *, model: str) -> tuple[K5Response, ...]:
    """Derive a committed, synthesized (not live-captured) K5 response
    set matching a correctly-behaving codec: every item answered
    correctly under both conditions, so the shipped fixture reports the
    accuracy delta a working codec should produce (0.00pp) rather than
    fabricating a number no capture ever measured.

    Matches the same "synthesized, not live-captured" precedent
    ``.docs/DEVELOPMENT_PLAN_HISTORY.md`` H-23/H-25 record for K1/K2's
    corpus fixtures. A negative control proving K5 can detect a real
    accuracy gap perturbs one answer in a scratch response set built
    directly in ``tests/test_gates.py``, never this fixture.
    """
    captured_at = "2026-07-28T00:00:00Z"
    provenance = Provenance(source="recorded", model=model, captured_at=captured_at)
    responses: list[K5Response] = []
    conditions: tuple[Literal["off", "on"], Literal["off", "on"]] = ("off", "on")
    for item in items:
        for condition in conditions:
            responses.append(
                K5Response(
                    item_id=item.item_id,
                    condition=condition,
                    answer=item.expected_answer,
                    provenance=provenance,
                )
            )
    return tuple(responses)


def write_responses(path: Path, responses: Sequence[K5Response]) -> None:
    """Write ``responses`` as a committed K5 response fixture."""
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
