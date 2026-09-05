"""M18 beta qualification: manifest freeze, receipt derivation, and the
aggregate report's completeness/privacy/determinism guarantees."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from laconic.beta import cli as beta_cli
from laconic.beta.manifest import (
    ELIGIBLE_OMP_VERSION,
    MIN_REPOSITORIES,
    MIN_SESSIONS,
    CampaignManifest,
    ManifestValidationError,
    build_manifest,
    fingerprint_manifest,
    validate_manifest_json,
)
from laconic.beta.privacy import PrivacyViolationError, validate_receipt_json, validate_report_json
from laconic.beta.receipt import (
    KNOWN_REASONS,
    RECEIPT_SCHEMA_VERSION,
    ReceiptDerivationError,
    ReceiptFormatError,
    derive_receipt,
    receipt_from_json,
    receipt_schema_fingerprint,
    repository_id_for_path,
)
from laconic.beta.report import (
    EvidenceRejection,
    EvidenceValidationError,
    Verdict,
    VerdictReason,
    generate_report,
    nearest_rank_percentile,
    render_from_payloads,
    render_markdown,
    validate_evidence_set,
)
from laconic.beta.scenarios import POST_SIGNOFF_SCENARIOS, PRE_SIGNOFF_SCENARIOS
from laconic.ledger import ObservationKind
from laconic.runtime.references import RuntimeReference
from laconic.runtime.storage import RuntimeStorage, resolve_data_dir, session_ledger_path

WHEEL_BYTES = b"fake-wheel-bytes"
WHEEL_HASH = hashlib.sha256(WHEEL_BYTES).hexdigest()

#: The manifest's frozen population: 10 slots distributed across exactly 3
#: repository roots. Fixed, absolute, and never touching tmp_path, so one
#: module-level manifest can be reused by every test.
DEFAULT_REPO_ROOTS: dict[int, Path] = {
    1: Path("/repo/one"),
    2: Path("/repo/one"),
    3: Path("/repo/one"),
    4: Path("/repo/one"),
    5: Path("/repo/two"),
    6: Path("/repo/two"),
    7: Path("/repo/two"),
    8: Path("/repo/three"),
    9: Path("/repo/three"),
    10: Path("/repo/three"),
}


def _build_manifest(
    *, overrides: dict[int, Path] | None = None, wheel_hash: str = WHEEL_HASH
) -> CampaignManifest:
    roots = dict(DEFAULT_REPO_ROOTS)
    if overrides:
        roots.update(overrides)
    slots = tuple((slot, repository_id_for_path(root)) for slot, root in sorted(roots.items()))
    return build_manifest(candidate_wheel_sha256=wheel_hash, slots=slots)


MANIFEST = _build_manifest()
MANIFEST_HASH = fingerprint_manifest(MANIFEST)
SCHEMA_HASH = receipt_schema_fingerprint()


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_build_manifest_declares_the_plan_minimums_and_full_scenario_sets() -> None:
    assert MANIFEST.min_sessions == MIN_SESSIONS == 10
    assert MANIFEST.min_repositories == MIN_REPOSITORIES == 3
    assert MANIFEST.min_eligible_observations == 100
    assert MANIFEST.eligible_omp_version == ELIGIBLE_OMP_VERSION == "18.1.10"
    assert set(MANIFEST.pre_signoff_scenarios) == PRE_SIGNOFF_SCENARIOS
    assert set(MANIFEST.post_signoff_scenarios) == POST_SIGNOFF_SCENARIOS
    assert len(PRE_SIGNOFF_SCENARIOS) == 24
    assert len(POST_SIGNOFF_SCENARIOS) == 2
    assert MANIFEST.candidate_wheel_sha256 == WHEEL_HASH
    assert [slot for slot, _ in MANIFEST.slots] == list(range(1, 11))
    assert len({repo_id for _, repo_id in MANIFEST.slots}) == 3


def test_manifest_never_serializes_a_repository_root_path() -> None:
    blob = json.dumps(MANIFEST.to_json())
    assert "/repo/one" not in blob
    assert "/repo/two" not in blob
    assert "/repo/three" not in blob


def test_manifest_hash_is_deterministic_and_survives_a_json_round_trip() -> None:
    round_tripped = validate_manifest_json(json.loads(json.dumps(MANIFEST.to_json())))
    assert fingerprint_manifest(round_tripped) == MANIFEST_HASH
    assert fingerprint_manifest(_build_manifest()) == MANIFEST_HASH


def test_build_manifest_rejects_a_population_with_the_wrong_shape() -> None:
    with pytest.raises(ManifestValidationError, match="repository"):
        # Only 2 distinct repositories, not the required 3.
        _build_manifest(
            overrides={8: Path("/repo/one"), 9: Path("/repo/one"), 10: Path("/repo/one")}
        )
    with pytest.raises(ManifestValidationError):
        build_manifest(candidate_wheel_sha256="not-a-hex-digest", slots=MANIFEST.slots)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(min_sessions=1),
        lambda payload: payload.update(eligible_omp_version="19.0.0"),
        lambda payload: payload.update(pre_signoff_scenarios=["made_up_scenario"]),
        lambda payload: payload.pop("min_repositories"),
        lambda payload: payload.update(extra_field="nope"),
        lambda payload: payload.update(candidate_wheel_sha256="too-short"),
        lambda payload: payload.update(slots=payload["slots"][:9]),  # only 9 of 10 slots
        lambda payload: payload.update(slots=[[1, s] for _, s in payload["slots"]]),  # dup slot 1
        lambda payload: payload.update(
            slots=[[slot, payload["slots"][0][1]] for slot, _ in payload["slots"]]
        ),  # one repository, not 3
    ],
)
def test_manifest_validate_rejects_any_deviation_from_the_frozen_contract(mutation: Any) -> None:
    payload = MANIFEST.to_json()
    mutation(payload)
    with pytest.raises(ManifestValidationError):
        validate_manifest_json(payload)


# ---------------------------------------------------------------------------
# Receipt derivation from a real session ledger
# ---------------------------------------------------------------------------


def _seed_session(storage: RuntimeStorage, session_id: str) -> None:
    """Seed one session with an emitted, a referenced pass-through, and an
    unreferenced pass-through decision -- the same shapes engine.py emits."""
    with storage.open_ledger(session_id) as ledger:
        emitted_record = ledger.register(ObservationKind.FILE, "src/a.py", "x" * 500, "outline", 1)
        emitted_reference = str(
            RuntimeReference.from_ledger_reference(session_id, emitted_record.handle)
        )
        ledger.record_runtime_decision(
            sequence=1,
            request_id="req-1",
            tool_name="Read",
            outcome="emitted",
            reason="smaller_envelope",
            candidate_reference=emitted_reference,
            raw_chars=500,
            visible_chars=40,
            latency_ms=3.5,
            created_at=100.0,
        )
        passthrough_record = ledger.register(
            ObservationKind.COMMAND, "ls -la", "y" * 30, "y" * 30, 2
        )
        passthrough_reference = str(
            RuntimeReference.from_ledger_reference(session_id, passthrough_record.handle)
        )
        ledger.record_runtime_decision(
            sequence=2,
            request_id="req-2",
            tool_name="Bash",
            outcome="pass_through",
            reason="not_smaller",
            candidate_reference=passthrough_reference,
            raw_chars=30,
            visible_chars=30,
            latency_ms=0.2,
            created_at=101.0,
        )
        ledger.record_runtime_decision(
            sequence=3,
            request_id="req-3",
            tool_name="Write",
            outcome="pass_through",
            reason="unsupported_tool",
            candidate_reference=None,
            raw_chars=10,
            visible_chars=10,
            latency_ms=0.1,
            created_at=102.0,
        )
        ledger.record_runtime_expansion(
            request_id="exp-1", reference=emitted_record.handle, span=False, created_at=103.0
        )
        ledger.record_runtime_expansion(
            request_id="exp-2",
            reference=f"{emitted_record.handle}:1-1",
            span=True,
            created_at=104.0,
        )


def _derive(
    tmp_path: Path,
    session_id: str = "session-1",
    *,
    seed: bool = True,
    slot: int = 1,
    manifest: CampaignManifest = MANIFEST,
    repository_root: Path | None = None,
    wheel_bytes: bytes = WHEEL_BYTES,
    scenarios: tuple[str, ...] = (),
    **overrides: Any,
) -> Any:
    storage = RuntimeStorage(tmp_path / "data")
    if seed:
        _seed_session(storage, session_id)
    wheel = tmp_path / f"candidate-{hashlib.sha256(wheel_bytes).hexdigest()[:8]}.whl"
    if not wheel.exists():
        wheel.write_bytes(wheel_bytes)
    kwargs: dict[str, Any] = {
        "storage": storage,
        "session_id": session_id,
        "manifest": manifest,
        "omp_version": ELIGIBLE_OMP_VERSION,
        "candidate_wheel_path": wheel,
        "slot": slot,
        "repository_root": repository_root or DEFAULT_REPO_ROOTS.get(slot, Path("/repo/other")),
        "clean_shutdown": True,
        "started_at": 100.0,
        "ended_at": 200.0,
        "scenarios": scenarios,
    }
    kwargs.update(overrides)
    return derive_receipt(**kwargs)


def test_derive_receipt_aggregates_exactly_the_seeded_ledger_totals(tmp_path: Path) -> None:
    receipt = _derive(tmp_path)

    assert receipt.decisions_total == 3
    # The unreferenced `unsupported_tool` pass-through is a recorded decision
    # the engine never evaluated for compression, so it is not eligible.
    assert receipt.eligible_observations == 2
    assert receipt.emitted_count == 1
    assert dict(receipt.pass_through_counts) == {"not_smaller": 1, "unsupported_tool": 1}
    assert receipt.raw_chars == 500 + 30 + 10
    assert receipt.visible_chars == 40 + 30 + 10
    assert receipt.characters_avoided == receipt.raw_chars - receipt.visible_chars
    assert receipt.full_expansions == 1
    assert receipt.span_expansions == 1
    assert receipt.latencies_ms == (3.5, 0.2, 0.1)
    assert receipt.exact_expansion_failures == 0
    assert receipt.compressed_tool_errors == 0
    assert receipt.oversized_envelopes == 0
    assert receipt.schema_version == RECEIPT_SCHEMA_VERSION
    assert receipt.schema_hash == SCHEMA_HASH
    assert receipt.manifest_hash == MANIFEST_HASH
    assert receipt.candidate_wheel_sha256 == WHEEL_HASH
    assert receipt.repository_id == repository_id_for_path(DEFAULT_REPO_ROOTS[1])


def test_derive_receipt_never_serializes_the_real_session_id_or_repository_path(
    tmp_path: Path,
) -> None:
    secret_repo = tmp_path / "very-secret-repo-name"
    secret_repo.mkdir()
    manifest = _build_manifest(
        overrides={1: secret_repo, 2: secret_repo, 3: secret_repo, 4: secret_repo}
    )
    receipt = _derive(
        tmp_path,
        session_id="a-very-real-session-id-42",
        manifest=manifest,
        repository_root=secret_repo,
    )

    blob = json.dumps(receipt.to_json())
    assert "a-very-real-session-id-42" not in blob
    assert "very-secret-repo-name" not in blob
    assert receipt.repository_id == repository_id_for_path(secret_repo)


def test_derive_receipt_flags_an_emitted_reference_that_cannot_be_recovered(tmp_path: Path) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    session_id = "session-1"
    with storage.open_ledger(session_id) as ledger:
        # No `register()` call: the handle this decision claims was never
        # actually committed, so independent re-expansion must fail.
        stale_reference = str(RuntimeReference.from_ledger_reference(session_id, "F1"))
        ledger.record_runtime_decision(
            sequence=1,
            request_id="req-1",
            tool_name="Read",
            outcome="emitted",
            reason="smaller_envelope",
            candidate_reference=stale_reference,
            raw_chars=500,
            visible_chars=40,
            latency_ms=1.0,
            created_at=100.0,
        )
    receipt = _derive(tmp_path, session_id=session_id, seed=False)
    assert receipt.exact_expansion_failures == 1


def test_derive_receipt_flags_recovered_content_that_differs_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The independent check compares recovered text to the locally stored
    raw record, not merely its length."""
    from laconic import ledger as ledger_module

    storage = RuntimeStorage(tmp_path / "data")
    session_id = "session-1"
    with storage.open_ledger(session_id) as ledger:
        record = ledger.register(ObservationKind.FILE, "src/a.py", "same length!", "enc", 1)
        reference = str(RuntimeReference.from_ledger_reference(session_id, record.handle))
        ledger.record_runtime_decision(
            sequence=1,
            request_id="req-1",
            tool_name="Read",
            outcome="emitted",
            reason="smaller_envelope",
            candidate_reference=reference,
            raw_chars=len("same length!"),
            visible_chars=3,
            latency_ms=1.0,
            created_at=100.0,
        )
    # Same length as the real raw text, but different bytes: a length-only
    # check would miss this; a byte-for-byte check must not.
    monkeypatch.setattr(ledger_module.Ledger, "expand", lambda self, ref: "different!!!")
    receipt = _derive(tmp_path, session_id=session_id, seed=False)
    assert receipt.exact_expansion_failures == 1


