"""M1 deliverable: the GO/NO-GO event matrix across every known client."""

from __future__ import annotations

from laconic.observe.contracts import ClientId
from laconic.observe.report import build_matrix


def test_matrix_covers_both_known_clients() -> None:
    matrix = build_matrix()
    clients = {verdict.contract.client for verdict in matrix}
    assert clients == {ClientId.CLAUDE_CODE, ClientId.OMP}


def test_both_clients_are_go() -> None:
    matrix = build_matrix()
    assert all(verdict.verdict == "GO" for verdict in matrix)


def test_go_verdict_cites_a_reason() -> None:
    matrix = build_matrix()
    assert all(verdict.reason for verdict in matrix)
