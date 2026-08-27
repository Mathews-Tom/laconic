"""Hash-chained local audit log: in-memory chain construction, tamper
detection, and file round-tripping across separate process-like calls."""

from __future__ import annotations

from pathlib import Path

import pytest

from laconic.observe.audit import (
    GENESIS_HASH,
    AuditIntegrityError,
    append,
    append_to_file,
    read_chain,
    verify_chain,
)

_RECEIPT_A = {"tool_category": "file_read", "session_id": "a"}
_RECEIPT_B = {"tool_category": "command", "session_id": "a"}


def test_first_entry_chains_to_genesis() -> None:
    chain = append((), _RECEIPT_A)
    assert chain[0].sequence == 0
    assert chain[0].previous_hash == GENESIS_HASH


def test_second_entry_chains_to_first_entry_hash() -> None:
    chain = append(append((), _RECEIPT_A), _RECEIPT_B)
    assert chain[1].previous_hash == chain[0].entry_hash
    assert chain[1].sequence == 1


def test_valid_chain_verifies() -> None:
    chain = append(append((), _RECEIPT_A), _RECEIPT_B)
    verify_chain(chain)  # must not raise


def test_empty_chain_verifies() -> None:
    verify_chain(())  # must not raise


def test_tampered_receipt_content_breaks_verification() -> None:
    chain = append(append((), _RECEIPT_A), _RECEIPT_B)
    tampered_entry = chain[0]
    tampered = (
        type(tampered_entry)(
            sequence=tampered_entry.sequence,
            receipt={**tampered_entry.receipt, "tool_category": "network"},
            previous_hash=tampered_entry.previous_hash,
            entry_hash=tampered_entry.entry_hash,
        ),
        chain[1],
    )
    with pytest.raises(AuditIntegrityError):
        verify_chain(tampered)


def test_dropped_middle_entry_breaks_previous_hash_linkage() -> None:
    chain = append(append(append((), _RECEIPT_A), _RECEIPT_B), _RECEIPT_A)
    gapped = (chain[0], chain[2])
    with pytest.raises(AuditIntegrityError):
        verify_chain(gapped)


def test_reordered_entries_break_previous_hash_linkage() -> None:
    chain = append(append((), _RECEIPT_A), _RECEIPT_B)
    reordered = (chain[1], chain[0])
    with pytest.raises(AuditIntegrityError):
        verify_chain(reordered)


def test_read_chain_on_missing_file_is_empty() -> None:
    assert read_chain(Path("/nonexistent/does/not/exist.jsonl")) == ()


def test_append_to_file_creates_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "audit.jsonl"
    append_to_file(target, _RECEIPT_A)
    assert target.exists()


def test_append_to_file_across_two_calls_chains_correctly(tmp_path: Path) -> None:
    """Each hook invocation is a fresh subprocess; the chain must extend
    correctly across separate `append_to_file` calls, not just within one
    in-memory session."""
    target = tmp_path / "audit.jsonl"
    first = append_to_file(target, _RECEIPT_A)
    second = append_to_file(target, _RECEIPT_B)

    assert first.sequence == 0
    assert second.sequence == 1
    assert second.previous_hash == first.entry_hash

    chain = read_chain(target)
    assert len(chain) == 2
    verify_chain(chain)


def test_read_chain_round_trips_receipt_content(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    append_to_file(target, _RECEIPT_A)
    chain = read_chain(target)
    assert chain[0].receipt == _RECEIPT_A