def test_derive_receipt_flags_an_emitted_decision_that_claims_a_tool_error(
    tmp_path: Path,
) -> None:
    """`laconic.runtime.engine` passes a failed tool through before the encode
    path, so an emitted row carrying `tool_error` cannot come from the engine.
    The counter exists to catch exactly that ledger, not ordinary traffic."""
    storage = RuntimeStorage(tmp_path / "data")
    session_id = "session-1"
    with storage.open_ledger(session_id) as ledger:
        record = ledger.register(ObservationKind.COMMAND, "ls", "z" * 200, "enc", 1)
        reference = str(RuntimeReference.from_ledger_reference(session_id, record.handle))
        ledger.record_runtime_decision(
            sequence=1,
            request_id="req-1",
            tool_name="Bash",
            outcome="emitted",
            reason="tool_error",
            candidate_reference=reference,
            raw_chars=200,
            visible_chars=20,
            latency_ms=1.0,
            created_at=100.0,
        )
    receipt = _derive(tmp_path, session_id=session_id, seed=False)
    assert receipt.compressed_tool_errors == 1


def test_derive_receipt_flags_an_emitted_envelope_that_is_not_smaller(tmp_path: Path) -> None:
    """`Ledger.record_runtime_decision` refuses to write this row, so the only
    way one exists is a hand-edited ledger file -- which must still be caught
    rather than aggregated into a clean campaign."""
    storage = RuntimeStorage(tmp_path / "data")
    session_id = "session-1"
    _seed_session(storage, session_id)
    ledger_file = session_ledger_path(session_id, resolve_data_dir(tmp_path / "data"))
    with sqlite3.connect(ledger_file) as database:
        database.execute(
            "UPDATE runtime_decisions SET visible_chars = raw_chars WHERE outcome = 'emitted'"
        )

    receipt = _derive(tmp_path, session_id=session_id, seed=False)
    assert receipt.oversized_envelopes == 1


