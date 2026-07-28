"""Replay engine: counterfactual cost and action equivalence.

``docs/system-design.md`` §2.6 names this the evaluation engine -- the
reason the gates in its §4 are checkable rather than aspirational. It
walks a real session transcript turn by turn, optionally applies the
observation codec, and reports action equivalence and cost.

Two replay modes exist because a live model call has no place in CI
(``docs/system-design.md`` §2.6/§8.3; ``DEVELOPMENT_PLAN.md`` §2's
``> GAP:`` on exactly this):

- ``"recorded"`` (the CI-safe default) reads a *committed*,
  provenance-tagged recorded-response transcript that a prior live run
  already produced. It never calls a model.
- ``"live"`` calls a real model through an injected :class:`ReplayClient`,
  requires an explicit model identifier and a hard per-run USD cost cap,
  and writes every response it receives to an artifact file tagged with
  its own provenance, so a human can review and commit it as tomorrow's
  ``"recorded"`` fixture.

A recorded-response fixture is an ordinary transcript in the same
``*.jsonl`` schema ``tests/corpus/README.md`` documents, with two
assistant-record extensions: a top-level ``provenance`` object naming
where the turn's content came from, and an optional top-level
``induced: true`` marking a turn the codec-encoded observation provoked
that the baseline session never took at all. Every non-induced turn pairs,
in order, with the baseline transcript's own turn at the same position --
that pairing is what makes structural equivalence
(:mod:`laconic.replay.equivalence`) a plain zip, and what makes net cost
(:meth:`NetCostReport.net_savings_usd`) a plain subtraction: the
recorded-response fixture's own recorded cost already includes whatever
extra turns the induced follow-ups cost.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from laconic.costs import CostBreakdown, ModelUsage, session_cost
from laconic.replay.corpus import (
    REPLAY_ARTIFACT_SUFFIX as RECORDED_RESPONSE_SUFFIX,
)
from laconic.replay.corpus import (
    JsonValue,
    MalformedRecordError,
    find_transcripts,
    iter_records,
    scan,
    turn_usage,
)

#: The committed recorded-response fixture paired with a baseline transcript
#: lives beside it, same stem, this suffix instead of a bare ``.jsonl``. A
#: re-export of :data:`laconic.replay.corpus.REPLAY_ARTIFACT_SUFFIX` -- the
#: two names describe the same fact from two vantage points (corpus
#: discovery vs. fixture naming) and must never drift apart, so this module
#: does not define its own copy.

#: A same-content recomputation must agree to within this many USD --
#: floating point noise only, not a real accounting tolerance.
BASELINE_TOLERANCE_USD = 1e-9


class ReplayError(RuntimeError):
    """Base class for every replay engine failure."""


class MissingRecordedResponseError(ReplayError):
    """Raised when ``codec="on"`` replay in ``mode="recorded"`` has no
    committed, fully provenance-tagged fixture for a baseline transcript.

    There is deliberately no fallback to a gross, codec-only figure here:
    a session with nothing to net induced-read cost against cannot honestly
    report savings at all, per ``docs/system-design.md``'s "Honest
    measurement" constraint.
    """


class BaselineMismatchError(ReplayError):
    """Raised by :func:`assert_baseline` when ``codec="off"`` replay's own
    turn-by-turn accumulation disagrees with the transcript's independently
    recomputed cost by more than :data:`BASELINE_TOLERANCE_USD`."""


class UnknownTurnIndexError(ReplayError):
    """Raised when ``observations`` names a turn index ``baseline`` never
    had, checked before any client call so a typo cannot spend money
    first and fail after."""


class MismatchedSessionError(ReplayError):
    """Raised by :func:`net_cost` when ``session.baseline`` does not equal
    the ``baseline`` it was called with -- a mispaired call would
    otherwise compute a plausible-looking but meaningless savings figure
    across two unrelated sessions."""


@dataclass(frozen=True, slots=True)
class RecordedAction:
    """One ``tool_use`` action exactly as a transcript recorded it."""

    tool_use_id: str
    tool_name: str
    tool_input: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class TurnUsage:
    """One assistant record's own token counters and billing model."""

    model: str
    input_tokens: int
    cache_read: int
    cache_write: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where one recorded-response turn's content came from.

    ``"live"`` names a turn actually captured from a real model call via
    :func:`replay_live`; ``"recorded"`` names one hand-authored or scripted
    as a committed stand-in before any live capture exists. A baseline
    transcript -- a real session as it actually happened -- carries no
    ``provenance`` at all; only a recorded-response *fixture* does.
    """

    source: Literal["recorded", "live"]
    model: str
    captured_at: str
    run_id: str | None = None

    @staticmethod
    def from_record(value: JsonValue) -> Provenance | None:
        """Parse a ``provenance`` block, or ``None`` if absent or malformed.

        A malformed ``provenance`` block is treated exactly like a missing
        one -- :func:`load_recorded_response` then rejects the whole
        fixture via :class:`MissingRecordedResponseError` rather than
        silently trusting an unprovenanced turn.
        """
        if not isinstance(value, dict):
            return None
        source = value.get("source")
        model = value.get("model")
        captured_at = value.get("captured_at")
        valid_source = isinstance(source, str) and source in ("recorded", "live")
        if not valid_source or not isinstance(model, str):
            return None
        if not isinstance(captured_at, str):
            return None
        run_id = value.get("run_id")
        return Provenance(
            source=cast(Literal["recorded", "live"], source),
            model=model,
            captured_at=captured_at,
            run_id=run_id if isinstance(run_id, str) else None,
        )

    def to_record(self) -> dict[str, JsonValue]:
        """The inverse of :meth:`from_record`, for artifact capture."""
        record: dict[str, JsonValue] = {
            "source": self.source,
            "model": self.model,
            "captured_at": self.captured_at,
        }
        if self.run_id is not None:
            record["run_id"] = self.run_id
        return record


@dataclass(frozen=True, slots=True)
class ReplayTurn:
    """One assistant turn, replayed: its actions, its usage, and -- for a
    recorded-response fixture -- whether it is an induced follow-up the
    baseline never took."""

    index: int
    actions: tuple[RecordedAction, ...]
    usage: TurnUsage | None
    induced: bool
    provenance: Provenance | None


def _record_actions(message: Mapping[str, JsonValue]) -> tuple[RecordedAction, ...]:
    content = message.get("content")
    if not isinstance(content, list):
        return ()
    actions: list[RecordedAction] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        tool_input = block.get("input")
        tool_id = block.get("id")
        if isinstance(name, str) and isinstance(tool_input, dict) and isinstance(tool_id, str):
            actions.append(
                RecordedAction(tool_use_id=tool_id, tool_name=name, tool_input=tool_input)
            )
    return tuple(actions)


def iter_turns(path: Path) -> Iterator[ReplayTurn]:
    """Yield one :class:`ReplayTurn` per assistant record in ``path``, in
    file order.

    ``index`` counts every assistant record seen, 0-based -- the ordinal a
    baseline and its paired recorded-response fixture must agree on for
    non-induced turns to line up positionally.
    """
    index = 0
    for line_number, record in iter_records(path):
        if record is None or record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        raw_usage = message.get("usage")
        if raw_usage is not None and not isinstance(raw_usage, dict):
            raise MalformedRecordError(
                f"{path}:{line_number}: usage is not an object: {raw_usage!r}"
            )
        usage: TurnUsage | None = None
        counts = turn_usage(raw_usage, origin=f"{path}:{line_number}")
        if counts is not None:
            model = message.get("model")
            usage = TurnUsage(model=model if isinstance(model, str) else "unknown", **counts)
        yield ReplayTurn(
            index=index,
            actions=_record_actions(message),
            usage=usage,
            induced=record.get("induced") is True,
            provenance=Provenance.from_record(record.get("provenance")),
        )
        index += 1


def recorded_response_path(baseline: Path) -> Path:
    """Return the committed recorded-response fixture path paired with
    ``baseline``, by naming convention alone: same directory and stem, with
    :data:`RECORDED_RESPONSE_SUFFIX` instead of a bare ``.jsonl``.
    """
    if baseline.suffix != ".jsonl":
        raise ValueError(f"not a transcript path: {baseline}")
    return baseline.with_name(baseline.stem + RECORDED_RESPONSE_SUFFIX)


def find_baseline_transcripts(paths: Sequence[Path]) -> list[Path]:
    """Return every session transcript under ``paths``.

    An alias for :func:`~laconic.replay.corpus.find_transcripts`, named
    for replay call sites: ``find_transcripts`` itself now excludes every
    committed recorded-response fixture
    (:data:`~laconic.replay.corpus.REPLAY_ARTIFACT_SUFFIX`), so a fixture
    can never be scanned as a baseline session anywhere in this package --
    including ``laconic measure``, which calls ``find_transcripts``
    directly and would otherwise double-count a fixture committed beside
    its baseline as an extra session.
    """
    return find_transcripts(list(paths))


@dataclass(frozen=True, slots=True)
class SessionCost:
    """A session's cost, read from its own recorded usage."""

    turns: int
    cost: CostBreakdown


