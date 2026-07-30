"""Tests for private K1 eligibility ledger validation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from laconic.k1.eligibility import (
    EligibilityLedger,
    EligibilityLedgerError,
    EligibilityRecord,
    read_eligibility_ledger,
    write_eligibility_ledger,
)

_DIGEST = "a" * 64


def _ledger() -> EligibilityLedger:
    return EligibilityLedger(
        _DIGEST,
        (
            EligibilityRecord(
                "session-a",
                "b" * 64,
                "confirmatory",
                "native evidence satisfies K1 confirmatory contract",
                "claude-code-jsonl-v1",
                "claude-sonnet-4-6",
                3,
            ),
        ),
    )


def test_private_ledger_round_trip_enforces_permissions(tmp_path: Path) -> None:
    path = tmp_path / "private" / "eligibility.json"

    write_eligibility_ledger(path, _ledger())

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert read_eligibility_ledger(path) == _ledger()


def test_private_ledger_rejects_nonprivate_parent(tmp_path: Path) -> None:
    path = tmp_path / "eligibility.json"
    os.chmod(tmp_path, 0o755)

    with pytest.raises(EligibilityLedgerError, match="directory must have mode 0700"):
        write_eligibility_ledger(path, _ledger())


def test_confirmatory_record_requires_complete_evidence() -> None:
    with pytest.raises(EligibilityLedgerError, match="requires positive event_count"):
        EligibilityRecord(
            "session-a",
            "b" * 64,
            "confirmatory",
            "incomplete",
            "claude-code-jsonl-v1",
            "claude-sonnet-4-6",
            None,
        )