def test_derive_receipt_excludes_observations_the_engine_never_evaluated(tmp_path: Path) -> None:
    """A session of nothing but unsupported-tool pass-throughs records
    decisions but no eligible observations, so it cannot inflate the
    campaign's 100-eligible-observation minimum."""
    storage = RuntimeStorage(tmp_path / "data")
    session_id = "session-1"
    with storage.open_ledger(session_id) as ledger:
        for sequence in range(1, 4):
            ledger.record_runtime_decision(
                sequence=sequence,
                request_id=f"req-{sequence}",
                tool_name="Write",
                outcome="pass_through",
                reason="unsupported_tool",
                candidate_reference=None,
                raw_chars=10,
                visible_chars=10,
                latency_ms=0.1,
                created_at=100.0 + sequence,
            )

    receipt = _derive(tmp_path, session_id=session_id, seed=False)
    assert receipt.decisions_total == 3
    assert receipt.eligible_observations == 0


def test_derive_receipt_rejects_a_ledger_reason_outside_the_known_vocabulary(
    tmp_path: Path,
) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    session_id = "session-1"
    with storage.open_ledger(session_id) as ledger:
        ledger.record_runtime_decision(
            sequence=1,
            request_id="req-1",
            tool_name="Read",
            outcome="pass_through",
            reason="a_reason_engine_py_never_emits",
            candidate_reference=None,
            raw_chars=10,
            visible_chars=10,
            latency_ms=1.0,
            created_at=100.0,
        )
    with pytest.raises(ReceiptDerivationError, match="outside the closed engine vocabulary"):
        _derive(tmp_path, session_id=session_id, seed=False)


def test_derive_receipt_rejects_an_unknown_scenario_name(tmp_path: Path) -> None:
    with pytest.raises(ReceiptDerivationError, match="unknown scenario"):
        _derive(tmp_path, scenarios=("not_a_real_scenario",))


def test_derive_receipt_rejects_an_ineligible_omp_version(tmp_path: Path) -> None:
    with pytest.raises(ReceiptDerivationError, match="18.1.10"):
        _derive(tmp_path, omp_version="17.0.0")


