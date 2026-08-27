"""Shared Observe adapter-contract model behaviour."""

from __future__ import annotations

from laconic.observe.contracts import (
    AdapterContract,
    ClientId,
    ConfigLocation,
    InstallMechanism,
    ObserveEventKind,
)


def _contract(*events: ObserveEventKind) -> AdapterContract:
    return AdapterContract(
        client=ClientId.CLAUDE_CODE,
        supported_events=events,
        install_mechanism=InstallMechanism.JSON_ENTRY_MERGE,
        config_locations=(ConfigLocation(scope="project", path=".x", shareable=True),),
        default_timeout_seconds=1.0,
        max_timeout_seconds=1.0,
        source="test",
    )


def test_go_requires_completed_result_and_session_close() -> None:
    contract = _contract(ObserveEventKind.TOOL_RESULT_SUCCESS, ObserveEventKind.SESSION_CLOSE)
    assert contract.go() is True


def test_go_is_false_without_session_close() -> None:
    contract = _contract(ObserveEventKind.TOOL_RESULT_SUCCESS)
    assert contract.go() is False


def test_go_is_false_without_completed_result() -> None:
    contract = _contract(ObserveEventKind.SESSION_CLOSE)
    assert contract.go() is False


def test_go_ignores_failure_only_result_event() -> None:
    """`TOOL_RESULT_FAILURE` alone does not satisfy the completed-result
    requirement -- a client must also cover the success path."""
    contract = _contract(ObserveEventKind.TOOL_RESULT_FAILURE, ObserveEventKind.SESSION_CLOSE)
    assert contract.go() is False