def session_cost_of(path: Path) -> SessionCost:
    """Reproduce ``path``'s own recorded cost via the exact accounting
    ``laconic measure`` uses -- the independent reference
    :func:`assert_baseline` checks the replay engine's own turn-by-turn
    accumulation against.
    """
    result = scan([path])
    return SessionCost(
        turns=sum(u.turns for u in result.usage.values()), cost=session_cost(result.usage)
    )


@dataclass(frozen=True, slots=True)
class BaselineSession:
    """``codec="off"`` replay of one transcript: the replay engine's own
    turn-by-turn accumulation of exactly what the session already cost."""

    path: Path
    cost: SessionCost


def replay_off(paths: Sequence[Path]) -> tuple[BaselineSession, ...]:
    """Replay every transcript under ``paths`` with the codec disabled.

    This walks each transcript turn by turn via :func:`iter_turns`,
    accumulating usage independently of :func:`laconic.replay.corpus.scan`
    -- codec="off" never transforms an observation, so the two
    accumulations must agree; :func:`assert_baseline` is the check that
    they do.
    """
    sessions: list[BaselineSession] = []
    for path in find_baseline_transcripts(paths):
        usage: dict[str, ModelUsage] = {}
        turns = 0
        for turn in iter_turns(path):
            if turn.usage is None:
                continue
            usage[turn.usage.model] = usage.get(turn.usage.model, ModelUsage()).add_turn(
                input_tokens=turn.usage.input_tokens,
                cache_read=turn.usage.cache_read,
                cache_write=turn.usage.cache_write,
                output_tokens=turn.usage.output_tokens,
            )
            turns += 1
        sessions.append(
            BaselineSession(path=path, cost=SessionCost(turns=turns, cost=session_cost(usage)))
        )
    return tuple(sessions)