def test_derive_receipt_rejects_a_wheel_that_does_not_match_the_frozen_manifest(
    tmp_path: Path,
) -> None:
    with pytest.raises(ReceiptDerivationError, match="wheel"):
        _derive(tmp_path, wheel_bytes=b"a-different-candidate-build")


def test_derive_receipt_rejects_a_repository_that_does_not_match_its_declared_slot(
    tmp_path: Path,
) -> None:
    with pytest.raises(ReceiptDerivationError, match="repository"):
        _derive(tmp_path, slot=1, repository_root=DEFAULT_REPO_ROOTS[5])


def test_derive_receipt_rejects_a_slot_the_manifest_never_declared(tmp_path: Path) -> None:
    with pytest.raises(ReceiptDerivationError, match="not declared"):
        _derive(tmp_path, slot=11, repository_root=Path("/repo/eleven"))


def test_derive_receipt_rejects_a_negative_slot_or_backwards_timestamps(tmp_path: Path) -> None:
    with pytest.raises(ReceiptDerivationError, match="slot"):
        _derive(tmp_path, session_id="session-a", slot=0)
    with pytest.raises(ReceiptDerivationError, match="ended_at"):
        _derive(tmp_path, session_id="session-b", started_at=200.0, ended_at=100.0)


# ---------------------------------------------------------------------------
# Receipt privacy and self-consistency ("mutated" evidence)
# ---------------------------------------------------------------------------


def _valid_receipt_payload(
    *,
    slot: int = 1,
    repository_root: Path | None = None,
    clean_shutdown: bool = True,
    scenarios: tuple[str, ...] = (),
    eligible: int = 10,
    emitted: int = 4,
    ineligible: int = 0,
    raw_chars_each: int = 100,
    visible_chars_each: int = 10,
    schema_hash: str = SCHEMA_HASH,
    manifest_hash: str = MANIFEST_HASH,
    wheel_hash: str = WHEEL_HASH,
    exact_expansion_failures: int = 0,
    compressed_tool_errors: int = 0,
    oversized_envelopes: int = 0,
    observed_corruption: int = 0,
    started_at: float = 1_000.0,
    ended_at: float = 1_100.0,
) -> dict[str, Any]:
    """One arithmetically consistent receipt.

    ``eligible`` counts observations the engine actually evaluated for
    compression; ``ineligible`` adds pass-throughs it never evaluated
    (``unsupported_tool``), which count toward ``decisions_total`` only.
    """
    pass_through_count = eligible - emitted
    decisions_total = eligible + ineligible
    raw_total = decisions_total * raw_chars_each
    visible_total = (
        emitted * visible_chars_each + (pass_through_count + ineligible) * raw_chars_each
    )
    pass_through_counts = []
    if pass_through_count:
        pass_through_counts.append(["not_smaller", pass_through_count])
    if ineligible:
        pass_through_counts.append(["unsupported_tool", ineligible])
    root = repository_root if repository_root is not None else DEFAULT_REPO_ROOTS[slot]
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "schema_hash": schema_hash,
        "manifest_hash": manifest_hash,
        "candidate_wheel_sha256": wheel_hash,
        "slot": slot,
        "repository_id": repository_id_for_path(root),
        "clean_shutdown": clean_shutdown,
        "started_at": started_at,
        "ended_at": ended_at,
        "scenarios": sorted(scenarios),
        "decisions_total": decisions_total,
        "eligible_observations": eligible,
        "emitted_count": emitted,
        "pass_through_counts": pass_through_counts,
        "raw_chars": raw_total,
        "visible_chars": visible_total,
        "characters_avoided": raw_total - visible_total,
        "full_expansions": 1,
        "span_expansions": 1,
        "latencies_ms": [1.0] * decisions_total,
        "exact_expansion_failures": exact_expansion_failures,
        "compressed_tool_errors": compressed_tool_errors,
        "oversized_envelopes": oversized_envelopes,
        "observed_corruption": observed_corruption,
    }


def test_valid_receipt_payload_clears_both_privacy_and_format_gates() -> None:
    payload = _valid_receipt_payload()
    validate_receipt_json(payload)
    receipt = receipt_from_json(payload)
    assert receipt.slot == 1
    assert set(dict(receipt.pass_through_counts)) <= KNOWN_REASONS


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(extra_field="nope"),
        lambda payload: payload.pop("clean_shutdown"),
        lambda payload: payload.update(scenarios=["not_a_real_scenario"]),
        lambda payload: payload.update(pass_through_counts=[["free_text_reason", 1]]),
        lambda payload: payload.update(schema_hash="not-a-hex-digest"),
        lambda payload: payload.update(slot=0),
        lambda payload: payload.update(slot="one"),
    ],
)
def test_validate_receipt_json_rejects_privacy_violations(mutation: Any) -> None:
    payload = _valid_receipt_payload()
    mutation(payload)
    with pytest.raises(PrivacyViolationError):
        validate_receipt_json(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(emitted_count=payload["emitted_count"] + 1),
        lambda payload: payload.update(characters_avoided=payload["characters_avoided"] + 1),
        lambda payload: payload.update(latencies_ms=payload["latencies_ms"][:-1]),
        lambda payload: payload.update(exact_expansion_failures=payload["emitted_count"] + 1),
    ],
)
def test_receipt_from_json_rejects_hand_edited_arithmetic(mutation: Any) -> None:
    payload = _valid_receipt_payload()
    mutation(payload)
    with pytest.raises(ReceiptFormatError):
        receipt_from_json(payload)


# ---------------------------------------------------------------------------
# Evidence-set validation: the six rejection categories
# ---------------------------------------------------------------------------


def test_validate_evidence_set_rejects_empty_evidence() -> None:
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_evidence_set([], MANIFEST)
    assert excinfo.value.category is EvidenceRejection.EMPTY


