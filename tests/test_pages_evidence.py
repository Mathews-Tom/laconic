"""The frozen Pages snapshot fails closed before publishing aggregate evidence."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from laconic.pages.evidence import (
    LIMITATIONS,
    PagesEvidence,
    PagesEvidenceError,
    build_pages_evidence,
)
from laconic.pages.snapshot import (
    INCLUSIVE_START,
    MAX_METADATA_RECORDS,
    SnapshotError,
    discover_inventory,
    metadata_from_record,
    read_selected_runtime,
    read_session_metadata,
)
from laconic.pages.snapshot import _generate_snapshot as generate_snapshot
from laconic.runtime.storage import RuntimeStorage
from laconic.spend.join import Composition, join
from laconic.spend.ledger import SessionDecisions
from laconic.spend.omp import SessionUsage, TurnUsage
from laconic.spend.report import build_report

SESSION = "01a078c0-15c2-7000-9389-19db9037f833"
OTHER_SESSION = "32f56d6d-5d56-4c02-b9dd-13a486e780e3"
NOW = datetime(2026, 9, 16, 17, 30, tzinfo=UTC)
TIMESTAMP = "2026-09-10T12:00:00Z"
COMMIT = "a" * 40
HASH = "b" * 64


@pytest.fixture(autouse=True)
def _clear_ci_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)


class GuardedRecord(dict[str, Any]):
    """Mapping that exposes an allowlisted-key access violation immediately."""

    def get(self, key: str, default: Any = None) -> Any:
        if key not in {"cwd", "timestamp"}:
            raise AssertionError(f"forbidden key access: {key}")
        return super().get(key, default)


def _write_records(path: Path, records: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _usage() -> dict[str, Any]:
    components = {
        "input": 0.00005,
        "output": 0.0005,
        "cacheRead": 0.0005,
        "cacheWrite": 0.000625,
    }
    return {
        "input": 10,
        "output": 20,
        "cacheRead": 1000,
        "cacheWrite": 100,
        "totalTokens": 1130,
        "cost": {**components, "total": sum(components.values())},
        "cttl": {"ephemeral5m": 100},
    }


def _omp_records(repo: Path, session_id: str = SESSION) -> list[dict[str, Any]]:
    return [
        {
            "type": "session",
            "version": 3,
            "id": session_id,
            "cwd": str(repo),
            "timestamp": TIMESTAMP,
        },
        {
            "type": "message",
            "timestamp": TIMESTAMP,
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "provider": "anthropic",
                "content": [{"type": "text", "text": "PRIVATE CONTENT"}],
                "usage": _usage(),
            },
        },
    ]


def _closed(path: Path) -> None:
    closed = int((NOW - timedelta(hours=2)).timestamp() * 1_000_000_000)
    os.utime(path, ns=(closed, closed))


def _record_decisions(data_dir: Path, session_id: str = SESSION, *, emitted: bool = True) -> None:
    storage = RuntimeStorage(data_dir)
    with storage.open_ledger(session_id) as ledger:
        ledger.record_runtime_decision(
            sequence=1,
            request_id="req-1",
            tool_name="read",
            outcome="emitted" if emitted else "pass_through",
            reason="smaller" if emitted else "not_smaller",
            candidate_reference="F1" if emitted else None,
            raw_chars=1000,
            visible_chars=100 if emitted else 1000,
            latency_ms=1.0,
            created_at=1.0,
        )
        if emitted:
            ledger.record_runtime_expansion(
                request_id="expand-1",
                reference="F1",
                span=False,
                created_at=2.0,
            )


def _fixture(tmp_path: Path, *, emitted: bool = True) -> tuple[Path, Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    omp_root = tmp_path / "omp"
    transcript = _write_records(omp_root / "session.jsonl", _omp_records(repo))
    _closed(transcript)
    data_dir = tmp_path / "runtime"
    _record_decisions(data_dir, emitted=emitted)
    output = repo / "docs" / "pages" / "evidence" / "laconic-development.json"
    return repo, omp_root, data_dir, output


def _generate(tmp_path: Path, *, emitted: bool = True) -> PagesEvidence:
    repo, omp_root, data_dir, output = _fixture(tmp_path, emitted=emitted)
    result = generate_snapshot(
        repo_root=repo,
        manifest_dir=repo / ".laconic" / "research" / "pages-snapshot",
        output=output,
        omp_roots=[omp_root],
        claude_roots=[],
        data_dir=data_dir,
        now=NOW,
        generator_commit=COMMIT,
    )
    assert result.source_unchanged
    assert result.selected_rows_unchanged
    return result.evidence


def _turn(model: str = "claude-opus-4-8") -> TurnUsage:
    return TurnUsage(
        model=model,
        provider="anthropic",
        input_tokens=10,
        cache_read=1000,
        cache_write=100,
        output_tokens=20,
        host_cost_usd=0.001675,
    )


def _composition(*, emitted: bool, model: str = "claude-opus-4-8") -> PagesEvidence:
    usage = SessionUsage(
        session_id=SESSION,
        turns=(_turn(model),),
        turns_without_usage=0,
        malformed_lines=0,
        unknown_usage_keys=frozenset(),
        nested=False,
    )
    decisions = SessionDecisions(
        session_id=SESSION,
        eligible=1,
        emitted=int(emitted),
        raw_chars=1000,
        visible_chars=100 if emitted else 1000,
        full_expansions=int(emitted),
        span_expansions=0,
    )
    return build_pages_evidence(
        join([usage], [decisions]),
        snapshot_id="laconic-development-20260916T173000Z",
        inclusive_start=INCLUSIVE_START,
        exclusive_end="2026-09-16T17:30:00Z",
        laconic_version="0.12.0",
        generator_commit=COMMIT,
        manifest_sha256=HASH,
        source_inventory_sha256="c" * 64,
    )


def test_metadata_parser_can_observe_only_allowlisted_top_level_values() -> None:
    record = GuardedRecord(
        cwd="/allowed",
        timestamp=TIMESTAMP,
        title="PRIVATE TITLE",
        instructions="PRIVATE INSTRUCTIONS",
        message="PRIVATE MESSAGE",
    )

    assert metadata_from_record(record) == ("/allowed", TIMESTAMP)


def test_metadata_reader_stops_before_record_51(tmp_path: Path) -> None:
    path = _write_records(
        tmp_path / "late.jsonl",
        [{"type": "noise"} for _ in range(MAX_METADATA_RECORDS)]
        + [{"cwd": str(tmp_path), "timestamp": TIMESTAMP}],
    )

    with pytest.raises(SnapshotError, match="metadata_missing"):
        read_session_metadata(path)


def test_selector_excludes_missing_late_malformed_outside_active_and_symlink_paths(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "repo-private"
    outside.mkdir()
    symlink = repo / "escape"
    symlink.symlink_to(outside, target_is_directory=True)
    root = tmp_path / "sessions"
    valid = _write_records(root / "valid.jsonl", _omp_records(repo))
    missing = _write_records(root / "missing.jsonl", [{"cwd": str(repo)}])
    late = _write_records(
        root / "late.jsonl",
        [{**_omp_records(repo)[0], "timestamp": "2026-09-16T17:30:00Z"}],
    )
    malformed = root / "malformed.jsonl"
    malformed.write_text("{not json}\n", encoding="utf-8")
    prefix = _write_records(root / "prefix.jsonl", _omp_records(outside, OTHER_SESSION))
    escaped = _write_records(root / "escaped.jsonl", _omp_records(symlink, OTHER_SESSION))
    active = _write_records(root / "active.jsonl", _omp_records(repo, OTHER_SESSION))
    for path in (valid, missing, late, malformed, prefix, escaped):
        _closed(path)

    inventory = discover_inventory(
        repo_root=repo.resolve(),
        omp_roots=[root],
        claude_roots=[],
        inclusive_start=datetime.fromisoformat(INCLUSIVE_START.replace("Z", "+00:00")),
        exclusive_end=NOW,
    )

    assert [item.path.name for item in inventory] == ["valid.jsonl"]
    assert active not in [item.path for item in inventory]


def test_selected_runtime_digest_ignores_unrelated_rows_but_detects_selected_changes(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "runtime"
    _record_decisions(data_dir)
    first = read_selected_runtime([SESSION], data_dir)
    _record_decisions(data_dir, OTHER_SESSION)
    unrelated = read_selected_runtime([SESSION], data_dir)
    assert unrelated.rows_sha256 == first.rows_sha256

    storage = RuntimeStorage(data_dir)
    with storage.open_existing_ledger(SESSION) as ledger:
        ledger.record_runtime_expansion(
            request_id="expand-2", reference="F1:1-2", span=True, created_at=3.0
        )
    changed = read_selected_runtime([SESSION], data_dir)
    assert changed.rows_sha256 != first.rows_sha256


def test_damaged_runtime_ledger_aborts_selected_query(tmp_path: Path) -> None:
    data_dir = tmp_path / "runtime"
    _record_decisions(data_dir)
    damaged = data_dir / "sessions" / f"{'f' * 64}.sqlite3"
    damaged.touch(mode=0o600)

    with pytest.raises(SnapshotError, match="runtime_schema_invalid"):
        read_selected_runtime([SESSION], data_dir)


def test_invalid_price_provenance_aborts_without_public_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, omp_root, data_dir, output = _fixture(tmp_path)
    output.parent.mkdir(parents=True)
    output.write_text("previous public snapshot\n", encoding="utf-8")
    from laconic.pages import snapshot as module
    from laconic.pricing.registry import PriceRegistry

    monkeypatch.setattr(
        module,
        "load_registry",
        lambda _: PriceRegistry(rates={}, source="unpinned", source_commit=None, overrides=0),
    )

    with pytest.raises(SnapshotError, match="pricing_provenance_invalid"):
        generate_snapshot(
            repo_root=repo,
            manifest_dir=repo / ".laconic" / "research" / "pages-snapshot",
            output=output,
            omp_roots=[omp_root],
            claude_roots=[],
            data_dir=data_dir,
            now=NOW,
            generator_commit=COMMIT,
        )
    assert output.read_text() == "previous public snapshot\n"


def test_public_contract_rejects_broad_report_unknown_keys_and_private_values() -> None:
    evidence = _composition(emitted=True)
    broad = build_report(
        join(
            [
                SessionUsage(
                    session_id=SESSION,
                    turns=(_turn(),),
                    turns_without_usage=0,
                    malformed_lines=0,
                    unknown_usage_keys=frozenset(),
                    nested=False,
                )
            ],
            [
                SessionDecisions(
                    session_id=SESSION,
                    eligible=1,
                    emitted=1,
                    raw_chars=1000,
                    visible_chars=100,
                    full_expansions=0,
                    span_expansions=0,
                )
            ],
        )
    ).payload
    with pytest.raises(PagesEvidenceError):
        PagesEvidence(broad)

    leaked = json.loads(evidence.to_json())
    leaked["session_hash"] = "private"
    with pytest.raises(PagesEvidenceError):
        PagesEvidence(leaked)

    leaked = json.loads(evidence.to_json())
    leaked["cohort"] = "/Users/private/repository"
    with pytest.raises(PagesEvidenceError):
        PagesEvidence(leaked)

    detached = evidence.payload
    detached["provider_calls"] = 1
    assert evidence.payload["provider_calls"] == 0


def test_public_contract_rejects_arithmetic_claim_and_limitation_drift() -> None:
    evidence = _composition(emitted=True)
    inconsistent = json.loads(evidence.to_json())
    inconsistent["observed"]["chars_avoided"] += 1
    with pytest.raises(PagesEvidenceError, match="arithmetic"):
        PagesEvidence(inconsistent)

    measured = json.loads(evidence.to_json())
    measured["estimate"]["basis"] = "measured_savings"
    with pytest.raises(PagesEvidenceError):
        PagesEvidence(measured)

    weakened = json.loads(evidence.to_json())
    weakened["limitations"] = list(reversed(LIMITATIONS))
    with pytest.raises(PagesEvidenceError, match="limitations"):
        PagesEvidence(weakened)

    inverted_reread = json.loads(evidence.to_json())
    inverted_reread["estimate"]["reread_credited_low"] = 2.0
    inverted_reread["estimate"]["reread_credited_high"] = 1.0
    with pytest.raises(PagesEvidenceError, match="band is inverted"):
        PagesEvidence(inverted_reread)


def test_fallback_gate_uses_the_serialized_rounded_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    monkeypatch.setattr(
        Composition,
        "fallback_priced_cost_share",
        lambda _self, _sessions=None: 0.250000004,
    )

    evidence = _composition(emitted=True)

    assert evidence.payload["estimate"]["fallback_priced_cost_share_pct"] == 25.0
    assert evidence.payload["estimate"]["status"] == "available"


def test_zero_emission_result_is_preserved_as_valid_unfavourable_evidence() -> None:
    evidence = _composition(emitted=False)

    assert evidence.payload["observed"]["emitted"] == 0
    assert evidence.payload["observed"]["chars_avoided"] == 0
    assert evidence.payload["estimate"] == {
        "basis": "modelled_not_measured",
        "status": "withheld",
        "reason": "insufficient_model_inputs",
        "denominator": evidence.payload["estimate"]["denominator"],
        "fallback_priced_cost_share_pct": evidence.payload["estimate"][
            "fallback_priced_cost_share_pct"
        ],
    }


def test_fallback_price_gate_withholds_dollar_range() -> None:
    evidence = _composition(emitted=True, model="unpriced-private-model")

    assert evidence.payload["estimate"]["status"] == "withheld"
    assert evidence.payload["estimate"]["reason"] == "fallback_price_share_above_25_percent"
    assert "avoided_cost_usd_low" not in evidence.payload["estimate"]


def test_real_fixture_generation_seals_private_manifest_and_publishes_exact_keys(
    tmp_path: Path,
) -> None:
    evidence = _generate(tmp_path)
    public_text = evidence.to_json()

    assert set(json.loads(public_text)) == set(evidence.payload)
    assert evidence.payload["provider_calls"] == 0
    assert SESSION not in public_text
    assert str(tmp_path) not in public_text
    assert "PRIVATE CONTENT" not in public_text
    assert evidence.payload["observed"]["eligible"] == 1
    assert evidence.payload["observed"]["emitted"] == 1
    manifest = tmp_path / "repo" / ".laconic" / "research" / "pages-snapshot"
    assert (manifest / "manifest.json").stat().st_mode & 0o777 == 0o400
    assert json.loads((manifest / "ready.json").read_text())["status"] == "validated"


def test_ready_receipt_failure_preserves_prior_public_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, omp_root, data_dir, output = _fixture(tmp_path)
    output.parent.mkdir(parents=True)
    output.write_text("previous public snapshot\n", encoding="utf-8")
    from laconic.pages import snapshot as module

    original = module._record_receipt

    def fail_ready(directory: Path, name: str, payload: dict[str, Any]) -> None:
        if name == "ready.json":
            raise SnapshotError("receipt_write_failed")
        original(directory, name, payload)

    monkeypatch.setattr(module, "_record_receipt", fail_ready)
    manifest_dir = repo / ".laconic" / "research" / "pages-snapshot"
    with pytest.raises(SnapshotError, match="receipt_write_failed"):
        generate_snapshot(
            repo_root=repo,
            manifest_dir=manifest_dir,
            output=output,
            omp_roots=[omp_root],
            claude_roots=[],
            data_dir=data_dir,
            now=NOW,
            generator_commit=COMMIT,
        )

    assert output.read_text() == "previous public snapshot\n"
    assert json.loads((manifest_dir / "failure.json").read_text()) == {
        "reason": "receipt_write_failed",
        "status": "failed",
    }


def test_source_mutation_aborts_without_replacing_public_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, omp_root, data_dir, output = _fixture(tmp_path)
    output.parent.mkdir(parents=True)
    output.write_text("previous public snapshot\n", encoding="utf-8")
    from laconic.pages import snapshot as module

    original = module.discover_inventory
    calls = 0

    def mutating_discovery(**kwargs: Any) -> tuple[Any, ...]:
        nonlocal calls
        calls += 1
        if calls == 2:
            transcript = next(omp_root.rglob("*.jsonl"))
            transcript.write_text(transcript.read_text() + "\n", encoding="utf-8")
            _closed(transcript)
        return original(**kwargs)

    monkeypatch.setattr(module, "discover_inventory", mutating_discovery)
    manifest_dir = repo / ".laconic" / "research" / "pages-snapshot"
    with pytest.raises(SnapshotError, match="source_mutated"):
        generate_snapshot(
            repo_root=repo,
            manifest_dir=manifest_dir,
            output=output,
            omp_roots=[omp_root],
            claude_roots=[],
            data_dir=data_dir,
            now=NOW,
            generator_commit=COMMIT,
        )

    assert output.read_text() == "previous public snapshot\n"
    failure = json.loads((manifest_dir / "failure.json").read_text())
    assert failure == {"reason": "source_mutated", "status": "failed"}
    with pytest.raises(SnapshotError, match="manifest_attempt_already_exists"):
        generate_snapshot(
            repo_root=repo,
            manifest_dir=manifest_dir,
            output=output,
            omp_roots=[omp_root],
            claude_roots=[],
            data_dir=data_dir,
            now=NOW + timedelta(minutes=1),
            generator_commit=COMMIT,
        )


def test_selected_row_mutation_aborts_without_replacing_public_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, omp_root, data_dir, output = _fixture(tmp_path)
    output.parent.mkdir(parents=True)
    output.write_text("previous public snapshot\n", encoding="utf-8")
    from laconic.pages import snapshot as module

    original = module.read_selected_runtime
    calls = 0

    def mutating_runtime(session_ids: list[str], selected_data_dir: Path) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            storage = RuntimeStorage(selected_data_dir)
            with storage.open_existing_ledger(SESSION) as ledger:
                ledger.record_runtime_expansion(
                    request_id="expand-2",
                    reference="F1:1-2",
                    span=True,
                    created_at=3.0,
                )
        return original(session_ids, selected_data_dir)

    monkeypatch.setattr(module, "read_selected_runtime", mutating_runtime)
    with pytest.raises(SnapshotError, match="selected_runtime_rows_mutated"):
        generate_snapshot(
            repo_root=repo,
            manifest_dir=repo / ".laconic" / "research" / "pages-snapshot",
            output=output,
            omp_roots=[omp_root],
            claude_roots=[],
            data_dir=data_dir,
            now=NOW,
            generator_commit=COMMIT,
        )
    assert output.read_text() == "previous public snapshot\n"


def test_private_extractor_script_refuses_ci_without_reading_sources(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import scripts.generate_pages_snapshot as script

    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(
        script,
        "generate_snapshot",
        lambda **_: pytest.fail("CI refusal attempted private extraction"),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "generate_pages_snapshot.py",
            "--repo-root",
            ".",
            "--manifest-dir",
            ".laconic/research/pages-snapshot",
            "--output",
            "docs/pages/evidence/laconic-development.json",
        ],
    )

    assert script.main() == 1
    assert (
        capsys.readouterr().err
        == "pages snapshot generation failed: private_extraction_forbidden_in_ci\n"
    )


def test_extraction_boundary_refuses_ci_before_source_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from laconic.pages import snapshot as module

    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(
        module,
        "discover_inventory",
        lambda **_: pytest.fail("CI refusal attempted source discovery"),
    )

    with pytest.raises(SnapshotError, match="private_extraction_forbidden_in_ci"):
        module.generate_snapshot(
            repo_root=tmp_path,
            manifest_dir=tmp_path / ".laconic" / "research" / "pages-snapshot",
            output=tmp_path / "docs" / "pages" / "evidence" / "laconic-development.json",
        )


def test_private_manifest_destination_must_equal_ignored_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    from laconic.pages import snapshot as module

    monkeypatch.setattr(
        module,
        "discover_inventory",
        lambda **_: pytest.fail("invalid destination attempted source discovery"),
    )

    with pytest.raises(SnapshotError, match="destination_invalid"):
        generate_snapshot(
            repo_root=repo,
            manifest_dir=repo / ".laconic" / "research" / "pages-snapshot-typo",
            output=repo / "docs" / "pages" / "evidence" / "laconic-development.json",
            generator_commit=COMMIT,
        )
    assert not (repo / ".laconic" / "research" / "pages-snapshot-typo").exists()


def test_receipt_write_failure_is_explicit(tmp_path: Path) -> None:
    from laconic.pages import snapshot as module

    with pytest.raises(SnapshotError, match="receipt_write_failed"):
        module._record_receipt(tmp_path / "missing", "failure.json", {"status": "failed"})


def test_generator_commit_must_be_closed_40_hex_before_any_public_write(tmp_path: Path) -> None:
    repo, omp_root, data_dir, output = _fixture(tmp_path)

    with pytest.raises(SnapshotError, match="generator_commit_unverifiable"):
        generate_snapshot(
            repo_root=repo,
            manifest_dir=repo / ".laconic" / "research" / "pages-snapshot",
            output=output,
            omp_roots=[omp_root],
            claude_roots=[],
            data_dir=data_dir,
            now=NOW,
            generator_commit="branch-tip",
        )
    assert not output.exists()


def test_manifest_digest_binds_selected_inventory_and_runtime_rows(tmp_path: Path) -> None:
    repo, omp_root, data_dir, output = _fixture(tmp_path)
    result = generate_snapshot(
        repo_root=repo,
        manifest_dir=repo / ".laconic" / "research" / "pages-snapshot",
        output=output,
        omp_roots=[omp_root],
        claude_roots=[],
        data_dir=data_dir,
        now=NOW,
        generator_commit=COMMIT,
    )
    manifest_bytes = (
        repo / ".laconic" / "research" / "pages-snapshot" / "manifest.json"
    ).read_bytes()
    import hashlib

    assert hashlib.sha256(manifest_bytes).hexdigest() == result.manifest_sha256
    assert result.evidence.payload["manifest_sha256"] == result.manifest_sha256
    assert result.evidence.payload["source_inventory_sha256"] == result.source_inventory_sha256
