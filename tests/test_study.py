"""Tests for the K3 human-study harness."""

from __future__ import annotations

import pytest

from laconic.ledger import Ledger, ObservationKind
from laconic.study.materials import DefectClass, build_materials, materials_for


def test_materials_cover_all_four_defect_classes() -> None:
    materials = build_materials()
    assert {material.defect_class for material in materials} == set(DefectClass)


def test_materials_each_defect_class_has_exactly_two_matched_variants() -> None:
    materials = build_materials()
    for defect_class in DefectClass:
        matched = [m for m in materials if m.defect_class == defect_class]
        assert {m.variant for m in matched} == {"a", "b"}


def test_materials_task_ids_are_unique() -> None:
    materials = build_materials()
    task_ids = [material.task_id for material in materials]
    assert len(task_ids) == len(set(task_ids)) == 8


def test_materials_matched_pair_id_links_variants_of_the_same_class() -> None:
    materials = build_materials()
    variant_a, variant_b = materials_for(materials, DefectClass.BOUNDARY_CONDITION)
    assert variant_a.matched_pair_id == variant_b.matched_pair_id == "incorrect_boundary_condition"
    assert variant_a.task_id != variant_b.task_id


def test_materials_for_raises_on_missing_pair() -> None:
    with pytest.raises(ValueError, match="expected exactly 2"):
        materials_for((), DefectClass.SWALLOWED_EXCEPTION)


def test_materials_raw_text_shows_the_seeded_defect() -> None:
    materials = build_materials()
    by_task = {material.task_id: material for material in materials}
    assert "DISCOUNT_TABLE[code]" in by_task["unhandled-error-a"].raw_text
    assert "CUSTOMERS[customer_id]" in by_task["unhandled-error-b"].raw_text
    assert "range(1, len(levels))" in by_task["boundary-a"].raw_text
    assert "range(1, len(purchases))" in by_task["boundary-b"].raw_text
    assert "except Exception:\n        pass" in by_task["swallowed-a"].raw_text
    assert "logger.debug" in by_task["swallowed-b"].raw_text
    assert "def normalize_email(email):\n    return email.lower()" in (
        by_task["wrong-target-a"].raw_text
    )
    assert "def compute_order_discount(order):\n    return round(order.subtotal * 0.9)" in (
        by_task["wrong-target-b"].raw_text
    )


def test_materials_rendered_text_never_repeats_the_raw_defect_line() -> None:
    """The rendered condition asserts only structural facts and narration --
    never the literal source line -- proving the two conditions genuinely
    differ in what content they expose, not merely in formatting.
    """
    materials = build_materials()
    for material in materials:
        assert "DISCOUNT_TABLE[code]" not in material.rendered_text
        assert "except Exception:" not in material.rendered_text


def test_materials_rendered_text_includes_a_visually_distinct_narration_block() -> None:
    materials = build_materials()
    for material in materials:
        assert "--- generated narration" in material.rendered_text
        assert "--- end generated narration ---" in material.rendered_text
        for handle in material.handles:
            assert f"[{handle}]" in material.rendered_text


def test_materials_rendered_text_carries_a_handle_per_turn() -> None:
    materials = build_materials()
    for material in materials:
        assert len(material.handles) == len(set(material.handles))
        for handle in material.handles:
            assert f"[{handle}]" in material.rendered_text


def test_materials_handles_match_what_a_real_ledger_would_mint() -> None:
    """Regression for a per-task global handle index that diverged from
    :meth:`~laconic.ledger.Ledger.register`'s per-kind ordinal scheme --
    the rendered condition exists specifically to show participants what
    ``laconic view`` actually renders, so a handle scheme the product
    never produces would be a defect in the measurement instrument itself.
    Independently reconstructs each material's expected handle sequence by
    feeding a real :class:`~laconic.ledger.Ledger` observations of the
    same kinds, in the same order, and confirms it mints the identical
    handles.
    """
    kind_by_prefix = {
        "F": ObservationKind.FILE,
        "B": ObservationKind.COMMAND,
        "X": ObservationKind.OTHER,
    }
    materials = build_materials()
    for material in materials:
        kinds = [kind_by_prefix[handle[0]] for handle in material.handles]
        with Ledger(":memory:", f"handle-check-{material.task_id}") as ledger:
            minted = tuple(
                ledger.register(
                    kind, f"subject-{index}", f"raw-{index}", "encoded", turn=index
                ).handle
                for index, kind in enumerate(kinds)
            )
        assert minted == material.handles


def test_materials_are_deterministic_across_builds() -> None:
    first = {m.task_id: (m.rendered_text, m.raw_text, m.handles) for m in build_materials()}
    second = {m.task_id: (m.rendered_text, m.raw_text, m.handles) for m in build_materials()}
    assert first == second