def test_validate_evidence_set_rejects_missing_declared_slots() -> None:
    payloads = [_valid_receipt_payload(slot=1), _valid_receipt_payload(slot=3)]
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_evidence_set(payloads, MANIFEST)
    assert excinfo.value.category is EvidenceRejection.PARTIAL


def test_validate_evidence_set_rejects_a_duplicate_slot() -> None:
    payloads = [_valid_receipt_payload(slot=1), _valid_receipt_payload(slot=1)]
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_evidence_set(payloads, MANIFEST)
    assert excinfo.value.category is EvidenceRejection.DUPLICATE


def test_validate_evidence_set_rejects_a_stale_manifest_hash() -> None:
    payloads = [_valid_receipt_payload(slot=1, manifest_hash="b" * 64)]
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_evidence_set(payloads, MANIFEST)
    assert excinfo.value.category is EvidenceRejection.STALE


def test_validate_evidence_set_rejects_a_stale_schema_hash() -> None:
    payloads = [_valid_receipt_payload(slot=1, schema_hash="c" * 64)]
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_evidence_set(payloads, MANIFEST)
    assert excinfo.value.category is EvidenceRejection.STALE


def test_validate_evidence_set_rejects_a_wheel_not_bound_to_the_manifest() -> None:
    payloads = [_valid_receipt_payload(slot=1, wheel_hash="d" * 64)]
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_evidence_set(payloads, MANIFEST)
    assert excinfo.value.category is EvidenceRejection.STALE


def test_validate_evidence_set_rejects_a_receipt_whose_repository_does_not_match_its_slot() -> None:
    payloads = _full_campaign_payloads()
    payloads[0]["repository_id"] = repository_id_for_path(DEFAULT_REPO_ROOTS[5])
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_evidence_set(payloads, MANIFEST)
    assert excinfo.value.category is EvidenceRejection.MUTATED


def test_validate_evidence_set_rejects_a_mutated_receipt() -> None:
    payload = _valid_receipt_payload(slot=1)
    payload["characters_avoided"] += 1
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_evidence_set([payload], MANIFEST)
    assert excinfo.value.category is EvidenceRejection.MUTATED


def test_validate_evidence_set_rejects_a_privacy_invalid_receipt() -> None:
    payload = _valid_receipt_payload(slot=1)
    del payload["clean_shutdown"]
    with pytest.raises(EvidenceValidationError) as excinfo:
        validate_evidence_set([payload], MANIFEST)
    assert excinfo.value.category is EvidenceRejection.PRIVACY_INVALID


# ---------------------------------------------------------------------------
# Aggregate report: totals, percentiles, and verdict logic
# ---------------------------------------------------------------------------


def test_nearest_rank_percentile_matches_the_textbook_definition() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert nearest_rank_percentile(values, 50) == 3.0
    assert nearest_rank_percentile(values, 95) == 5.0
    assert nearest_rank_percentile([7.0], 50) == 7.0


def _full_campaign_payloads(
    *,
    with_post_signoff: bool = False,
    clean_shutdown_overrides: dict[int, bool] | None = None,
    roots: dict[int, Path] | None = None,
    manifest_hash: str = MANIFEST_HASH,
) -> list[dict[str, Any]]:
    """All 10 declared slots across the manifest's 3 repositories, >= 100
    eligible observations, and every pre-signoff scenario covered."""
    scenario_names = sorted(PRE_SIGNOFF_SCENARIOS)
    overrides = clean_shutdown_overrides or {}
    slot_roots = roots or DEFAULT_REPO_ROOTS
    payloads = []
    for slot in range(1, MIN_SESSIONS + 1):
        # Distribute the 24 pre-signoff scenarios across the 10 sessions.
        chunk = tuple(scenario_names[(slot - 1) * 3 : (slot - 1) * 3 + 3])
        if slot == MIN_SESSIONS and with_post_signoff:
            chunk = chunk + tuple(sorted(POST_SIGNOFF_SCENARIOS))
        payloads.append(
            _valid_receipt_payload(
                slot=slot,
                eligible=12,
                emitted=6,
                scenarios=chunk,
                clean_shutdown=overrides.get(slot, True),
                started_at=1_000.0 + slot,
                ended_at=1_100.0 + slot,
                repository_root=slot_roots[slot],
                manifest_hash=manifest_hash,
            )
        )
    return payloads


def test_validate_evidence_set_accepts_the_full_declared_population_out_of_order() -> None:
    payloads = list(reversed(_full_campaign_payloads()))
    receipts = validate_evidence_set(payloads, MANIFEST)
    assert [receipt.slot for receipt in receipts] == list(range(1, MIN_SESSIONS + 1))


def test_generate_report_computes_totals_percentiles_and_scenario_coverage() -> None:
    payloads = _full_campaign_payloads()
    report, _ = render_from_payloads(payloads, MANIFEST)

    assert report.sessions_total == 10
    assert report.sessions_completed == 10
    assert report.repositories_total == 3
    assert report.decisions_total == 120
    assert report.eligible_observations_total == 120
    assert report.emitted_total == 60
    assert dict(report.pass_through_totals) == {"not_smaller": 60}
    assert report.characters_avoided_total == report.raw_chars_total - report.visible_chars_total
    assert report.latency_p50_ms == 1.0 and report.latency_p95_ms == 1.0
    assert report.pre_signoff_scenarios_missing == ()
    assert report.savings_gate_applies is False
    assert report.schema_hash == SCHEMA_HASH
    assert report.candidate_wheel_sha256 == WHEEL_HASH
    assert report.eligible_omp_version == ELIGIBLE_OMP_VERSION
    assert report.min_sessions == MIN_SESSIONS
    assert report.min_repositories == MIN_REPOSITORIES
    assert report.min_eligible_observations == 100