def assert_baseline(
    sessions: Sequence[BaselineSession], *, tolerance_usd: float = BASELINE_TOLERANCE_USD
) -> None:
    """Verify every session in ``sessions`` reproduces its own recorded
    cost within ``tolerance_usd``.

    ``sessions`` came from :func:`replay_off`'s independent, turn-by-turn
    walk; this cross-checks each one against :func:`session_cost_of`'s
    whole-transcript scan -- two separately written accumulations that
    must agree exactly for the same content, since neither applies the
    codec. A drift here is a defect in one of the two implementations, not
    a codec effect.
    """
    for entry in sessions:
        reference = session_cost_of(entry.path).cost.total
        replayed = entry.cost.cost.total
        if abs(replayed - reference) > tolerance_usd:
            raise BaselineMismatchError(
                f"{entry.path}: replayed baseline cost {replayed:.6f} does not reproduce "
                f"recomputed cost {reference:.6f} within {tolerance_usd} USD"
            )


@dataclass(frozen=True, slots=True)
class RecordedResponseSession:
    """A committed, provenance-tagged transcript pairing a baseline session
    with what actually happened, turn for turn, once the codec was active.

    Every non-induced turn corresponds, in order, to the baseline's own
    turn at the same position; :attr:`induced_turns` names the extra
    follow-up reads the codec's elision provoked.
    """

    baseline: Path
    fixture: Path
    turns: tuple[ReplayTurn, ...]

    @property
    def cost(self) -> SessionCost:
        """This fixture's own recorded cost, every turn included -- the
        induced ones' cost is not a separate figure, it is already inside
        this total, which is exactly what makes net cost a subtraction."""
        usage: dict[str, ModelUsage] = {}
        turns = 0
        for turn in self.turns:
            if turn.usage is None:
                continue
            usage[turn.usage.model] = usage.get(turn.usage.model, ModelUsage()).add_turn(
                input_tokens=turn.usage.input_tokens,
                cache_read=turn.usage.cache_read,
                cache_write=turn.usage.cache_write,
                output_tokens=turn.usage.output_tokens,
            )
            turns += 1
        return SessionCost(turns=turns, cost=session_cost(usage))

    @property
    def induced_turns(self) -> tuple[ReplayTurn, ...]:
        return tuple(turn for turn in self.turns if turn.induced)

    @property
    def non_induced_actions(self) -> tuple[RecordedAction, ...]:
        """The terminal action of every non-induced turn, in order -- what
        :mod:`laconic.replay.equivalence` pairs against the baseline's own
        actions. A turn with no action (pure prose, or an induced turn
        with nothing decisive to compare) contributes nothing here.
        """
        actions: list[RecordedAction] = []
        for turn in self.turns:
            if turn.induced or not turn.actions:
                continue
            actions.append(turn.actions[-1])
        return tuple(actions)


