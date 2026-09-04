"""Namespaced runtime reference parsing and exact cross-session recovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from laconic.ledger import ObservationKind
from laconic.runtime.references import (
    InvalidRuntimeReferenceError,
    InvalidSessionIdError,
    RuntimeReference,
    validate_session_id,
)
from laconic.runtime.storage import RuntimeStorage, SessionLedgerNotFoundError


def test_full_and_span_references_round_trip() -> None:
    full = RuntimeReference.parse("session-42/F3")
    span = RuntimeReference.parse("session-42/F3:61-94")

    assert (full.session_id, full.ledger_reference, str(full)) == (
        "session-42",
        "F3",
        "session-42/F3",
    )
    assert (span.first_line, span.last_line, span.ledger_reference, str(span)) == (
        61,
        94,
        "F3:61-94",
        "session-42/F3:61-94",
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "session",
        "session/F0",
        "session/F01",
        "session/Q1",
        "session/F1:0-1",
        "session/F1:2-1",
        "session/F1:1-",
        "../session/F1",
        "session/../../F1",
    ],
)
def test_malformed_runtime_references_fail_loudly(value: str) -> None:
    with pytest.raises(InvalidRuntimeReferenceError, match="runtime reference"):
        RuntimeReference.parse(value)


def test_unsafe_session_ids_are_rejected_without_coercion() -> None:
    with pytest.raises(InvalidSessionIdError, match="session id"):
        validate_session_id("../../outside")


def test_namespaced_reference_selects_exactly_one_session_ledger(tmp_path: Path) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    with storage.open_ledger("parent-session") as parent:
        parent.register(ObservationKind.FILE, "a.py", "parent\nsecond", "encoded", 1)
    with storage.open_ledger("child-session") as child:
        child.register(ObservationKind.FILE, "a.py", "child\nsecond", "encoded", 1)

    assert storage.expand("parent-session/F1") == "parent\nsecond"
    assert storage.expand("child-session/F1:1-1") == "child"


def test_missing_session_fails_without_creating_a_ledger(tmp_path: Path) -> None:
    storage = RuntimeStorage(tmp_path / "data")
    missing_path = storage.ledger_path("missing-session")

    with pytest.raises(SessionLedgerNotFoundError, match="missing-session"):
        storage.expand("missing-session/F1")

    assert not missing_path.exists()
