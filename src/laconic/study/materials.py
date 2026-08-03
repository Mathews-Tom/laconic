"""Seeded-defect trace materials for the human-bug-catch human-study harness.

``docs/system-design.md`` §4.1's Materials line: "Real agent traces from the
corpus, each containing exactly one seeded defect of a known class: an
unhandled error path, an incorrect boundary condition, a silently swallowed
exception, or an edit applied to the wrong target." This module builds eight
such traces -- four matched pairs, one pair per :class:`DefectClass` -- each
exposed in the two conditions the study compares:

- :attr:`TraceMaterial.raw_text` -- the full, unelided trace a raw-trace
  reader sees, turn by turn.
- :attr:`TraceMaterial.rendered_text` -- exactly what ``laconic view`` would
  print: :func:`laconic.render.templates.render`'s provenance-tagged
  structural facts, plus a narration block via
  :func:`laconic.render.templates.render_narration`. This exercises the same
  milestone narration-rendering code path a real ``--provider ollama`` view uses
  (visually distinct generated prose, every span source-handle-tagged), but
  through a deterministic, committed fixture provider rather than a live
  model call -- the same CI-safety posture milestone/milestone already use for
  recorded-response replay, and confirmed compatible with milestone's actual
  ``NarrationProvider`` contract at milestone's design gate (H-31).

Materials are synthetic and hand-authored, not derived from a real corpus
transcript: each ``TraceRecord`` is constructed directly rather than through
a live :class:`~laconic.ledger.Ledger`, so no database or encoder pipeline
participates and every material is trivially reproducible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from laconic.ledger import ObservationKind, TraceRecord
from laconic.render.narrate import Narration, NarrationProvider
from laconic.render.templates import render, render_narration
from laconic.render.view import TraceEntry


class DefectClass(StrEnum):
    """The four seeded-defect classes named in ``docs/system-design.md`` §4.1."""

    UNHANDLED_ERROR = "unhandled_error_path"
    BOUNDARY_CONDITION = "incorrect_boundary_condition"
    SWALLOWED_EXCEPTION = "swallowed_exception"
    WRONG_TARGET_EDIT = "wrong_target_edit"


@dataclass(frozen=True, slots=True)
class TraceMaterial:
    """One seeded-defect trace, ready to present in either study condition.

    ``task_id`` is unique per material; ``matched_pair_id`` (equal to
    ``defect_class``'s value) links the two variants -- ``"a"`` and ``"b"``
    -- of the same defect class into the matched pair the protocol requires.
    Every material contains exactly one seeded defect; there is no "clean"
    variant, since the protocol measures detection rate, not discrimination
    between defective and non-defective traces.
    """

    task_id: str
    defect_class: DefectClass
    variant: Literal["a", "b"]
    rendered_text: str
    raw_text: str
    handles: tuple[str, ...]

    @property
    def matched_pair_id(self) -> str:
        return self.defect_class.value


@dataclass(frozen=True, slots=True)
class _Observation:
    """One turn's tool call, before it is projected into a study material."""

    kind: ObservationKind
    subject: str
    raw: str


@dataclass(frozen=True, slots=True)
class _FixtureNarrationProvider:
    """A deterministic, committed stand-in for a real milestone narration call.

    Implements :class:`~laconic.render.narrate.NarrationProvider` structurally
    (``narrate(entries) -> Narration | None``) so :func:`_build_task` can call
    the real :func:`~laconic.render.templates.render_narration` template, the
    same one a live ``OllamaProvider`` response would flow through -- without
    a network call inside a synthetic, committed fixture.
    """

    text: str

    def narrate(self, entries: Sequence[TraceEntry]) -> Narration | None:
        if not entries:
            return None
        return Narration(text=self.text, source_handles=tuple(e.record.handle for e in entries))


#: A one-line typed binding pins ``_FixtureNarrationProvider`` to the real
#: protocol at type-check time (``mypy --strict``) -- otherwise the class's
#: docstring claim of structural conformance is never actually verified,
#: and a future ``NarrationProvider`` signature change could silently
#: diverge from it.
_narration_provider_conforms_to_protocol: NarrationProvider = _FixtureNarrationProvider("")


def _build_task(
    task_id: str,
    defect_class: DefectClass,
    variant: Literal["a", "b"],
    observations: Sequence[_Observation],
    narration_text: str,
) -> TraceMaterial:
    """Project ``observations`` into both study conditions for one task.

    Handles are minted with a per-:class:`~laconic.ledger.ObservationKind`
    counter, exactly matching :meth:`laconic.ledger.Ledger.register`'s own
    ordinal scheme (``ledger.py``'s ``self._counters.get(kind, 0) + 1``) --
    not a per-task running index. A global index would mint a second
    observation's handle as e.g. ``B2`` whenever it differs in kind from the
    first, where the real ledger mints ``B1``; since the rendered condition
    exists specifically to show participants what ``laconic view`` actually
    renders, a handle scheme the product never produces would be a defect in
    the measurement instrument itself.
    """
    counters: dict[ObservationKind, int] = {}
    entries: list[TraceEntry] = []
    for index, observation in enumerate(observations):
        counters[observation.kind] = counters.get(observation.kind, 0) + 1
        entries.append(
            TraceEntry(
                turn=index + 1,
                record=TraceRecord(
                    handle=f"{observation.kind.value}{counters[observation.kind]}",
                    kind=observation.kind,
                    subject=observation.subject,
                    raw_chars=len(observation.raw),
                    turn=index,
                ),
            )
        )
    entries_tuple: tuple[TraceEntry, ...] = tuple(entries)
    facts = render(entries_tuple)
    narration = _FixtureNarrationProvider(narration_text).narrate(entries_tuple)
    rendered_text = facts if narration is None else f"{facts}\n\n{render_narration(narration)}"
    raw_text = "\n\n".join(
        f"Turn {index + 1}: {observation.kind.value} {observation.subject}\n{observation.raw}"
        for index, observation in enumerate(observations)
    )
    return TraceMaterial(
        task_id=task_id,
        defect_class=defect_class,
        variant=variant,
        rendered_text=rendered_text,
        raw_text=raw_text,
        handles=tuple(entry.record.handle for entry in entries_tuple),
    )


#: Ground truth: ``code[key]`` on an un-validated key raises ``KeyError``
#: uncaught, and the adjacent passing test never exercises an unknown key.
_UNHANDLED_ERROR_A = (
    _Observation(
        ObservationKind.FILE,
        "orders/checkout.py",
        'DISCOUNT_TABLE = {"SAVE10": 0.10, "SAVE20": 0.20}\n\n'
        "def apply_discount(order, code):\n"
        "    discount = DISCOUNT_TABLE[code]\n"
        "    order.total *= 1 - discount\n"
        "    return order\n",
    ),
    _Observation(
        ObservationKind.COMMAND,
        "pytest tests/test_checkout.py",
        "tests/test_checkout.py::test_apply_valid_discount PASSED\n"
        "tests/test_checkout.py::test_apply_no_discount PASSED\n"
        "2 passed in 0.04s\n",
    ),
)

_UNHANDLED_ERROR_B = (
    _Observation(
        ObservationKind.FILE,
        "billing/invoice.py",
        "def send_invoice(customer_id, amount):\n"
        "    customer = CUSTOMERS[customer_id]\n"
        "    mailer.send(customer.email, render_invoice(customer, amount))\n"
        "    return True\n",
    ),
    _Observation(
        ObservationKind.COMMAND,
        "pytest tests/test_invoice.py",
        "tests/test_invoice.py::test_send_invoice_known_customer PASSED\n1 passed in 0.02s\n",
    ),
)

#: Ground truth: ``range(1, len(x))`` skips index 0, silently dropping the
#: first element from every loop that walks it.
_BOUNDARY_A = (
    _Observation(
        ObservationKind.FILE,
        "inventory/restock.py",
        "def reorder_needed(levels, threshold):\n"
        "    flagged = []\n"
        "    for i in range(1, len(levels)):\n"
        "        if levels[i] < threshold:\n"
        "            flagged.append(i)\n"
        "    return flagged\n",
    ),
    _Observation(
        ObservationKind.COMMAND,
        "pytest tests/test_restock.py",
        "tests/test_restock.py::test_reorder_flags_low_stock PASSED\n1 passed in 0.03s\n",
    ),
)

_BOUNDARY_B = (
    _Observation(
        ObservationKind.FILE,
        "loyalty/points.py",
        "def award_points(purchases):\n"
        "    total = 0\n"
        "    for i in range(1, len(purchases)):\n"
        "        total += purchases[i].points\n"
        "    return total\n",
    ),
    _Observation(
        ObservationKind.COMMAND,
        "pytest tests/test_points.py",
        "tests/test_points.py::test_award_points_sums_purchases PASSED\n1 passed in 0.02s\n",
    ),
)

#: Ground truth: a bare ``except Exception: pass``/log-and-continue swallows
#: a real failure and reports success (or stale data) to the caller anyway.
_SWALLOWED_A = (
    _Observation(
        ObservationKind.FILE,
        "sync/replicate.py",
        "def replicate_record(record):\n"
        "    try:\n"
        "        remote.write(record)\n"
        "    except Exception:\n"
        "        pass\n"
        "    return True\n",
    ),
    _Observation(
        ObservationKind.COMMAND,
        "pytest tests/test_replicate.py",
        "tests/test_replicate.py::test_replicate_record_returns_true PASSED\n1 passed in 0.02s\n",
    ),
)

_SWALLOWED_B = (
    _Observation(
        ObservationKind.FILE,
        "cache/refresh.py",
        "def refresh_cache(key):\n"
        "    try:\n"
        "        cache[key] = fetch_upstream(key)\n"
        "    except Exception:\n"
        '        logger.debug("refresh failed for %s", key)\n'
        "    return cache.get(key)\n",
    ),
    _Observation(
        ObservationKind.COMMAND,
        "pytest tests/test_cache.py",
        "tests/test_cache.py::test_refresh_cache_returns_value PASSED\n1 passed in 0.02s\n",
    ),
)

#: Ground truth: the fix belonged on the second function the read exposed;
#: the diff instead lands on the first, leaving the real target unfixed.
_WRONG_TARGET_A = (
    _Observation(
        ObservationKind.FILE,
        "auth/signup.py",
        "def normalize_username(name):\n"
        "    return name.strip().lower()\n\n"
        "def normalize_email(email):\n"
        "    return email.lower()\n",
    ),
    _Observation(
        ObservationKind.OTHER,
        "auth/signup.py: normalize_username",
        "--- a/auth/signup.py\n"
        "+++ b/auth/signup.py\n"
        "@@ def normalize_username(name):\n"
        "-    return name.strip().lower()\n"
        "+    return name.strip().lower()  # trimmed\n",
    ),
)

_WRONG_TARGET_B = (
    _Observation(
        ObservationKind.FILE,
        "pricing/discount.py",
        "def compute_shipping_discount(order):\n"
        "    return round(order.shipping * 0.9, 2)\n\n"
        "def compute_order_discount(order):\n"
        "    return round(order.subtotal * 0.9)\n",
    ),
    _Observation(
        ObservationKind.OTHER,
        "pricing/discount.py: compute_shipping_discount",
        "--- a/pricing/discount.py\n"
        "+++ b/pricing/discount.py\n"
        "@@ def compute_shipping_discount(order):\n"
        "-    return round(order.shipping * 0.9, 2)\n"
        "+    return round(order.shipping * 0.90, 2)\n",
    ),
)


def build_materials() -> tuple[TraceMaterial, ...]:
    """Build the eight seeded-defect materials: four matched pairs, one pair
    per :class:`DefectClass`, each pair covering the same defect shape in two
    independent code contexts. Deterministic: every field is a literal.
    """
    return (
        _build_task(
            "unhandled-error-a",
            DefectClass.UNHANDLED_ERROR,
            "a",
            _UNHANDLED_ERROR_A,
            "The read is followed by a passing test run against the same module.",
        ),
        _build_task(
            "unhandled-error-b",
            DefectClass.UNHANDLED_ERROR,
            "b",
            _UNHANDLED_ERROR_B,
            "The read precedes a single passing test that exercises one known customer id.",
        ),
        _build_task(
            "boundary-a",
            DefectClass.BOUNDARY_CONDITION,
            "a",
            _BOUNDARY_A,
            "The loop below the read walks the same list the adjacent test builds.",
        ),
        _build_task(
            "boundary-b",
            DefectClass.BOUNDARY_CONDITION,
            "b",
            _BOUNDARY_B,
            "The summation below the read is exercised once by the adjacent test run.",
        ),
        _build_task(
            "swallowed-a",
            DefectClass.SWALLOWED_EXCEPTION,
            "a",
            _SWALLOWED_A,
            "The function below the read is called once by the adjacent test, which "
            "only checks the return value.",
        ),
        _build_task(
            "swallowed-b",
            DefectClass.SWALLOWED_EXCEPTION,
            "b",
            _SWALLOWED_B,
            "The read is followed by one passing test that supplies an upstream fetch "
            "which never fails.",
        ),
        _build_task(
            "wrong-target-a",
            DefectClass.WRONG_TARGET_EDIT,
            "a",
            _WRONG_TARGET_A,
            "The edit below the read targets the first of the two functions the read exposed.",
        ),
        _build_task(
            "wrong-target-b",
            DefectClass.WRONG_TARGET_EDIT,
            "b",
            _WRONG_TARGET_B,
            "The edit below the read targets the first of the two functions the read exposed.",
        ),
    )


def materials_for(
    materials: Sequence[TraceMaterial], defect_class: DefectClass
) -> tuple[TraceMaterial, TraceMaterial]:
    """Return the matched ``("a", "b")`` pair for ``defect_class``.

    Raises :class:`ValueError` if ``materials`` does not carry exactly two
    entries for the class -- a study built on an incomplete or duplicated
    pair would silently break the within-subjects matched-pair design.
    """
    matches = [material for material in materials if material.defect_class == defect_class]
    if len(matches) != 2:
        raise ValueError(
            f"expected exactly 2 matched materials for {defect_class}, found {len(matches)}"
        )
    first, second = sorted(matches, key=lambda material: material.variant)
    return first, second