def load_recorded_response(baseline: Path) -> RecordedResponseSession:
    """Load the committed recorded-response fixture paired with ``baseline``.

    Raises :class:`MissingRecordedResponseError` when no fixture file
    exists at :func:`recorded_response_path`, or when any assistant turn in
    one that does exist carries no ``provenance`` block -- a committed
    recorded-response fixture must tag every turn's origin, with no
    exception, so a fixture can never be mistaken for a live capture (or
    vice versa) later.
    """
    fixture = recorded_response_path(baseline)
    if not fixture.is_file():
        raise MissingRecordedResponseError(
            f"no committed recorded-response fixture at {fixture} for baseline {baseline}; "
            'codec="on" replay in mode="recorded" requires one -- see replay_live() to capture it'
        )
    turns = tuple(iter_turns(fixture))
    for turn in turns:
        if turn.provenance is None:
            raise MissingRecordedResponseError(
                f"{fixture}: assistant turn {turn.index} carries no `provenance` block; "
                "every turn in a committed recorded-response fixture must be provenance-tagged"
            )
    return RecordedResponseSession(baseline=baseline, fixture=fixture, turns=turns)


@dataclass(frozen=True, slots=True)
class NetCostReport:
    """The only savings figure this package can produce: gross reduction
    netted against every induced turn's own real cost.

    There is no method anywhere in this module that returns a savings
    number without this subtraction -- ``docs/system-design.md``'s
    "Honest measurement" constraint makes that a structural property, not
    a reporting convention a caller could opt out of: :attr:`codec_on`
    already includes every induced turn's cost (it is the recorded-response
    fixture's own total, ``RecordedResponseSession.cost``), so
    :attr:`net_savings_usd` cannot be computed as a gross figure even by
    accident.
    """

    baseline: SessionCost
    codec_on: SessionCost
    induced_turns: int
    induced_cost_usd: float

    @property
    def net_savings_usd(self) -> float:
        return self.baseline.cost.total - self.codec_on.cost.total

    @property
    def net_savings_pct(self) -> float:
        """Savings as a percentage of baseline cost; ``0.0`` for a
        zero-cost baseline rather than a division error -- a session that
        cost nothing had nothing to save."""
        if self.baseline.cost.total <= 0:
            return 0.0
        return 100 * self.net_savings_usd / self.baseline.cost.total


