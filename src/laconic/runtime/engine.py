"""Session-bound orchestration for recoverable observation compression."""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from laconic.codec.observe import (
    COMMAND_TOOLS,
    FILE_TOOLS,
    SEARCH_TOOLS,
    ObservationCodec,
    subject_for,
)
from laconic.ledger import (
    DuplicateRuntimeExpansionError,
    InvalidSpanError,
    Ledger,
    RuntimeDecision,
    RuntimeDecisionOutcome,
    UnknownHandleError,
)
from laconic.runtime.protocol import (
    PROTOCOL_VERSION,
    EncodeObservationRequest,
    EncodeObservationResponse,
    ExpandRequest,
    ExpandResponse,
    InitializeRequest,
    InitializeResponse,
    ProtocolError,
    ProtocolErrorCode,
    RuntimeRequest,
    RuntimeResponse,
    ShutdownRequest,
    ShutdownResponse,
)
from laconic.runtime.references import (
    InvalidRuntimeReferenceError,
    InvalidSessionIdError,
    RuntimeReference,
)
from laconic.runtime.storage import (
    RuntimeStorage,
    SessionLedgerNotFoundError,
    UnsafeStoragePathError,
)

RUNTIME_TOOLS = FILE_TOOLS | COMMAND_TOOLS | SEARCH_TOOLS


@dataclass(frozen=True, slots=True)
class SessionMetrics:
    """Content-free aggregate decisions for one runtime session."""

    eligible_observations: int
    compressed_observations: int
    pass_through_by_reason: tuple[tuple[str, int], ...]
    raw_chars: int
    visible_chars: int
    characters_avoided: int
    full_expansions: int
    span_expansions: int
    encoding_latencies_ms: tuple[float, ...]