def test_generate_report_is_no_go_below_the_session_minimum() -> None:
    payloads = _full_campaign_payloads(clean_shutdown_overrides={10: False})
    receipts = validate_evidence_set(payloads, MANIFEST)
    report = generate_report(receipts, MANIFEST)
    assert report.verdict is Verdict.NO_GO
    assert VerdictReason.BELOW_SESSION_MINIMUM in report.verdict_reasons
    assert report.sessions_completed == 9


def test_generate_report_is_no_go_when_a_safety_counter_is_nonzero() -> None:
    payloads = _full_campaign_payloads()
    payloads[0]["observed_corruption"] = 1
    receipts = validate_evidence_set(payloads, MANIFEST)
    report = generate_report(receipts, MANIFEST)
    assert report.verdict is Verdict.NO_GO
    assert VerdictReason.SAFETY_COUNTERS_NONZERO in report.verdict_reasons


def test_generate_report_is_human_review_required_before_post_signoff_evidence() -> None:
    payloads = _full_campaign_payloads(with_post_signoff=False)
    receipts = validate_evidence_set(payloads, MANIFEST)
    report = generate_report(receipts, MANIFEST)
    assert report.verdict is Verdict.HUMAN_REVIEW_REQUIRED
    assert set(report.post_signoff_scenarios_missing) == POST_SIGNOFF_SCENARIOS


def test_generate_report_is_go_once_post_signoff_evidence_is_present() -> None:
    payloads = _full_campaign_payloads(with_post_signoff=True)
    receipts = validate_evidence_set(payloads, MANIFEST)
    report = generate_report(receipts, MANIFEST)
    assert report.verdict is Verdict.GO
    assert report.post_signoff_scenarios_missing == ()


def test_generate_report_verdict_ignores_a_low_observed_reduction() -> None:
    """No minimum aggregate savings percentage may gate the verdict
    (`.docs/DEVELOPMENT_PLAN.md` §6 M18)."""
    payloads = _full_campaign_payloads(with_post_signoff=True)
    for payload in payloads:
        # Shrink the gap between raw and visible characters to force a low
        # (but still non-negative, per the ledger's own invariants) reduction.
        payload["visible_chars"] = payload["raw_chars"] - 1
        payload["characters_avoided"] = 1
    receipts = validate_evidence_set(payloads, MANIFEST)
    report = generate_report(receipts, MANIFEST)
    assert report.observed_reduction_pct is not None
    assert report.observed_reduction_pct < 1.0
    assert report.verdict is Verdict.GO
    assert report.savings_gate_applies is False


def test_report_to_json_clears_the_privacy_gate_and_is_allowlisted() -> None:
    payloads = _full_campaign_payloads(with_post_signoff=True)
    receipts = validate_evidence_set(payloads, MANIFEST)
    report = generate_report(receipts, MANIFEST)
    payload = report.to_json()
    validate_report_json(payload)
    from laconic.beta.report import ALLOWED_REPORT_KEYS

    assert set(payload) == ALLOWED_REPORT_KEYS


def test_serialized_shapes_stay_coupled_to_their_privacy_allowlists() -> None:
    """A field added to a dataclass but not to its allowlist -- or the
    reverse -- must fail here rather than at campaign time, since the
    allowlist is what keeps an unreviewed value from reaching disk."""
    from dataclasses import fields

    from laconic.beta.receipt import ALLOWED_RECEIPT_KEYS, SessionReceipt
    from laconic.beta.report import ALLOWED_REPORT_KEYS, AggregateReport

    assert {field.name for field in fields(SessionReceipt)} == ALLOWED_RECEIPT_KEYS
    assert {field.name for field in fields(AggregateReport)} == ALLOWED_REPORT_KEYS


@pytest.mark.parametrize(
    "key, value",
    [
        ("sessions_total", "ten"),
        ("decisions_total", 1.5),
        ("generated_at", "recently"),
        ("latency_p95_ms", "fast"),
        ("min_sessions", 0),
    ],
)
def test_validate_report_json_type_checks_every_numeric_field(key: str, value: Any) -> None:
    payloads = _full_campaign_payloads(with_post_signoff=True)
    receipts = validate_evidence_set(payloads, MANIFEST)
    payload = generate_report(receipts, MANIFEST).to_json()
    payload[key] = value
    with pytest.raises(PrivacyViolationError):
        validate_report_json(payload)


def test_render_markdown_is_byte_deterministic_across_calls() -> None:
    payloads = _full_campaign_payloads(with_post_signoff=True)
    receipts = validate_evidence_set(payloads, MANIFEST)
    report = generate_report(receipts, MANIFEST)
    assert render_markdown(report) == render_markdown(report)

    _, markdown_a = render_from_payloads(payloads, MANIFEST)
    _, markdown_b = render_from_payloads(list(reversed(payloads)), MANIFEST)
    assert markdown_a == markdown_b


def test_render_markdown_surfaces_the_frozen_contract_pin() -> None:
    payloads = _full_campaign_payloads(with_post_signoff=True)
    receipts = validate_evidence_set(payloads, MANIFEST)
    report = generate_report(receipts, MANIFEST)
    markdown = render_markdown(report)
    assert SCHEMA_HASH in markdown
    assert WHEEL_HASH in markdown
    assert ELIGIBLE_OMP_VERSION in markdown
    assert (
        f"Frozen minimums: {MIN_SESSIONS} sessions, {MIN_REPOSITORIES} repositories, "
        "100 eligible observations" in markdown
    )
    assert f"| Recorded decisions | {report.decisions_total} |" in markdown
    assert "Verdict: **go**" in markdown


# ---------------------------------------------------------------------------
# CLI: manifest / receipt / report, end to end
# ---------------------------------------------------------------------------


def _git_root(path: Path) -> Path:
    """Create, idempotently, a canonical Git root the CLI will accept."""
    (path / ".git").mkdir(parents=True, exist_ok=True)
    return path