def net_cost(baseline: Path, session: RecordedResponseSession) -> NetCostReport:
    """Compute :class:`NetCostReport` for one baseline transcript and its
    paired recorded-response ``session`` (from :func:`load_recorded_response`
    or :func:`replay_live`).

    ``induced_cost_usd`` is reported separately for transparency -- so a
    caller can see how much of the difference is induced-read overhead --
    but it is never the only netting: :attr:`NetCostReport.net_savings_usd`
    subtracts ``session``'s full recorded cost, not ``baseline`` minus
    ``induced_cost_usd`` alone, so a bug in induced-turn detection could
    never silently inflate a reported saving.

    Raises :class:`MismatchedSessionError` when ``session.baseline`` is not
    ``baseline`` -- a mispaired call would otherwise report a confident,
    meaningless number computed across two unrelated sessions.
    """
    if session.baseline != baseline:
        raise MismatchedSessionError(
            f"net_cost: baseline {baseline} does not match session.baseline {session.baseline}"
        )
    baseline_cost = session_cost_of(baseline)
    induced_usage: dict[str, ModelUsage] = {}
    for turn in session.induced_turns:
        if turn.usage is None:
            continue
        existing = induced_usage.get(turn.usage.model, ModelUsage())
        induced_usage[turn.usage.model] = existing.add_turn(
            input_tokens=turn.usage.input_tokens,
            cache_read=turn.usage.cache_read,
            cache_write=turn.usage.cache_write,
            output_tokens=turn.usage.output_tokens,
        )
    return NetCostReport(
        baseline=baseline_cost,
        codec_on=session.cost,
        induced_turns=len(session.induced_turns),
        induced_cost_usd=session_cost(induced_usage).total,
    )


def replay_on(paths: Sequence[Path]) -> tuple[tuple[Path, NetCostReport], ...]:
    """``codec="on"`` replay in ``mode="recorded"`` for every baseline
    transcript found under ``paths``.

    Every baseline transcript under ``paths`` must have a committed
    recorded-response fixture (:func:`recorded_response_path`) or this
    raises :class:`MissingRecordedResponseError` naming the first one
    missing one -- a partial report that silently skipped a session with
    no fixture would look like a clean corpus-wide result when it is not.
    A live-mode net-cost report is composed by the caller from
    :func:`replay_live` and :func:`net_cost` directly; this function is the
    CI-safe, no-model-call aggregation path.
    """
    reports: list[tuple[Path, NetCostReport]] = []
    for baseline in find_baseline_transcripts(paths):
        session = load_recorded_response(baseline)
        reports.append((baseline, net_cost(baseline, session)))
    return tuple(reports)


@dataclass(frozen=True, slots=True)
class ReplayTurnCapture:
    """One turn a :class:`ReplayClient` reports back from a live call: the
    action it took and the real token usage that call billed."""

    action: RecordedAction
    usage: TurnUsage


class ReplayClient(Protocol):
    """A real model client the live replay path calls.

    No concrete implementation ships in this package: live replay is
    opt-in by design, and this repository's dependencies stay minimal. A
    caller wires a concrete client -- typically a thin wrapper over
    whichever provider SDK ``~/.laconic/config.toml``'s ``[replay]``
    section already names a model for.
    """

    def respond(
        self, *, prefix: Sequence[ReplayTurn], observation: str, model: str
    ) -> Sequence[ReplayTurnCapture]:
        """Return every turn taken in response to ``observation``, in
        order. The **last** entry is the terminal action compared against
        the baseline's recorded action; every earlier one is an induced
        follow-up the client chose to take first."""
        ...


class LiveModeConfigError(ReplayError):
    """Raised when live replay is requested without its full, explicit
    opt-in configuration: a model identifier, a positive cost cap, and a
    client all present together, no defaults."""


class CostCapExceededError(ReplayError):
    """Raised when a live replay run would spend past its configured cap.

    Raised *after* the call that would breach it has already been billed
    and written to the artifact file -- the cap stops the run from going
    further, it does not (and cannot) un-spend a call already made.
    """