class RuntimeSession:
    """Serve one initialized runtime session through transport-neutral requests."""

    def __init__(self) -> None:
        self._storage: RuntimeStorage | None = None
        self._ledger: Ledger | None = None
        self._codec: ObservationCodec | None = None
        self._session_id: str | None = None
        self._working_directory: Path | None = None
        self._last_sequence = -1
        self._shutdown_metrics: SessionMetrics | None = None
        self._closed = False

    @property
    def session_id(self) -> str:
        """Return the bound session identifier after initialization."""
        if self._session_id is None:
            raise ProtocolError(ProtocolErrorCode.INVALID_STATE, "runtime is not initialized")
        return self._session_id

    @property
    def working_directory(self) -> Path:
        """Return the canonical working directory bound at initialization."""
        if self._working_directory is None:
            raise ProtocolError(ProtocolErrorCode.INVALID_STATE, "runtime is not initialized")
        return self._working_directory

    def handle(self, request: RuntimeRequest) -> RuntimeResponse:
        """Apply one typed request or raise a correlated content-free error."""
        try:
            if request.protocol_version != PROTOCOL_VERSION:
                raise ProtocolError(
                    ProtocolErrorCode.UNSUPPORTED_VERSION,
                    "unsupported protocol version",
                )
            if isinstance(request, InitializeRequest):
                return self._initialize(request)
            self._require_active()
            if isinstance(request, EncodeObservationRequest):
                return self._encode(request)
            if isinstance(request, ExpandRequest):
                return self._expand(request)
            if isinstance(request, ShutdownRequest):
                return self._shutdown(request)
            raise ProtocolError(ProtocolErrorCode.INVALID_FRAME, "unsupported request")
        except ProtocolError as error:
            if error.request_id is not None:
                raise
            raise ProtocolError(
                error.code,
                str(error),
                request_id=request.request_id,
                operation=request.operation.value,
            ) from error
        except InvalidSessionIdError as error:
            raise self._failure(
                request, ProtocolErrorCode.INVALID_FRAME, "invalid session id"
            ) from error
        except InvalidRuntimeReferenceError as error:
            raise self._failure(
                request, ProtocolErrorCode.INVALID_REFERENCE, "invalid runtime reference"
            ) from error
        except SessionLedgerNotFoundError as error:
            raise self._failure(
                request, ProtocolErrorCode.UNKNOWN_SESSION, "runtime session does not exist"
            ) from error
        except UnknownHandleError as error:
            raise self._failure(
                request, ProtocolErrorCode.UNKNOWN_HANDLE, "runtime handle does not exist"
            ) from error
        except InvalidSpanError as error:
            raise self._failure(
                request, ProtocolErrorCode.INVALID_SPAN, "invalid runtime span"
            ) from error
        except (sqlite3.Error, OSError, UnsafeStoragePathError) as error:
            raise self._failure(
                request, ProtocolErrorCode.STORAGE_FAILURE, "runtime storage operation failed"
            ) from error
        except Exception as error:
            raise self._failure(
                request, ProtocolErrorCode.OPERATION_FAILED, "runtime operation failed"
            ) from error

    def metrics(self) -> SessionMetrics:
        """Return persisted session aggregates, including reopen-safe expansion counts."""
        if self._shutdown_metrics is not None:
            return self._shutdown_metrics
        ledger = self._active_ledger()
        return _session_metrics(ledger.runtime_decisions(), ledger.runtime_expansion_counts())

    def close(self) -> None:
        """Close the bound ledger without inventing a protocol response."""
        if self._ledger is not None:
            self._ledger.close()
            self._ledger = None
        self._codec = None
        self._closed = True

    def _initialize(self, request: InitializeRequest) -> InitializeResponse:
        if self._closed or self._ledger is not None:
            raise ProtocolError(ProtocolErrorCode.INVALID_STATE, "runtime cannot initialize twice")
        try:
            working_directory = Path(request.working_directory).expanduser().resolve(strict=True)
        except OSError as error:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_FRAME,
                "working_directory must name an existing directory",
            ) from error
        if not working_directory.is_dir():
            raise ProtocolError(
                ProtocolErrorCode.INVALID_FRAME,
                "working_directory must name an existing directory",
            )

        storage = RuntimeStorage(Path(request.data_directory))
        ledger = storage.open_ledger(request.session_id)
        try:
            codec = ObservationCodec(
                ledger,
                span_budget=request.policy.span_budget,
                keep_head=request.policy.keep_head,
                keep_tail=request.policy.keep_tail,
                max_errors=request.policy.max_errors,
            )
            decisions = ledger.runtime_decisions()
        except BaseException:
            ledger.close()
            raise

        self._storage = storage
        self._ledger = ledger
        self._codec = codec
        self._session_id = request.session_id
        self._working_directory = working_directory
        self._last_sequence = decisions[-1].sequence if decisions else -1
        return InitializeResponse(request_id=request.request_id, session_id=request.session_id)

    def _encode(self, request: EncodeObservationRequest) -> EncodeObservationResponse:
        if request.sequence <= self._last_sequence:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_FRAME,
                "observation sequence must increase monotonically",
            )
        started = time.perf_counter()
        if request.tool_name not in RUNTIME_TOOLS:
            return self._pass_through(request, "unsupported_tool", started)
        if not request.success:
            return self._pass_through(request, "tool_error", started)

        ledger = self._active_ledger()
        codec = self._active_codec()
        try:
            record = codec.encode(
                request.tool_name,
                subject_for(request.tool_input),
                request.raw_text,
                request.tool_input,
                turn=request.sequence,
            )
        except (sqlite3.Error, OSError, UnsafeStoragePathError) as error:
            raise ProtocolError(
                ProtocolErrorCode.STORAGE_FAILURE,
                "runtime storage operation failed",
            ) from error
        except Exception as error:
            raise ProtocolError(
                ProtocolErrorCode.ENCODING_FAILURE,
                "observation encoding failed",
            ) from error

        reference = str(RuntimeReference.from_ledger_reference(self.session_id, record.handle))
        envelope = _envelope(reference, record.encoded)
        recovered = ledger.expand(record.handle)
        if recovered != request.raw_text:
            return self._pass_through(
                request,
                "recovery_mismatch",
                started,
                candidate_reference=reference,
            )
        if len(envelope) >= len(request.raw_text):
            return self._pass_through(
                request,
                "not_smaller",
                started,
                candidate_reference=reference,
            )

        latency_ms = _elapsed_ms(started)
        self._record_decision(
            request=request,
            outcome="emitted",
            reason="smaller_envelope",
            candidate_reference=reference,
            visible_chars=len(envelope),
            latency_ms=latency_ms,
        )
        return EncodeObservationResponse(
            request_id=request.request_id,
            decision="emitted",
            reason="smaller_envelope",
            content=envelope,
            reference=reference,
            raw_chars=len(request.raw_text),
            visible_chars=len(envelope),
            latency_ms=latency_ms,
        )

    def _pass_through(
        self,
        request: EncodeObservationRequest,
        reason: str,
        started: float,
        *,
        candidate_reference: str | None = None,
    ) -> EncodeObservationResponse:
        latency_ms = _elapsed_ms(started)
        self._record_decision(
            request=request,
            outcome="pass_through",
            reason=reason,
            candidate_reference=candidate_reference,
            visible_chars=len(request.raw_text),
            latency_ms=latency_ms,
        )
        return EncodeObservationResponse(
            request_id=request.request_id,
            decision="pass_through",
            reason=reason,
            content=None,
            reference=None,
            raw_chars=len(request.raw_text),
            visible_chars=len(request.raw_text),
            latency_ms=latency_ms,
        )

    def _record_decision(
        self,
        *,
        request: EncodeObservationRequest,
        outcome: RuntimeDecisionOutcome,
        reason: str,
        candidate_reference: str | None,
        visible_chars: int,
        latency_ms: float,
    ) -> None:
        ledger = self._active_ledger()
        try:
            ledger.record_runtime_decision(
                sequence=request.sequence,
                request_id=request.request_id,
                tool_name=request.tool_name,
                outcome=outcome,
                reason=reason,
                candidate_reference=candidate_reference,
                raw_chars=len(request.raw_text),
                visible_chars=visible_chars,
                latency_ms=latency_ms,
            )
        except Exception as error:
            raise ProtocolError(
                ProtocolErrorCode.METRIC_FAILURE,
                "runtime decision could not be recorded",
            ) from error
        self._last_sequence = request.sequence

    def _expand(self, request: ExpandRequest) -> ExpandResponse:
        reference = RuntimeReference.parse(request.reference)
        storage = self._active_storage()
        content = storage.expand(str(reference))
        metric_recorded = True
        try:
            self._active_ledger().record_runtime_expansion(
                request_id=request.request_id,
                reference=str(reference),
                span=reference.first_line is not None,
            )
        except (sqlite3.Error, OSError, ValueError, DuplicateRuntimeExpansionError):
            metric_recorded = False
        return ExpandResponse(
            request_id=request.request_id,
            reference=str(reference),
            content=content,
            metric_recorded=metric_recorded,
        )

    def _shutdown(self, request: ShutdownRequest) -> ShutdownResponse:
        session_id = self.session_id
        self._shutdown_metrics = self.metrics()
        self.close()
        return ShutdownResponse(request_id=request.request_id, session_id=session_id)

    def _require_active(self) -> None:
        if self._closed:
            raise ProtocolError(ProtocolErrorCode.INVALID_STATE, "runtime is shut down")
        if self._ledger is None:
            raise ProtocolError(ProtocolErrorCode.INVALID_STATE, "runtime is not initialized")

    def _active_storage(self) -> RuntimeStorage:
        self._require_active()
        assert self._storage is not None
        return self._storage

    def _active_ledger(self) -> Ledger:
        self._require_active()
        assert self._ledger is not None
        return self._ledger

    def _active_codec(self) -> ObservationCodec:
        self._require_active()
        assert self._codec is not None
        return self._codec

    @staticmethod
    def _failure(
        request: RuntimeRequest,
        code: ProtocolErrorCode,
        message: str,
    ) -> ProtocolError:
        return ProtocolError(
            code,
            message,
            request_id=request.request_id,
            operation=request.operation.value,
        )


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1_000


def _envelope(reference: str, encoded: str) -> str:
    argument = json.dumps({"reference": reference}, ensure_ascii=True, separators=(",", ":"))
    return f"[laconic {reference} | full: laconic_expand({argument})]\n{encoded}"


def _session_metrics(
    decisions: tuple[RuntimeDecision, ...],
    expansions: tuple[int, int],
) -> SessionMetrics:
    reasons = Counter(
        decision.reason for decision in decisions if decision.outcome == "pass_through"
    )
    raw_chars = sum(decision.raw_chars for decision in decisions)
    visible_chars = sum(decision.visible_chars for decision in decisions)
    return SessionMetrics(
        eligible_observations=sum(
            decision.candidate_reference is not None for decision in decisions
        ),
        compressed_observations=sum(decision.outcome == "emitted" for decision in decisions),
        pass_through_by_reason=tuple(sorted(reasons.items())),
        raw_chars=raw_chars,
        visible_chars=visible_chars,
        characters_avoided=raw_chars - visible_chars,
        full_expansions=expansions[0],
        span_expansions=expansions[1],
        encoding_latencies_ms=tuple(decision.latency_ms for decision in decisions),
    )
