"""Synthetic, zero-spend tests for K1 Stage C's batch core."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from laconic.k1corpus.stage_a import Provider
from laconic.k1corpus.stage_b import ManifestSet
from laconic.k1corpus.stage_c import (
    RETAILOGISTS_EXCLUDED_LINEAGES,
    OriginalModelError,
    ResolvedStageCSession,
    StageCManifestEntry,
    StageCManifestError,
    load_stage_c_manifest,
    resolve_original_model,
    run_untracked_batch,
)

_INCLUDED_LINEAGE = "lineage:allowed"
_EXCLUDED_LINEAGE = next(iter(RETAILOGISTS_EXCLUDED_LINEAGES))


def _row(*, session_id: str, lineage: str, set_name: str = "design") -> dict[str, str]:
    return {
        "set": set_name,
        "provider": "claude-code",
        "session_id": session_id,
        "project_lineage_id": lineage,
    }


def _write_manifest(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    path = tmp_path / "session_manifest.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def _write_baseline(path: Path, models: list[str]) -> None:
    records = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": model,
                "content": [],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }
        for model in models
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


@dataclass
class FakeRunner:
    costs: dict[str, float]
    calls: list[tuple[str, str, float]] = field(default_factory=list)

    def run(self, session: ResolvedStageCSession, *, cost_cap_usd: float) -> float:
        self.calls.append((session.entry.session_id, session.model, cost_cap_usd))
        return self.costs[session.entry.session_id]


def test_manifest_loader_filters_retailogists_before_fake_client_can_receive_id(
    tmp_path: Path,
) -> None:
    included = "claude-code:included"
    excluded = "claude-code:excluded"
    manifest = _write_manifest(
        tmp_path,
        [
            _row(session_id=included, lineage=_INCLUDED_LINEAGE),
            _row(session_id=excluded, lineage=_EXCLUDED_LINEAGE),
        ],
    )
    loaded = load_stage_c_manifest(manifest, selected_set=ManifestSet.DESIGN)
    baseline = tmp_path / "included.jsonl"
    _write_baseline(baseline, ["claude-sonnet-5"])
    runner = FakeRunner({included: 0.25})

    results = run_untracked_batch(
        loaded.entries,
        spend_cap_usd=1.0,
        runner=runner,
        resolver=lambda _provider, session_id: baseline if session_id == included else None,
    )

    assert [entry.session_id for entry in loaded.excluded_retailogists] == [excluded]
    assert [call[0] for call in runner.calls] == [included]
    assert excluded not in [result.session_id for result in results]


def test_batch_cap_stops_future_sessions_after_realized_overage(tmp_path: Path) -> None:
    entries = tuple(
        StageCManifestEntry(
            set=ManifestSet.DESIGN,
            provider=Provider.CLAUDE_CODE,
            session_id=f"claude-code:{index}",
            project_lineage_id=_INCLUDED_LINEAGE,
        )
        for index in range(3)
    )
    baselines = []
    for index in range(3):
        path = tmp_path / f"session-{index}.jsonl"
        _write_baseline(path, ["claude-sonnet-5"])
        baselines.append(path)
    runner = FakeRunner({entry.session_id: 0.6 for entry in entries})

    results = run_untracked_batch(
        entries,
        spend_cap_usd=1.0,
        runner=runner,
        resolver=lambda _provider, session_id: baselines[int(session_id.rsplit(":", 1)[1])],
    )

    assert [call[0] for call in runner.calls] == [entries[0].session_id, entries[1].session_id]
    assert [result.outcome for result in results] == [
        "completed",
        "completed",
        "batch_cap_exceeded",
    ]
    assert runner.calls[0][2] == pytest.approx(1.0)
    assert runner.calls[1][2] == pytest.approx(0.4)


def test_missing_or_multi_model_baseline_never_reaches_runner(tmp_path: Path) -> None:
    entry = StageCManifestEntry(
        set=ManifestSet.DESIGN,
        provider=Provider.CLAUDE_CODE,
        session_id="claude-code:ambiguous",
        project_lineage_id=_INCLUDED_LINEAGE,
    )
    baseline = tmp_path / "ambiguous.jsonl"
    _write_baseline(baseline, ["claude-sonnet-5", "gpt-5.6-terra"])
    runner = FakeRunner({entry.session_id: 0.1})

    results = run_untracked_batch(
        (entry,),
        spend_cap_usd=1.0,
        runner=runner,
        resolver=lambda _provider, _session_id: baseline,
    )

    assert results[0].outcome == "model_unresolved"
    assert runner.calls == []
    with pytest.raises(OriginalModelError, match="claude-sonnet-5, gpt-5.6-terra"):
        resolve_original_model(baseline)


def test_loader_rejects_empty_filtered_set(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path,
        [_row(session_id="claude-code:x", lineage=_EXCLUDED_LINEAGE)],
    )

    with pytest.raises(StageCManifestError, match="no eligible 'design' sessions"):
        load_stage_c_manifest(manifest, selected_set=ManifestSet.DESIGN)
