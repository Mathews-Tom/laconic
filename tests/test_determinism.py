"""The determinism invariant: same input, same handles, same encodings.

A ledger that mints a different handle for the same observation rewrites the
model's prefix on every turn, and a rewritten prefix converts a 0.10x cache
read into a 1.25x cache write. Non-determinism here does not degrade the cost
thesis, it inverts it. So this suite pins the ledger's output across
independent instances and across separate processes, including a fresh
interpreter hash seed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from laconic.ledger import Ledger, ObservationKind

type Observation = tuple[str, str, str, str, int]

#: Every fingerprint runs under one session id, so handles are comparable.
SESSION = "fixed-session"

#: A fixed session: several kinds interleaved, one byte-identical
#: re-observation to exercise dedup, and payloads with awkward line endings.
SEQUENCE: list[Observation] = [
    ("F", "src/auth/tokens.py", "def verify():\n    return True\n", "F1 outline", 1),
    ("B", "pytest -q", "1 failed\nE   assert 0\n", "B1 errors", 1),
    ("S", "grep -rn check_token src/", "src/a.py:12\nsrc/b.py:44\n", "S1 2 hits", 2),
    ("F", "src/auth/tokens.py", "def verify():\n    return True\n", "F1 outline again", 3),
    ("F", "src/auth/store.py", "class Store:\r\n    pass\r\n", "F2 outline", 3),
    ("W", "https://example.test/doc", "\u4e2d\u6587 \U0001f600\n", "W1 fetched", 4),
    ("X", "unknown-tool", "", "X1 empty", 5),
]


def fingerprint(db_path: str, sequence: list[Observation]) -> dict[str, Any]:
    """Everything about a session that must not vary between runs.

    Deliberately excludes ``created_at``: wall-clock time is the one field
    that legitimately differs, and including it would make the suite fail for
    a reason that has nothing to do with the invariant.
    """
    with Ledger(db_path, SESSION) as ledger:
        records = [
            ledger.register(ObservationKind(kind), subject, raw, encoded, turn)
            for kind, subject, raw, encoded, turn in sequence
        ]
        summary = {
            "handles": [record.handle for record in records],
            "shas": [record.content_sha for record in records],
            "encodings": [record.encoded for record in records],
            "expansions": [ledger.expand(record.handle) for record in records],
            "first_lines": [ledger.expand(f"{record.handle}:1-1") for record in records],
        }
    return summary | {"stored": _stored_blobs(db_path)}


def _stored_blobs(db_path: str) -> list[str]:
    """The compressed bytes on disk, read as an outside observer."""
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute(
            "SELECT raw FROM observations WHERE session_id = ? ORDER BY handle",
            (SESSION,),
        ).fetchall()
    finally:
        db.close()
    return [bytes(blob).hex() for (blob,) in rows]


def _in_another_process(
    db_path: Path, hash_seed: str, sequence: list[Observation] | None = None
) -> dict[str, Any]:
    """Replay a sequence in a fresh interpreter and bring back its fingerprint.

    The worker reads its sequence from stdin, so the property leg can hand it
    generated input rather than only the fixed session.
    """
    source = (
        "import json, sys;"
        f"sys.path.insert(0, {str(Path(__file__).parent)!r});"
        "from test_determinism import fingerprint;"
        "print(json.dumps(fingerprint(sys.argv[1], "
        "[tuple(item) for item in json.load(sys.stdin)])))"
    )
    result = subprocess.run(
        [sys.executable, "-c", source, str(db_path)],
        input=json.dumps(SEQUENCE if sequence is None else sequence),
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
    )
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


def test_the_same_session_replays_identically_in_this_process(tmp_path: Path) -> None:
    first = fingerprint(str(tmp_path / "first.db"), SEQUENCE)
    second = fingerprint(str(tmp_path / "second.db"), SEQUENCE)
    assert first == second


def test_the_same_session_replays_identically_in_another_process(tmp_path: Path) -> None:
    local = fingerprint(str(tmp_path / "local.db"), SEQUENCE)
    remote = _in_another_process(tmp_path / "remote.db", hash_seed="0")
    assert local == remote


def test_a_different_interpreter_hash_seed_changes_nothing(tmp_path: Path) -> None:
    """Handle ordering must come from the sequence, not from dict iteration."""
    first = _in_another_process(tmp_path / "seed-a.db", hash_seed="1")
    second = _in_another_process(tmp_path / "seed-b.db", hash_seed="99991")
    assert first == second


def test_the_fixed_session_mints_the_expected_handles(tmp_path: Path) -> None:
    """Pins the contract itself, so a silent renumbering is visible in review."""
    handles = fingerprint(str(tmp_path / "ledger.db"), SEQUENCE)["handles"]
    assert handles == ["F1", "B1", "S1", "F1", "F2", "W1", "X1"]


def test_replaying_into_an_existing_database_is_stable(tmp_path: Path) -> None:
    """A resumed session re-registers the same content without renumbering."""
    db_path = tmp_path / "ledger.db"
    first = fingerprint(str(db_path), SEQUENCE)
    second = fingerprint(str(db_path), SEQUENCE)
    assert first == second


def test_a_resumed_session_continues_the_numbering(tmp_path: Path) -> None:
    """New content after a reopen must not land on a handle that already exists."""
    db_path = tmp_path / "ledger.db"
    fingerprint(str(db_path), SEQUENCE)
    appended = SEQUENCE + [("F", "src/auth/new.py", "fresh content\n", "F3 outline", 6)]
    assert fingerprint(str(db_path), appended)["handles"][-1] == "F3"


SESSIONS = st.lists(
    st.tuples(
        st.sampled_from([kind.value for kind in ObservationKind]),
        st.text(st.characters(codec="utf-8"), max_size=40),
        st.text(st.characters(codec="utf-8"), max_size=400),
        st.text(st.characters(codec="utf-8"), max_size=40),
        st.integers(min_value=0, max_value=100),
    ),
    min_size=1,
    max_size=10,
)


@settings(deadline=None, max_examples=15)
@given(sequence=SESSIONS)
def test_any_session_replays_identically_in_another_process(
    tmp_path_factory: pytest.TempPathFactory, sequence: list[Observation]
) -> None:
    """The in-process leg cannot see cross-process drift; this one can.

    Fewer examples than its in-process sibling because each one pays for an
    interpreter start.
    """
    directory = tmp_path_factory.mktemp("determinism-remote")
    local = fingerprint(str(directory / "local.db"), sequence)
    remote = _in_another_process(directory / "remote.db", hash_seed="7", sequence=sequence)
    assert local == remote


@settings(deadline=None, max_examples=100)
@given(sequence=SESSIONS)
def test_any_session_replays_identically(
    tmp_path_factory: pytest.TempPathFactory, sequence: list[Observation]
) -> None:
    directory = tmp_path_factory.mktemp("determinism")
    first = fingerprint(str(directory / "first.db"), sequence)
    second = fingerprint(str(directory / "second.db"), sequence)
    assert first == second
