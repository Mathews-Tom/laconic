"""Local Observe status/report views: reads only the audit file."""

from __future__ import annotations

import json
from pathlib import Path

from laconic.observe.audit import append_to_file
from laconic.observe.status import compute_report, compute_status

_CC_RECEIPT = {
    "adapter": "claude-code",
    "tool_category": "file_write",
    "result_class": "success",
    "argument_size": "xs",
    "result_size": "s",
}

_OMP_RECEIPT = {
    "adapter": "omp",
    "tool_category": "command",
    "result_class": "failure",
    "argument_size": "s",
    "result_size": "m",
}


def test_status_on_missing_file_is_empty_and_valid(tmp_path: Path) -> None:
    status = compute_status(tmp_path / "audit.jsonl")
    assert status.exists is False
    assert status.entry_count == 0
    assert status.chain_valid is True
    assert status.integrity_error is None


def test_status_reflects_written_entries(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    append_to_file(target, _CC_RECEIPT)
    append_to_file(target, _OMP_RECEIPT)
    status = compute_status(target)
    assert status.exists is True
    assert status.entry_count == 2
    assert status.chain_valid is True


def test_status_detects_a_tampered_file(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    append_to_file(target, _CC_RECEIPT)
    text = target.read_text().replace('"file_write"', '"network"')
    target.write_text(text)
    status = compute_status(target)
    assert status.chain_valid is False
    assert status.integrity_error is not None


def test_report_on_missing_file_has_zero_entries(tmp_path: Path) -> None:
    report = compute_report(tmp_path / "audit.jsonl")
    assert report.entry_count == 0
    assert report.by_adapter == {}


def test_report_breaks_down_by_every_allowlisted_dimension(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    append_to_file(target, _CC_RECEIPT)
    append_to_file(target, _OMP_RECEIPT)
    report = compute_report(target)
    assert report.entry_count == 2
    assert report.by_adapter == {"claude-code": 1, "omp": 1}
    assert report.by_tool_category == {"file_write": 1, "command": 1}
    assert report.by_result_class == {"success": 1, "failure": 1}
    assert report.by_argument_size == {"xs": 1, "s": 1}
    assert report.by_result_size == {"s": 1, "m": 1}


def test_report_counts_repeated_categories_together(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    append_to_file(target, _CC_RECEIPT)
    append_to_file(target, _CC_RECEIPT)
    report = compute_report(target)
    assert report.by_adapter == {"claude-code": 2}


def test_status_and_report_json_are_json_safe(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    append_to_file(target, _CC_RECEIPT)
    json.dumps(compute_status(target).to_json())
    json.dumps(compute_report(target).to_json())
