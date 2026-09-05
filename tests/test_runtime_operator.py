from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from laconic.runtime.operator import (
    apply_purge,
    parse_duration,
    preview_purge_older_than,
    preview_purge_session,
    runtime_storage_status,
)
from laconic.runtime.storage import (
    RuntimeStorage,
    UnsafeStoragePathError,
    resolve_data_dir,
    session_ledger_path,
)


def _abandon_writer_mid_transaction(ledger: Path) -> None:
    """Leave a genuinely hot rollback journal behind, as a killed engine does.

    A tiny page cache forces the uncommitted rows out to the journal before the
    writer disappears without unwinding its transaction, so the next reader
    must roll that journal back before it can read anything.
    """
    rows = "[(900 + i, 'x' * 400 + str(i)) for i in range(4000)]"
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sqlite3\n"
            f"db = sqlite3.connect({str(ledger)!r})\n"
            "db.execute('PRAGMA cache_size = 2')\n"
            "db.execute('BEGIN IMMEDIATE')\n"
            "db.executemany('INSERT INTO runtime_decisions (session_id, sequence, "
            "request_id, tool_name, outcome, reason, candidate_reference, raw_chars, "
            "visible_chars, latency_ms, created_at) VALUES (\\'crashed\\', ?, ?, \\'Read\\', "
            "\\'pass_through\\', \\'not_smaller\\', NULL, 5, 5, 1.0, 101.0)', "
            f"{rows})\n"
            "os._exit(0)\n",
        ],
        check=True,
    )
    assert Path(f"{ledger}-journal").exists()


def test_status_and_retention_survive_a_ledger_whose_writer_was_killed(
    tmp_path: Path,
) -> None:
    """A crashed engine leaves a hot journal. Both read-only operator surfaces
    must still work: a read-only SQLite handle cannot roll that journal back,
    so one crashed session used to break `status` and `purge --older-than` for
    the whole store."""
    root = tmp_path / "data"
    storage = RuntimeStorage(root)
    with storage.open_ledger("crashed") as ledger:
        ledger.record_runtime_decision(
            sequence=0,
            request_id="encode-a",
            tool_name="Read",
            outcome="emitted",
            reason="smaller_envelope",
            candidate_reference="crashed/F1",
            raw_chars=100,
            visible_chars=40,
            latency_ms=2,
            created_at=100.0,
        )
    _abandon_writer_mid_transaction(session_ledger_path("crashed", resolve_data_dir(root)))

    status = runtime_storage_status(root)
    plan = preview_purge_older_than(60, root, now=1_000.0)

    # The abandoned transaction rolled back, so only the committed decision counts.
    assert status.sessions == 1
    assert status.eligible_observations == 1
    assert status.compressed_observations == 1
    assert plan.targets == (session_ledger_path("crashed", resolve_data_dir(root)),)


def test_status_on_absent_storage_is_empty_and_does_not_create_it(tmp_path: Path) -> None:
    root = tmp_path / "absent"

    status = runtime_storage_status(root)

    assert status.exists is False
    assert status.sessions == 0
    assert status.storage_bytes == 0
    assert status.eligible_observations == 0
    assert not root.exists()


def test_status_aggregates_only_content_free_runtime_counters(tmp_path: Path) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    with storage.open_ledger("session-a") as ledger:
        ledger.record_runtime_decision(
            sequence=0,
            request_id="encode-a",
            tool_name="Read",
            outcome="emitted",
            reason="smaller_envelope",
            candidate_reference="session-a/F1",
            raw_chars=100,
            visible_chars=40,
            latency_ms=2,
        )
        ledger.record_runtime_expansion(
            request_id="expand-full",
            reference="session-a/F1",
            span=False,
        )
    with storage.open_ledger("session-b") as ledger:
        ledger.record_runtime_decision(
            sequence=0,
            request_id="encode-b",
            tool_name="Bash",
            outcome="pass_through",
            reason="not_smaller",
            candidate_reference=None,
            raw_chars=20,
            visible_chars=20,
            latency_ms=1,
        )
        ledger.record_runtime_expansion(
            request_id="expand-span",
            reference="session-a/F1:1-1",
            span=True,
        )

    status = runtime_storage_status(storage.root)

    assert status.sessions == 2
    assert status.storage_bytes > 0
    assert status.eligible_observations == 2
    assert status.compressed_observations == 1
    assert status.pass_through_observations == 1
    assert status.raw_chars == 120
    assert status.visible_chars == 60
    assert status.full_expansions == 1
    assert status.span_expansions == 1


def test_session_purge_preview_and_apply_are_exact_and_keep_other_files(tmp_path: Path) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    with storage.open_ledger("delete-me"):
        pass
    with storage.open_ledger("keep-me"):
        pass
    foreign = storage.root / "sessions" / "notes.txt"
    foreign.write_text("keep", encoding="utf-8")
    target = storage.ledger_path("delete-me")
    survivor = storage.ledger_path("keep-me")

    plan = preview_purge_session("delete-me", storage.root)

    assert plan.targets == (target,)
    assert target.exists()
    result = apply_purge(plan)
    assert result.deleted_sessions == 1
    assert result.deleted_files == 1
    assert not target.exists()
    assert survivor.exists()
    assert foreign.read_text(encoding="utf-8") == "keep"


def test_older_than_purge_uses_last_activity_and_dry_plan_does_not_delete(
    tmp_path: Path,
) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    with storage.open_ledger("old-session"):
        pass
    with storage.open_ledger("new-session"):
        pass
    old = storage.ledger_path("old-session")
    new = storage.ledger_path("new-session")
    os.utime(old, (100, 100))
    os.utime(new, (900, 900))

    plan = preview_purge_older_than(500, storage.root, now=1000, selector="older-than=500s")

    assert plan.targets == (old,)
    assert old.exists()
    assert new.exists()
    apply_purge(plan)
    assert not old.exists()
    assert new.exists()


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("30s", 30), ("5m", 300), ("24h", 86400), ("30d", 2_592_000), ("4w", 2_419_200)],
)
def test_duration_parser_accepts_explicit_units(value: str, seconds: int) -> None:
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["", "0d", "1", "1.5h", "-2d", "tomorrow"])
def test_duration_parser_rejects_ambiguous_or_nonpositive_values(value: str) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        parse_duration(value)


def test_purge_rejects_a_symlinked_sessions_directory(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (root / "sessions").symlink_to(foreign, target_is_directory=True)

    with pytest.raises(UnsafeStoragePathError, match="must not be a symlink"):
        preview_purge_older_than(60, root, now=1000)