@dataclass(frozen=True, slots=True)
class LiveReplayConfig:
    """The full, explicit opt-in every live replay call must supply.

    ``docs/system-design.md`` §2.6's provenance-tagged recorded-response
    requirement exists precisely so that this configuration -- never a
    default -- is what CI must be structurally unable to reach.
    """

    model: str
    cost_cap_usd: float
    client: ReplayClient

    def __post_init__(self) -> None:
        if not self.model:
            raise LiveModeConfigError("live replay requires a configured model identifier")
        if self.cost_cap_usd <= 0:
            raise LiveModeConfigError(
                f"live replay cost cap must be positive, got {self.cost_cap_usd}"
            )


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _turn_record(
    capture: ReplayTurnCapture, provenance: Provenance, *, index: int, induced: bool
) -> dict[str, JsonValue]:
    return {
        "type": "assistant",
        "induced": induced,
        "provenance": provenance.to_record(),
        "message": {
            "role": "assistant",
            "model": capture.usage.model,
            "content": [
                {
                    "type": "tool_use",
                    "id": capture.action.tool_use_id or f"replay_{index}",
                    "name": capture.action.tool_name,
                    "input": dict(capture.action.tool_input),
                }
            ],
            "usage": {
                "input_tokens": capture.usage.input_tokens,
                "cache_read_input_tokens": capture.usage.cache_read,
                "cache_creation_input_tokens": capture.usage.cache_write,
                "output_tokens": capture.usage.output_tokens,
            },
        },
    }


def replay_live(
    baseline: Path,
    config: LiveReplayConfig,
    *,
    artifact_path: Path,
    observations: Mapping[int, str],
    run_id: str | None = None,
) -> RecordedResponseSession:
    """Replay ``baseline`` against a real model through ``config.client``,
    writing every response to ``artifact_path`` as it arrives.

    ``observations`` maps a baseline turn's :attr:`ReplayTurn.index` to the
    (already codec-encoded, if ``codec="on"``) observation text the client
    should see immediately before that turn's action. Only turns present in
    ``observations`` are replayed; every other baseline turn (pure prose,
    or an action with no preceding observation to encode) is skipped.

    Raises :class:`UnknownTurnIndexError` when ``observations`` names any
    turn index ``baseline`` never had -- checked up front, against every
    key at once, before ``artifact_path`` is opened at all: neither a real
    (paid) client call nor a previous run's artifact should ever be spent
    or overwritten to discover a typo in ``observations``.

    Raises :class:`CostCapExceededError` as soon as accumulated spend would
    exceed ``config.cost_cap_usd`` -- every call already made, including the
    one that tipped the cap, has already been written to ``artifact_path``,
    so a capped-out run still leaves genuine, committable partial data.
    """
    baseline_indices = {turn.index for turn in iter_turns(baseline)}
    unknown_indices = sorted(set(observations) - baseline_indices)
    if unknown_indices:
        raise UnknownTurnIndexError(f"{baseline}: no turn(s) at index {unknown_indices}")

    spent_usd = 0.0
    captured: list[ReplayTurn] = []
    prefix: list[ReplayTurn] = []
    ordered_indices = sorted(observations)
    with artifact_path.open("w", encoding="utf-8") as artifact:
        for baseline_index in ordered_indices:
            observation = observations[baseline_index]
            captures = config.client.respond(
                prefix=tuple(prefix), observation=observation, model=config.model
            )
            for position, capture in enumerate(captures):
                induced = position < len(captures) - 1
                call_cost = (
                    ModelUsage()
                    .add_turn(
                        input_tokens=capture.usage.input_tokens,
                        cache_read=capture.usage.cache_read,
                        cache_write=capture.usage.cache_write,
                        output_tokens=capture.usage.output_tokens,
                    )
                    .cost(capture.usage.model)
                    .total
                )
                provenance = Provenance(
                    source="live", model=config.model, captured_at=now_iso(), run_id=run_id
                )
                record = _turn_record(capture, provenance, index=baseline_index, induced=induced)
                artifact.write(json.dumps(record) + "\n")
                artifact.flush()
                spent_usd += call_cost
                replay_turn = ReplayTurn(
                    index=baseline_index,
                    actions=(capture.action,),
                    usage=capture.usage,
                    induced=induced,
                    provenance=provenance,
                )
                captured.append(replay_turn)
                prefix.append(replay_turn)
                if spent_usd > config.cost_cap_usd:
                    raise CostCapExceededError(
                        f"live replay of {baseline} spent ${spent_usd:.4f}, "
                        f"past the ${config.cost_cap_usd:.4f} cap; "
                        f"partial results captured at {artifact_path}"
                    )
    return RecordedResponseSession(baseline=baseline, fixture=artifact_path, turns=tuple(captured))
