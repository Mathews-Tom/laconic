"""Structural action equivalence: same tool, same target, same anchor,
decided without a model.

``docs/system-design.md`` §2.6: "Action equivalence is judged structurally
first -- same tool, same target, same anchor -- and only falls back to a
model judge for semantic near-misses." This module is that first pass,
and it alone answers most turns for free: nothing here calls a model, so
it is deterministic and always available even when the opt-in judge
(:mod:`laconic.replay.judge`) is disabled, which it is by default.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from laconic.replay.corpus import JsonValue
from laconic.replay.engine import RecordedAction


class EquivalenceVerdict(StrEnum):
    """The two possible outcomes of a structural comparison. There is no
    third "unsure" value -- an ambiguous case is DIVERGENT until an
    opt-in judge (:mod:`laconic.replay.judge`) overturns it."""

    EQUIVALENT = "equivalent"
    DIVERGENT = "divergent"


#: The key inside ``tool_input`` that names an action's target, per tool.
#: A tool outside this map falls back to comparing the whole ``tool_input``
#: mapping -- an unrecognized tool's target is everything it was given,
#: since there is nothing more specific to key on.
_TARGET_KEY: Mapping[str, str] = {
    "Read": "path",
    "Edit": "path",
    "Write": "path",
    "Bash": "command",
    "Grep": "pattern",
    "Glob": "pattern",
}

#: For an ``Edit``, the anchor is the region actually being changed --
#: ``old``, not ``new``: two edits are the same action only if they touch
#: the same existing text, regardless of what they replace it with.
_ANCHOR_KEY: Mapping[str, str] = {"Edit": "old"}


def _target(tool_name: str, tool_input: Mapping[str, JsonValue]) -> tuple[JsonValue, bool]:
    """Return ``(target, present)``. ``present`` is ``False`` only when
    ``tool_name`` maps to a specific key (per :data:`_TARGET_KEY`) that
    ``tool_input`` does not carry -- a real gap in the data, never
    silently treated as "no target," which two absent-key actions would
    otherwise both satisfy and compare as vacuously equal.
    """
    key = _TARGET_KEY.get(tool_name)
    if key is None:
        return dict(sorted(tool_input.items())), True
    if key not in tool_input:
        return None, False
    return tool_input.get(key), True


def _anchor(tool_name: str, tool_input: Mapping[str, JsonValue]) -> JsonValue:
    key = _ANCHOR_KEY.get(tool_name)
    return tool_input.get(key) if key is not None else None


@dataclass(frozen=True, slots=True)
class StructuralComparison:
    """One structural verdict, with the reason a human or the opt-in judge
    can read without re-deriving it from the two raw actions."""

    verdict: EquivalenceVerdict
    reason: str

    @property
    def is_equivalent(self) -> bool:
        return self.verdict is EquivalenceVerdict.EQUIVALENT


def compare(recorded: RecordedAction, proposed: RecordedAction) -> StructuralComparison:
    """Compare ``proposed`` against ``recorded`` on tool, target, and
    anchor alone.

    Two actions are :attr:`EquivalenceVerdict.EQUIVALENT` only if all
    three agree exactly; every other key in ``tool_input`` -- a line
    range, a request option, a replacement's exact wording -- is
    deliberately not compared. Doing the same *thing* to the same
    *target* at the same *anchor* is what "equivalent" means here, not
    restating the recorded action byte-for-byte.
    """
    if recorded.tool_name != proposed.tool_name:
        return StructuralComparison(
            EquivalenceVerdict.DIVERGENT,
            f"tool differs: recorded {recorded.tool_name!r}, proposed {proposed.tool_name!r}",
        )
    recorded_target, recorded_has_target = _target(recorded.tool_name, recorded.tool_input)
    proposed_target, proposed_has_target = _target(proposed.tool_name, proposed.tool_input)
    if not recorded_has_target or not proposed_has_target:
        key = _TARGET_KEY.get(recorded.tool_name)
        return StructuralComparison(
            EquivalenceVerdict.DIVERGENT,
            f"{recorded.tool_name} action carries no {key!r} to compare",
        )
    if recorded_target != proposed_target:
        return StructuralComparison(
            EquivalenceVerdict.DIVERGENT,
            f"target differs: recorded {recorded_target!r}, proposed {proposed_target!r}",
        )
    recorded_anchor = _anchor(recorded.tool_name, recorded.tool_input)
    proposed_anchor = _anchor(proposed.tool_name, proposed.tool_input)
    if recorded_anchor != proposed_anchor:
        return StructuralComparison(
            EquivalenceVerdict.DIVERGENT,
            f"anchor differs: recorded {recorded_anchor!r}, proposed {proposed_anchor!r}",
        )
    return StructuralComparison(EquivalenceVerdict.EQUIVALENT, "tool, target, and anchor agree")


@dataclass(frozen=True, slots=True)
class SessionEquivalence:
    """Turn-by-turn structural verdicts for one baseline/recorded-response
    pair, in the order :func:`compare_session` paired them."""

    comparisons: tuple[StructuralComparison, ...]

    @property
    def rate(self) -> float:
        """Fraction of compared turns judged equivalent.

        ``1.0`` for an empty session -- vacuously true, matching how an
        empty comparison set is treated elsewhere in this package (an
        induced-only recorded-response fixture with no terminal actions
        to compare has diverged from nothing).
        """
        if not self.comparisons:
            return 1.0
        equivalent = sum(1 for entry in self.comparisons if entry.is_equivalent)
        return equivalent / len(self.comparisons)

    @property
    def divergences(self) -> tuple[StructuralComparison, ...]:
        return tuple(entry for entry in self.comparisons if not entry.is_equivalent)


def compare_session(
    baseline_actions: Sequence[RecordedAction], proposed_actions: Sequence[RecordedAction]
) -> SessionEquivalence:
    """Pair ``baseline_actions`` and ``proposed_actions`` positionally and
    compare each pair with :func:`compare`.

    The two sequences must already be the same length: this is what
    :attr:`~laconic.replay.engine.RecordedResponseSession.non_induced_actions`
    guarantees against a baseline's own action sequence, since induced
    turns are excluded before comparison ever runs. A length mismatch
    means the caller paired the wrong sequences, not that some turns
    happen to be missing -- raising here catches that at the comparison
    boundary rather than silently truncating to the shorter sequence and
    hiding the mismatch inside a misleadingly clean equivalence rate.
    """
    if len(baseline_actions) != len(proposed_actions):
        raise ValueError(
            f"baseline has {len(baseline_actions)} action(s), proposed has "
            f"{len(proposed_actions)} -- pair only non-induced turns before comparing"
        )
    return SessionEquivalence(
        comparisons=tuple(
            compare(baseline, proposed)
            for baseline, proposed in zip(baseline_actions, proposed_actions, strict=True)
        )
    )
