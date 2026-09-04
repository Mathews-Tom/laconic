"""Transport-neutral runtime session behavior and failure boundaries."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from laconic.ledger import Ledger
from laconic.runtime.engine import RuntimeSession
from laconic.runtime.protocol import (
    EncodeObservationRequest,
    EncodeObservationResponse,
    ExpandRequest,
    ExpandResponse,
    InitializeRequest,
    InitializeResponse,
    ProtocolError,
    ProtocolErrorCode,
    RuntimePolicy,
    ShutdownRequest,
)
from laconic.runtime.storage import RuntimeStorage


def _initialize(
    runtime: RuntimeSession,
    tmp_path: Path,
    *,
    request_id: str = "init-1",
    session_id: str = "session-1",
) -> InitializeResponse:
    response = runtime.handle(
        InitializeRequest(
            request_id=request_id,
            session_id=session_id,
            working_directory=str(tmp_path),
            data_directory=str(tmp_path / "data"),
            policy=RuntimePolicy(
                span_budget=20,
                keep_head=2,
                keep_tail=2,
                max_errors=2,
            ),
        )
    )
    assert isinstance(response, InitializeResponse)
    return response


def _compressible_read(
    request_id: str = "encode-1", *, sequence: int = 1
) -> EncodeObservationRequest:
    raw = "\n".join(f"def function_{index}():\n    return {index}" for index in range(300))
    return EncodeObservationRequest(
        request_id=request_id,
        tool_name="Read",
        tool_input={"file_path": "sample.py"},
        raw_text=raw,
        success=True,
        sequence=sequence,
    )


def test_compressible_read_emits_only_after_exact_full_and_span_recovery(tmp_path: Path) -> None:
    runtime = RuntimeSession()
    _initialize(runtime, tmp_path)
    request = _compressible_read()

    encoded = runtime.handle(request)

    assert isinstance(encoded, EncodeObservationResponse)
    assert encoded.decision == "emitted"
    assert encoded.content is not None
    assert encoded.reference is not None
    assert len(encoded.content) < len(request.raw_text)
    assert encoded.content.startswith(f"[laconic {encoded.reference} | full: laconic_expand(")

    full = runtime.handle(ExpandRequest(request_id="expand-1", reference=encoded.reference))
    span = runtime.handle(
        ExpandRequest(request_id="expand-2", reference=f"{encoded.reference}:2-4")
    )
    assert isinstance(full, ExpandResponse)
    assert isinstance(span, ExpandResponse)
    assert full.content == request.raw_text
    assert span.content == "\n".join(request.raw_text.split("\n")[1:4])

    metrics = runtime.metrics()
    assert metrics.eligible_observations == 1
    assert metrics.compressed_observations == 1
    assert metrics.characters_avoided == len(request.raw_text) - len(encoded.content)
    assert metrics.full_expansions == 1
    assert metrics.span_expansions == 1
    assert len(metrics.encoding_latencies_ms) == 1


def test_non_smaller_candidate_passes_through_but_remains_auditable(tmp_path: Path) -> None:
    runtime = RuntimeSession()
    _initialize(runtime, tmp_path)
    request = EncodeObservationRequest(
        request_id="encode-short",
        tool_name="Read",
        tool_input={"file_path": "tiny.py"},
        raw_text="x",
        success=True,
        sequence=1,
    )

    response = runtime.handle(request)

    assert isinstance(response, EncodeObservationResponse)
    assert response.decision == "pass_through"
    assert response.reason == "not_smaller"
    assert response.content is None
    assert response.reference is None
    assert response.visible_chars == len(request.raw_text)
    metrics = runtime.metrics()
    assert metrics.eligible_observations == 1
    assert metrics.compressed_observations == 0
    assert metrics.pass_through_by_reason == (("not_smaller", 1),)

    with RuntimeStorage(tmp_path / "data").open_existing_ledger("session-1") as ledger:
        decisions = ledger.runtime_decisions()
        assert decisions[0].candidate_reference == "session-1/F1"
        assert ledger.expand("F1") == "x"


def test_expansion_metric_failure_never_discards_recovered_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = RuntimeSession()
    _initialize(runtime, tmp_path)
    encoded = runtime.handle(_compressible_read())
    assert isinstance(encoded, EncodeObservationResponse)
    assert encoded.reference is not None

    def fail_metric_write(self: Ledger, **_values: object) -> None:
        raise sqlite3.OperationalError("metric store unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(Ledger, "record_runtime_expansion", fail_metric_write)
        expanded = runtime.handle(
            ExpandRequest(request_id="expand-unrecorded", reference=encoded.reference)
        )

    assert isinstance(expanded, ExpandResponse)
    assert expanded.content == _compressible_read().raw_text
    assert expanded.metric_recorded is False
    assert runtime.metrics().full_expansions == 0


def test_recovery_mismatch_never_emits_the_registered_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = RuntimeSession()
    _initialize(runtime, tmp_path)
    request = _compressible_read()

    with monkeypatch.context() as patch:
        patch.setattr(Ledger, "expand", lambda self, reference: "altered")
        response = runtime.handle(request)

    assert isinstance(response, EncodeObservationResponse)
    assert (response.decision, response.reason) == ("pass_through", "recovery_mismatch")
    assert response.content is None
    assert response.reference is None
    metrics = runtime.metrics()
    assert metrics.eligible_observations == 1
    assert metrics.compressed_observations == 0
    assert RuntimeStorage(tmp_path / "data").expand("session-1/F1") == request.raw_text
    with RuntimeStorage(tmp_path / "data").open_existing_ledger("session-1") as ledger:
        assert ledger.runtime_decisions()[0].candidate_reference == "session-1/F1"


def test_unsupported_tools_and_errors_never_use_the_fallback_encoder(tmp_path: Path) -> None:
    runtime = RuntimeSession()
    _initialize(runtime, tmp_path)
    unsupported = EncodeObservationRequest(
        request_id="unknown",
        tool_name="Fetch",
        tool_input={"url": "https://example.invalid"},
        raw_text="raw fetch result",
        success=True,
        sequence=1,
    )
    failed = EncodeObservationRequest(
        request_id="failed",
        tool_name="Bash",
        tool_input={"command": "false"},
        raw_text="command failed",
        success=False,
        sequence=2,
    )

    first = runtime.handle(unsupported)
    second = runtime.handle(failed)

    assert isinstance(first, EncodeObservationResponse)
    assert isinstance(second, EncodeObservationResponse)
    assert (first.decision, first.reason) == ("pass_through", "unsupported_tool")
    assert (second.decision, second.reason) == ("pass_through", "tool_error")
    metrics = runtime.metrics()
    assert metrics.eligible_observations == 0
    assert metrics.pass_through_by_reason == (("tool_error", 1), ("unsupported_tool", 1))


def test_reopening_preserves_handles_metrics_and_sequence_boundary(tmp_path: Path) -> None:
    first = RuntimeSession()
    _initialize(first, tmp_path)
    encoded = first.handle(_compressible_read())
    assert isinstance(encoded, EncodeObservationResponse)
    assert encoded.reference is not None
    first.handle(ExpandRequest(request_id="expand-1", reference=encoded.reference))
    first.handle(ShutdownRequest(request_id="shutdown-1"))

    reopened = RuntimeSession()
    _initialize(reopened, tmp_path, request_id="init-2")
    metrics = reopened.metrics()
    assert metrics.compressed_observations == 1
    assert metrics.full_expansions == 1

    recovered = reopened.handle(ExpandRequest(request_id="expand-2", reference=encoded.reference))
    assert isinstance(recovered, ExpandResponse)
    assert recovered.content == _compressible_read().raw_text
    with pytest.raises(ProtocolError) as stale:
        reopened.handle(_compressible_read("stale", sequence=1))
    assert stale.value.code is ProtocolErrorCode.INVALID_FRAME


def test_metric_write_failure_returns_typed_non_emission_after_raw_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = RuntimeSession()
    _initialize(runtime, tmp_path)
    request = _compressible_read()

    def fail_metric_write(self: Ledger, **_values: object) -> object:
        raise sqlite3.OperationalError("do not expose this database detail")

    monkeypatch.setattr(Ledger, "record_runtime_decision", fail_metric_write)
    with pytest.raises(ProtocolError) as raised:
        runtime.handle(request)

    assert raised.value.code is ProtocolErrorCode.METRIC_FAILURE
    assert raised.value.request_id == request.request_id
    assert "database detail" not in str(raised.value)
    assert RuntimeStorage(tmp_path / "data").expand("session-1/F1") == request.raw_text


def test_recovery_failures_are_correlated_and_content_free(tmp_path: Path) -> None:
    runtime = RuntimeSession()
    _initialize(runtime, tmp_path)
    secret = "not-a-reference-secret"

    with pytest.raises(ProtocolError) as raised:
        runtime.handle(ExpandRequest(request_id="expand-bad", reference=secret))

    assert raised.value.code is ProtocolErrorCode.INVALID_REFERENCE
    assert raised.value.request_id == "expand-bad"
    assert secret not in str(raised.value)


def test_state_and_storage_failures_are_typed(tmp_path: Path) -> None:
    runtime = RuntimeSession()
    with pytest.raises(ProtocolError) as uninitialized:
        runtime.handle(_compressible_read())
    assert uninitialized.value.code is ProtocolErrorCode.INVALID_STATE

    data_file = tmp_path / "not-a-directory"
    data_file.write_text("occupied")
    with pytest.raises(ProtocolError) as storage:
        runtime.handle(
            InitializeRequest(
                request_id="init-bad",
                session_id="session-1",
                working_directory=str(tmp_path),
                data_directory=str(data_file),
                policy=RuntimePolicy(20, 2, 2, 2),
            )
        )
    assert storage.value.code is ProtocolErrorCode.STORAGE_FAILURE
    assert str(data_file) not in str(storage.value)
