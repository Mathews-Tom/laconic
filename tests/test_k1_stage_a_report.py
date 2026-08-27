"""Tests for `laconic.k1corpus.report`: stop-condition evaluation, report
serialization, and atomic ledger write. No test touches a real
`~/.claude`/`~/.codex`/`~/.omp` path."""

from __future__ import annotations

import json
import stat
from collections import Counter
from pathlib import Path

from laconic.k1corpus.report import (
    DEFAULT_LEDGER_PATH,
    Disposition,
    build_report,
    compute_disposition,
    evaluate_stop_conditions,
    write_ledger,
)
from laconic.k1corpus.stage_a import (
    AgeBand,
    ClosureStatus,
    ExclusionReason,
    Provider,
    SessionRecord,
    SizeBand,
)


def _record(provider: Provider, lineage: str, session_id: str) -> SessionRecord:
    return SessionRecord(
        provider=provider,
        session_id=session_id,
        project_lineage_id=lineage,
        closure_status=ClosureStatus.CLOSED,
        size_band=SizeBand.S,
        age_band=AgeBand.D1_7,
        provenance_hash="a" * 64,
    )


# --- evaluate_stop_conditions ---------------------------------------------


def test_stop_condition_fires_for_too_few_lineages() -> None:
    records = [
        _record(Provider.CLAUDE_CODE, "lineage:1", "claude-code:a"),
        _record(Provider.CODEX, "lineage:2", "codex:b"),
    ]
    conditions = evaluate_stop_conditions(records)
    by_name = {c.name: c for c in conditions}
    assert by_name["distinct_lineage_count"].fired is True
    assert compute_disposition(conditions) is Disposition.STOP


def test_stop_condition_passes_with_three_lineages_and_providers() -> None:
    records = [
        _record(Provider.CLAUDE_CODE, "lineage:1", "claude-code:a"),
        _record(Provider.CODEX, "lineage:2", "codex:b"),
        _record(Provider.OMP, "lineage:3", "omp:c"),
    ]
    conditions = evaluate_stop_conditions(records)
    assert all(not c.fired for c in conditions)
    assert compute_disposition(conditions) is Disposition.PROCEED_TO_STAGE_B_REQUEST


def test_stop_condition_fires_for_single_provider_surface() -> None:
    records = [
        _record(Provider.CLAUDE_CODE, "lineage:1", "claude-code:a"),
        _record(Provider.CLAUDE_CODE, "lineage:2", "claude-code:b"),
        _record(Provider.CLAUDE_CODE, "lineage:3", "claude-code:c"),
    ]
    conditions = evaluate_stop_conditions(records)
    by_name = {c.name: c for c in conditions}
    assert by_name["single_provider_surface"].fired is True
    assert by_name["distinct_lineage_count"].fired is False
    assert compute_disposition(conditions) is Disposition.STOP


def test_stop_condition_fires_for_zero_admitted_sessions() -> None:
    conditions = evaluate_stop_conditions([])
    by_name = {c.name: c for c in conditions}
    assert by_name["no_closed_sessions"].fired is True
    assert by_name["distinct_lineage_count"].fired is True
    assert compute_disposition(conditions) is Disposition.STOP


def test_ambiguous_association_condition_never_auto_fires() -> None:
    # Stage A structurally admits only unambiguous cwd matches; this
    # condition is reported for human review, never computed as fired.
    conditions = evaluate_stop_conditions([])
    by_name = {c.name: c for c in conditions}
    assert by_name["ambiguous_association"].fired is False


# --- build_report ----------------------------------------------------------


def test_build_report_combines_per_provider_records_and_exclusions() -> None:
    per_provider = {
        Provider.CLAUDE_CODE: (
            [_record(Provider.CLAUDE_CODE, "lineage:1", "claude-code:a")],
            Counter({ExclusionReason.ACTIVE: 2}),
        ),
        Provider.CODEX: ([], Counter({ExclusionReason.OUTSIDE_ALLOWLIST: 1})),
        Provider.OMP: ([], Counter()),
    }
    report = build_report(1000.0, per_provider)
    assert len(report.records) == 1
    assert report.exclusions["claude-code"]["active"] == 2
    assert report.exclusions["codex"]["outside_allowlist"] == 1
    assert report.exclusions["omp"] == {}
    assert report.disposition is Disposition.STOP  # only one lineage


def test_report_to_json_never_contains_a_raw_path() -> None:
    per_provider = {
        Provider.CLAUDE_CODE: (
            [_record(Provider.CLAUDE_CODE, "lineage:1", "claude-code:a")],
            Counter(),
        ),
        Provider.CODEX: ([], Counter()),
        Provider.OMP: ([], Counter()),
    }
    report = build_report(1000.0, per_provider)
    payload = report.to_json()
    serialized = json.dumps(payload)
    assert "/Users/" not in serialized
    assert "root_a" in serialized or "root_b" in serialized
    assert set(payload["roots"]) == {"root_a", "root_b"}  # type: ignore[arg-type]


# --- write_ledger: atomic, mode-restricted -----------------------------


def test_write_ledger_creates_mode_restricted_dir_and_file(tmp_path: Path) -> None:
    per_provider = {
        Provider.CLAUDE_CODE: ([], Counter()),
        Provider.CODEX: ([], Counter()),
        Provider.OMP: ([], Counter()),
    }
    report = build_report(1000.0, per_provider)
    out_path = tmp_path / "nested" / "ledger.json"
    write_ledger(report, out_path)

    assert out_path.exists()
    dir_mode = stat.S_IMODE(out_path.parent.stat().st_mode)
    file_mode = stat.S_IMODE(out_path.stat().st_mode)
    assert dir_mode == 0o700
    assert file_mode == 0o600

    loaded = json.loads(out_path.read_text())
    assert loaded["disposition"] == "stop"  # zero records -> stop


def test_write_ledger_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    per_provider = {
        Provider.CLAUDE_CODE: ([], Counter()),
        Provider.CODEX: ([], Counter()),
        Provider.OMP: ([], Counter()),
    }
    report = build_report(1000.0, per_provider)
    out_path = tmp_path / "ledger.json"
    write_ledger(report, out_path)
    remaining = list(tmp_path.iterdir())
    assert remaining == [out_path]


def test_default_ledger_path_is_under_gitignored_laconic_k1() -> None:
    assert DEFAULT_LEDGER_PATH == Path(".laconic/k1/stage_a/ledger.json")