def _cli_slot_roots(tmp_path: Path) -> dict[int, Path]:
    """Ten slot roots across exactly 3 real Git roots, matching
    :data:`DEFAULT_REPO_ROOTS`' 4/3/3 distribution."""
    one, two, three = (
        _git_root(tmp_path / name) for name in ("repo-one", "repo-two", "repo-three")
    )
    ordered = [one, one, one, one, two, two, two, three, three, three]
    return dict(enumerate(ordered, start=1))


def _write_manifest_cli(tmp_path: Path, out: Path) -> CampaignManifest:
    """Freeze a manifest through the CLI and return the equivalent typed
    manifest, so a caller can bind receipts to the same frozen population."""
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(WHEEL_BYTES)
    slot_roots = _cli_slot_roots(tmp_path)
    exit_code = beta_cli.main(
        [
            "manifest",
            "generate",
            "--candidate-wheel",
            str(wheel),
            "--out",
            str(out),
            *[str(slot_roots[slot]) for slot in range(1, MIN_SESSIONS + 1)],
        ]
    )
    assert exit_code == beta_cli.EXIT_OK
    return _build_manifest(overrides=slot_roots)


def test_cli_manifest_generate_validate_hash_round_trip(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    expected = _write_manifest_cli(tmp_path, manifest_path)
    assert beta_cli.main(["manifest", "validate", str(manifest_path)]) == beta_cli.EXIT_OK

    generated = validate_manifest_json(json.loads(manifest_path.read_text(encoding="utf-8")))
    assert fingerprint_manifest(generated) == fingerprint_manifest(expected)


def test_cli_manifest_generate_rejects_fewer_than_three_distinct_repositories(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(WHEEL_BYTES)
    same_repo = [str(_git_root(tmp_path / "only-repo"))] * MIN_SESSIONS
    manifest_path = tmp_path / "manifest.json"
    exit_code = beta_cli.main(
        [
            "manifest",
            "generate",
            "--candidate-wheel",
            str(wheel),
            "--out",
            str(manifest_path),
            *same_repo,
        ]
    )
    assert exit_code == beta_cli.EXIT_VALIDATION_ERROR
    assert not manifest_path.exists()


def test_cli_manifest_generate_rejects_a_root_that_is_not_a_canonical_git_root(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(WHEEL_BYTES)
    slot_roots = _cli_slot_roots(tmp_path)
    # Three distinct repositories, but slot 10 names a plain directory that
    # was never a checkout, so the campaign would not span 3 Git roots.
    not_a_repository = tmp_path / "just-a-directory"
    not_a_repository.mkdir()
    roots = [str(slot_roots[slot]) for slot in range(1, MIN_SESSIONS)] + [str(not_a_repository)]
    manifest_path = tmp_path / "manifest.json"

    exit_code = beta_cli.main(
        [
            "manifest",
            "generate",
            "--candidate-wheel",
            str(wheel),
            "--out",
            str(manifest_path),
            *roots,
        ]
    )

    assert exit_code == beta_cli.EXIT_VALIDATION_ERROR
    assert not manifest_path.exists()


def test_cli_manifest_generate_rejects_a_linked_worktree_root(tmp_path: Path) -> None:
    """A linked worktree presents `.git` as a file, not a directory. Three
    worktrees of one repository are three paths but one repository, which
    would otherwise clear the exactly-3-distinct-repositories rule."""
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(WHEEL_BYTES)
    slot_roots = _cli_slot_roots(tmp_path)
    worktree = tmp_path / "linked-worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/linked\n", encoding="utf-8")
    roots = [str(slot_roots[slot]) for slot in range(1, MIN_SESSIONS)] + [str(worktree)]
    manifest_path = tmp_path / "manifest.json"

    exit_code = beta_cli.main(
        [
            "manifest",
            "generate",
            "--candidate-wheel",
            str(wheel),
            "--out",
            str(manifest_path),
            *roots,
        ]
    )

    assert exit_code == beta_cli.EXIT_VALIDATION_ERROR
    assert not manifest_path.exists()


def test_cli_manifest_generate_refuses_to_refreeze_without_force(tmp_path: Path) -> None:
    """Re-freezing over a campaign already in flight silently invalidates
    every receipt collected so far, so it must be deliberate."""
    manifest_path = tmp_path / "manifest.json"
    _write_manifest_cli(tmp_path, manifest_path)
    frozen = manifest_path.read_text(encoding="utf-8")
    wheel = tmp_path / "candidate.whl"
    other_wheel = tmp_path / "other.whl"
    other_wheel.write_bytes(b"a-different-candidate-build")
    slot_roots = _cli_slot_roots(tmp_path)
    roots = [str(slot_roots[slot]) for slot in range(1, MIN_SESSIONS + 1)]

    refused = beta_cli.main(
        [
            "manifest",
            "generate",
            "--candidate-wheel",
            str(other_wheel),
            "--out",
            str(manifest_path),
            *roots,
        ]
    )
    assert refused == beta_cli.EXIT_VALIDATION_ERROR
    assert manifest_path.read_text(encoding="utf-8") == frozen

    forced = beta_cli.main(
        [
            "manifest",
            "generate",
            "--candidate-wheel",
            str(other_wheel),
            "--out",
            str(manifest_path),
            "--force",
            *roots,
        ]
    )
    assert forced == beta_cli.EXIT_OK
    assert manifest_path.read_text(encoding="utf-8") != frozen
    assert wheel.exists()


def test_cli_manifest_validate_rejects_a_tampered_local_file(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    payload = MANIFEST.to_json()
    payload["min_sessions"] = 1
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    exit_code = beta_cli.main(["manifest", "validate", str(manifest_path)])
    assert exit_code == beta_cli.EXIT_VALIDATION_ERROR


def test_cli_receipt_derive_and_validate(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest_cli(tmp_path, manifest_path)
    slot_roots = _cli_slot_roots(tmp_path)
    wheel_path = tmp_path / "candidate.whl"

    storage = RuntimeStorage(tmp_path / "data")
    _seed_session(storage, "session-cli")

    receipt_path = tmp_path / "receipt.json"
    exit_code = beta_cli.main(
        [
            "receipt",
            "derive",
            "--data-dir",
            str(tmp_path / "data"),
            "--session",
            "session-cli",
            "--manifest",
            str(manifest_path),
            "--omp-version",
            ELIGIBLE_OMP_VERSION,
            "--candidate-wheel",
            str(wheel_path),
            "--slot",
            "1",
            "--repository",
            str(slot_roots[1]),
            "--clean-shutdown",
            "--started-at",
            "100",
            "--ended-at",
            "200",
            "--scenarios",
            "pause,resume",
            "--out",
            str(receipt_path),
        ]
    )
    assert exit_code == beta_cli.EXIT_OK
    assert beta_cli.main(["receipt", "validate", str(receipt_path)]) == beta_cli.EXIT_OK

    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert "session-cli" not in json.dumps(payload)
    assert payload["scenarios"] == ["pause", "resume"]


def test_cli_receipt_derive_never_creates_or_echoes_anything_for_a_missing_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`receipt derive` reads evidence; it must not create or re-permission a
    directory an operator mistyped, and must not echo the session id it
    promises never to expose."""
    manifest_path = tmp_path / "manifest.json"
    _write_manifest_cli(tmp_path, manifest_path)
    slot_roots = _cli_slot_roots(tmp_path)
    untouched = tmp_path / "not-a-runtime-store"

    exit_code = beta_cli.main(
        [
            "receipt",
            "derive",
            "--data-dir",
            str(untouched),
            "--session",
            "a-very-real-session-id-42",
            "--manifest",
            str(manifest_path),
            "--omp-version",
            ELIGIBLE_OMP_VERSION,
            "--candidate-wheel",
            str(tmp_path / "candidate.whl"),
            "--slot",
            "1",
            "--repository",
            str(slot_roots[1]),
            "--clean-shutdown",
            "--started-at",
            "100",
            "--ended-at",
            "200",
            "--out",
            str(tmp_path / "receipt.json"),
        ]
    )

    assert exit_code == beta_cli.EXIT_VALIDATION_ERROR
    assert not untouched.exists()
    captured = capsys.readouterr()
    assert "a-very-real-session-id-42" not in captured.out + captured.err


def test_cli_report_generate_ignores_a_symlinked_receipt(tmp_path: Path) -> None:
    """The receipts directory means the files it contains, so a symlink to
    evidence living elsewhere is skipped rather than followed."""
    manifest_path = tmp_path / "manifest.json"
    manifest = _write_manifest_cli(tmp_path, manifest_path)
    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir()
    payloads = _full_campaign_payloads(
        roots=_cli_slot_roots(tmp_path), manifest_hash=fingerprint_manifest(manifest)
    )
    for payload in payloads[:-1]:
        slot = payload["slot"]
        (receipts_dir / f"slot-{slot:02d}.json").write_text(json.dumps(payload), encoding="utf-8")
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps(payloads[-1]), encoding="utf-8")
    (receipts_dir / "slot-10.json").symlink_to(elsewhere)

    exit_code = beta_cli.main(
        [
            "report",
            "generate",
            "--receipts-dir",
            str(receipts_dir),
            "--manifest",
            str(manifest_path),
            "--out",
            str(tmp_path / "report.md"),
        ]
    )

    # Slot 10 was linked, not present: the set is partial, not complete.
    assert exit_code == beta_cli.EXIT_VALIDATION_ERROR
    assert not (tmp_path / "report.md").exists()


def test_cli_report_generate_then_check_detects_drift(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest = _write_manifest_cli(tmp_path, manifest_path)

    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir()
    payloads = _full_campaign_payloads(
        with_post_signoff=True,
        roots=_cli_slot_roots(tmp_path),
        manifest_hash=fingerprint_manifest(manifest),
    )
    for payload in payloads:
        slot = payload["slot"]
        (receipts_dir / f"slot-{slot:02d}.json").write_text(json.dumps(payload), encoding="utf-8")

    report_path = tmp_path / "report.md"
    assert (
        beta_cli.main(
            [
                "report",
                "generate",
                "--receipts-dir",
                str(receipts_dir),
                "--manifest",
                str(manifest_path),
                "--out",
                str(report_path),
            ]
        )
        == beta_cli.EXIT_OK
    )
    assert "Verdict: **go**" in report_path.read_text(encoding="utf-8")
    assert (
        beta_cli.main(
            [
                "report",
                "check",
                "--receipts-dir",
                str(receipts_dir),
                "--manifest",
                str(manifest_path),
                "--report",
                str(report_path),
            ]
        )
        == beta_cli.EXIT_OK
    )

    # Mutate one receipt: the committed report is now stale.
    tampered = json.loads((receipts_dir / "slot-01.json").read_text(encoding="utf-8"))
    tampered["observed_corruption"] = 1
    (receipts_dir / "slot-01.json").write_text(json.dumps(tampered), encoding="utf-8")
    assert (
        beta_cli.main(
            [
                "report",
                "check",
                "--receipts-dir",
                str(receipts_dir),
                "--manifest",
                str(manifest_path),
                "--report",
                str(report_path),
            ]
        )
        == beta_cli.EXIT_REPORT_DRIFT
    )


def test_cli_report_generate_rejects_empty_evidence(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest_cli(tmp_path, manifest_path)
    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir()
    report_path = tmp_path / "report.md"
    exit_code = beta_cli.main(
        [
            "report",
            "generate",
            "--receipts-dir",
            str(receipts_dir),
            "--manifest",
            str(manifest_path),
            "--out",
            str(report_path),
        ]
    )
    assert exit_code == beta_cli.EXIT_VALIDATION_ERROR
    assert not report_path.exists()
