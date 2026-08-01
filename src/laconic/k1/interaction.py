"""Fail-closed executable-interaction receipts for K1 contemporary replay."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from laconic.k1.eligibility import EligibilityLedger, EligibilityLedgerError, verify_eligibility
from laconic.k1.environment import EnvironmentError, SnapshotEnvironment, SnapshotToolResolver
from laconic.k1.environment_ledger import (
    EnvironmentLedger,
    EnvironmentLedgerError,
    EnvironmentRecord,
    verify_environment,
)
from laconic.k1.epoch import (
    EpochError,
    read_access_audit,
    record_redesign_access,
    verify_epoch_manifest,
)
from laconic.k1.evidence import (
    JsonValue,
    NativeEvidenceError,
    NativeSession,
    ToolCall,
    ToolResult,
    validate_confirmatory_evidence,
)
from laconic.k1.extractors import extract_native
from laconic.k1.manifest import Candidate, Manifest, is_sha256

INTERACTION_RECEIPT_SCHEMA_VERSION = 1

InteractionKind = Literal["user_prompt", "assistant", "tool_call", "tool_result"]
ActionAuthority = Literal["recorded_exact", "rooted_read"]


class InteractionReceiptError(ValueError):
    """Raised when a K1 interaction receipt is incomplete or cannot be verified."""


@dataclass(frozen=True, slots=True)
class ToolInputSchema:
    """A closed canonical schema derived from one native tool input shape."""

    document: dict[str, JsonValue]

    def __post_init__(self) -> None:
        _validate_schema(self.document)

    @property
    def digest(self) -> str:
        """Return the canonical identity of this concrete tool schema."""
        return _digest(self.document)

    def to_document(self) -> dict[str, JsonValue]:
        """Return a deep-independent canonical schema document."""
        return cast(dict[str, JsonValue], json.loads(_canonical_json(self.document)))


@dataclass(frozen=True, slots=True)
class InteractionEvent:
    """A non-content chronological event required to replay one workload."""

    native_index: int
    kind: InteractionKind
    payload_digest: str
    call_digest: str | None = None
    tool_name: str | None = None
    input_schema: ToolInputSchema | None = None
    authority: ActionAuthority | None = None

    def __post_init__(self) -> None:
        if self.native_index < 0:
            raise InteractionReceiptError("interaction event native_index must be non-negative")
        if not is_sha256(self.payload_digest):
            raise InteractionReceiptError(
                "interaction event payload_digest must be 64 lowercase hex"
            )
        if self.kind in {"user_prompt", "assistant"}:
            if any(
                value is not None
                for value in (self.call_digest, self.tool_name, self.input_schema, self.authority)
            ):
                raise InteractionReceiptError(f"{self.kind} event carries tool-only fields")
            return
        if self.kind not in {"tool_call", "tool_result"}:
            raise InteractionReceiptError(f"unknown interaction event kind {self.kind!r}")
        if self.call_digest is None or not is_sha256(self.call_digest):
            raise InteractionReceiptError("tool interaction event requires call_digest")
        if self.kind == "tool_call":
            if self.tool_name is None or not self.tool_name.strip():
                raise InteractionReceiptError("tool_call event requires tool_name")
            if self.input_schema is None:
                raise InteractionReceiptError("tool_call event requires input_schema")
            if self.authority is None:
                raise InteractionReceiptError("tool_call event requires action authority")
            return
        if any(value is not None for value in (self.tool_name, self.input_schema, self.authority)):
            raise InteractionReceiptError("tool_result event carries call-only fields")

    def to_document(self) -> dict[str, JsonValue]:
        """Return canonical non-content event serialization."""
        return {
            "authority": self.authority,
            "call_digest": self.call_digest,
            "input_schema": self.input_schema.to_document() if self.input_schema else None,
            "kind": self.kind,
            "native_index": self.native_index,
            "payload_digest": self.payload_digest,
            "tool_name": self.tool_name,
        }


@dataclass(frozen=True, slots=True)
class InteractionReceipt:
    """A private, non-content receipt for one executable redesign workload."""

    epoch_digest: str
    manifest_digest: str
    eligibility_ledger_digest: str
    environment_ledger_digest: str
    audit_head_digest: str
    candidate_id: str
    source_sha256: str
    environment_digest: str
    environment_mode: Literal["recorded_tool", "snapshot"]
    events: tuple[InteractionEvent, ...]

    def __post_init__(self) -> None:
        for field_name, value in (
            ("epoch_digest", self.epoch_digest),
            ("manifest_digest", self.manifest_digest),
            ("eligibility_ledger_digest", self.eligibility_ledger_digest),
            ("environment_ledger_digest", self.environment_ledger_digest),
            ("audit_head_digest", self.audit_head_digest),
            ("source_sha256", self.source_sha256),
            ("environment_digest", self.environment_digest),
        ):
            if not is_sha256(value):
                raise InteractionReceiptError(f"{field_name} must be 64 lowercase hex")
        if not self.candidate_id.strip():
            raise InteractionReceiptError("candidate_id must not be empty")
        if self.environment_mode not in {"recorded_tool", "snapshot"}:
            raise InteractionReceiptError(
                f"unknown interaction environment_mode {self.environment_mode!r}"
            )
        if not any(event.kind == "user_prompt" for event in self.events):
            raise InteractionReceiptError("interaction receipt requires a user_prompt event")
        expected_call_digests: set[str] = set()
        result_digests: set[str] = set()
        prior_index = -1
        observed_indexes: set[int] = set()
        for event in self.events:
            if event.native_index < prior_index:
                raise InteractionReceiptError("interaction events are not chronologically ordered")
            prior_index = event.native_index
            observed_indexes.add(event.native_index)
            if event.kind == "tool_call":
                if event.call_digest in expected_call_digests:
                    raise InteractionReceiptError("interaction receipt has duplicate tool call")
                expected_call_digests.add(cast(str, event.call_digest))
            elif event.kind == "tool_result":
                call_digest = cast(str, event.call_digest)
                if call_digest not in expected_call_digests:
                    raise InteractionReceiptError(
                        "interaction receipt tool result lacks preceding call"
                    )
                if call_digest in result_digests:
                    raise InteractionReceiptError("interaction receipt has duplicate tool result")
                result_digests.add(call_digest)
        if observed_indexes != set(range(prior_index + 1)):
            raise InteractionReceiptError("interaction receipt has incomplete native chronology")
        if result_digests != expected_call_digests:
            raise InteractionReceiptError("interaction receipt has unlinked tool calls")

    @property
    def digest(self) -> str:
        """Return the canonical authenticated receipt digest."""
        return _digest(self.payload_without_digest())

    def payload_without_digest(self) -> dict[str, object]:
        """Return canonical receipt fields excluding its digest."""
        return {
            "audit_head_digest": self.audit_head_digest,
            "candidate_id": self.candidate_id,
            "eligibility_ledger_digest": self.eligibility_ledger_digest,
            "environment_digest": self.environment_digest,
            "environment_ledger_digest": self.environment_ledger_digest,
            "environment_mode": self.environment_mode,
            "epoch_digest": self.epoch_digest,
            "events": [event.to_document() for event in self.events],
            "manifest_digest": self.manifest_digest,
            "schema_version": INTERACTION_RECEIPT_SCHEMA_VERSION,
            "source_sha256": self.source_sha256,
        }

    def to_document(self) -> dict[str, object]:
        """Return the serialized private receipt."""
        return {"digest": self.digest, **self.payload_without_digest()}


def derive_interaction_receipt(
    session: NativeSession,
    *,
    epoch_digest: str,
    manifest_digest: str,
    eligibility_ledger_digest: str,
    environment_ledger_digest: str,
    audit_head_digest: str,
    environment_digest: str,
    environment_mode: Literal["recorded_tool", "snapshot"],
) -> InteractionReceipt:
    """Derive a chronology receipt without persisting transcript or response bodies."""
    events: list[InteractionEvent] = []
    calls: dict[str, str] = {}
    for native_event in session.events:
        if native_event.kind == "user_prompt":
            events.append(
                InteractionEvent(
                    native_event.index,
                    "user_prompt",
                    _digest({"text": native_event.text}),
                )
            )
            continue
        if native_event.kind == "assistant":
            events.append(
                InteractionEvent(
                    native_event.index,
                    "assistant",
                    _digest({"text": native_event.text}),
                )
            )
            for call in native_event.tool_calls:
                call_reference = _call_reference(call.call_id)
                if call.call_id in calls:
                    raise InteractionReceiptError(
                        "native session has duplicate tool call identifier"
                    )
                calls[call.call_id] = call_reference
                events.append(
                    InteractionEvent(
                        native_event.index,
                        "tool_call",
                        _tool_call_digest(call),
                        call_digest=call_reference,
                        tool_name=call.name,
                        input_schema=ToolInputSchema(schema_for_json(call.input)),
                        authority=_authority_for(environment_mode, call),
                    )
                )
            continue
        result = native_event.tool_result
        if result is None or result.call_id not in calls:
            raise InteractionReceiptError("native session has unlinked tool result")
        events.append(
            InteractionEvent(
                native_event.index,
                "tool_result",
                _tool_result_digest(result),
                call_digest=calls[result.call_id],
            )
        )
    return InteractionReceipt(
        epoch_digest,
        manifest_digest,
        eligibility_ledger_digest,
        environment_ledger_digest,
        audit_head_digest,
        session.candidate_id,
        session.source_sha256,
        environment_digest,
        environment_mode,
        tuple(events),
    )


ActionDisposition = Literal["recorded", "induced", "unsupported"]


@dataclass(frozen=True, slots=True)
class InteractionActionResolution:
    """One authority-bound contemporary tool resolution."""

    disposition: ActionDisposition
    output: JsonValue | None
    reason: str

    def __post_init__(self) -> None:
        if self.disposition not in {"recorded", "induced", "unsupported"}:
            raise InteractionReceiptError(
                f"unknown interaction action disposition {self.disposition!r}"
            )
        if not self.reason.strip():
            raise InteractionReceiptError("interaction action reason must not be empty")
        if self.disposition == "unsupported" and self.output is not None:
            raise InteractionReceiptError("unsupported interaction action has an output")


@dataclass(frozen=True, slots=True)
class _RecordedAction:
    event: InteractionEvent
    input: dict[str, JsonValue]
    output: JsonValue


class InteractionActionResolver:
    def __init__(
        self,
        receipt: InteractionReceipt,
        session: NativeSession,
        *,
        snapshot: SnapshotToolResolver | None = None,
    ) -> None:
        _verify_session_matches_receipt(receipt, session)
        self._receipt = receipt
        self._actions = _recorded_actions(receipt, session)
        self._position = 0
        self._terminated = False
        self._snapshot = snapshot
        if receipt.environment_mode == "snapshot" and snapshot is None:
            raise InteractionReceiptError(
                "snapshot interaction receipt requires a snapshot resolver"
            )
        if receipt.environment_mode == "recorded_tool" and snapshot is not None:
            raise InteractionReceiptError(
                "recorded-tool interaction receipt must not accept a snapshot"
            )

    @property
    def position(self) -> int:
        """Return the number of receipt-authorized actions consumed."""
        return self._position

    @property
    def terminated(self) -> bool:
        """Return whether an unsupported action has stopped this interaction."""
        return self._terminated

    def resolve(self, name: str, tool_input: dict[str, JsonValue]) -> InteractionActionResolution:
        """Resolve one current action or terminate without advancing on a mismatch."""
        if self._terminated:
            return InteractionActionResolution(
                "unsupported", None, "interaction already terminated"
            )
        if self._position == len(self._actions):
            self._terminated = True
            return InteractionActionResolution(
                "unsupported", None, "interaction trace is exhausted"
            )
        expected = self._actions[self._position]
        if not name.strip() or name != expected.event.tool_name:
            self._terminated = True
            return InteractionActionResolution(
                "unsupported", None, "tool name differs from interaction receipt"
            )
        schema = expected.event.input_schema
        if schema is None or not _matches_schema(schema.document, tool_input):
            self._terminated = True
            return InteractionActionResolution(
                "unsupported", None, "tool input does not match receipt schema"
            )
        if self._receipt.environment_mode == "recorded_tool":
            if _canonical_json(tool_input) != _canonical_json(expected.input):
                self._terminated = True
                return InteractionActionResolution(
                    "unsupported", None, "tool call differs from exact recorded action"
                )
            self._position += 1
            return InteractionActionResolution(
                "recorded", _copy_json(expected.output), "exact recorded tool action"
            )
        resolution = _resolve_rooted_read(self._snapshot, name, tool_input)
        if resolution.output is None:
            self._terminated = True
            return InteractionActionResolution("unsupported", None, resolution.reason)
        disposition: ActionDisposition = (
            "recorded"
            if _canonical_json(tool_input) == _canonical_json(expected.input)
            else "induced"
        )
        self._position += 1
        return InteractionActionResolution(disposition, resolution.output, resolution.reason)


@dataclass(frozen=True, slots=True)
class RenderedUserPrompt:
    """One private, in-memory prompt a provider renderer may deliver."""

    native_index: int
    text: str

    def __post_init__(self) -> None:
        if self.native_index < 0:
            raise InteractionReceiptError("rendered prompt native_index must be non-negative")
        if not self.text:
            raise InteractionReceiptError("rendered prompt must not be empty")


class InteractionRenderer:
    """Expose only authenticated user prompts and resolver-authorized tool outcomes."""

    def __init__(
        self,
        receipt: InteractionReceipt,
        session: NativeSession,
        snapshot: SnapshotToolResolver | None = None,
    ) -> None:
        self._resolver = InteractionActionResolver(receipt, session, snapshot=snapshot)
        self._prompts = tuple(
            RenderedUserPrompt(event.index, cast(str, event.text))
            for event in session.events
            if event.kind == "user_prompt"
        )

    @property
    def prompts(self) -> tuple[RenderedUserPrompt, ...]:
        """Return chronological user prompts without exposing assistant text."""
        return self._prompts

    @property
    def terminated(self) -> bool:
        """Return whether an unsupported tool action has terminated rendering."""
        return self._resolver.terminated

    def resolve_tool(
        self, name: str, tool_input: dict[str, JsonValue]
    ) -> InteractionActionResolution:
        """Resolve a tool action under the receipt's exact or rooted authority."""
        return self._resolver.resolve(name, tool_input)


def build_interaction_receipt(
    epoch_path: Path,
    manifest_path: Path,
    eligibility_ledger_path: Path,
    environment_ledger_path: Path,
    candidate_id: str,
    receipt_path: Path,
) -> InteractionReceipt:
    """Build a private receipt only after redesign M2 and M3 revalidation."""
    try:
        epoch, manifest = verify_epoch_manifest(epoch_path, manifest_path)
        eligibility = verify_eligibility(epoch_path, manifest_path, eligibility_ledger_path)
        environment = verify_environment(
            epoch_path, manifest_path, eligibility_ledger_path, environment_ledger_path
        )
    except (EligibilityLedgerError, EnvironmentLedgerError, EpochError) as error:
        raise InteractionReceiptError(str(error)) from error
    candidate = _redesign_candidate(manifest, candidate_id)
    _require_admitted_candidate(candidate_id, candidate.source_sha256, eligibility, environment)
    try:
        record_redesign_access(
            epoch_path,
            manifest_path,
            candidate_id,
            "interaction_build",
            timestamp=_audit_timestamp(),
        )
        session = extract_native(candidate)
        validate_confirmatory_evidence(candidate, session)
        audit = read_access_audit(epoch.audit_path)
    except (EpochError, NativeEvidenceError, OSError) as error:
        raise InteractionReceiptError(
            f"candidate {candidate_id}: cannot derive executable interaction"
        ) from error
    record = _environment_record(environment.records, candidate_id)
    if record.mode is None or record.environment_digest is None:
        raise InteractionReceiptError(
            f"candidate {candidate_id}: environment receipt is incomplete"
        )
    receipt = derive_interaction_receipt(
        session,
        epoch_digest=epoch.digest,
        manifest_digest=manifest.digest,
        eligibility_ledger_digest=eligibility.digest,
        environment_ledger_digest=environment.digest,
        audit_head_digest=audit.head_digest,
        environment_digest=record.environment_digest,
        environment_mode=record.mode,
    )
    write_interaction_receipt(receipt_path, receipt)
    return receipt


def verify_interaction_receipt(
    receipt_path: Path,
    epoch_path: Path,
    manifest_path: Path,
    eligibility_ledger_path: Path,
    environment_ledger_path: Path,
) -> InteractionReceipt:
    """Revalidate every binding before allowing a receipt into M4E."""
    receipt = read_interaction_receipt(receipt_path)
    try:
        epoch, manifest = verify_epoch_manifest(epoch_path, manifest_path)
        eligibility = verify_eligibility(epoch_path, manifest_path, eligibility_ledger_path)
        environment = verify_environment(
            epoch_path, manifest_path, eligibility_ledger_path, environment_ledger_path
        )
    except (EligibilityLedgerError, EnvironmentLedgerError, EpochError) as error:
        raise InteractionReceiptError(str(error)) from error
    candidate = _redesign_candidate(manifest, receipt.candidate_id)
    _require_admitted_candidate(
        receipt.candidate_id, candidate.source_sha256, eligibility, environment
    )
    record = _environment_record(environment.records, receipt.candidate_id)
    if (
        receipt.epoch_digest != epoch.digest
        or receipt.manifest_digest != manifest.digest
        or receipt.eligibility_ledger_digest != eligibility.digest
        or receipt.environment_ledger_digest != environment.digest
        or receipt.environment_digest != record.environment_digest
        or receipt.environment_mode != record.mode
    ):
        raise InteractionReceiptError("interaction receipt admission binding does not match")
    _verify_receipt_audit_head(epoch.digest, epoch.audit_path, receipt)
    try:
        record_redesign_access(
            epoch_path,
            manifest_path,
            receipt.candidate_id,
            "interaction_verify",
            timestamp=_audit_timestamp(),
        )
        session = extract_native(candidate)
        validate_confirmatory_evidence(candidate, session)
    except (EpochError, NativeEvidenceError, OSError) as error:
        raise InteractionReceiptError(
            f"candidate {receipt.candidate_id}: cannot revalidate executable interaction"
        ) from error
    _verify_session_matches_receipt(receipt, session)
    return receipt


def render_private_interaction(
    receipt_path: Path,
    epoch_path: Path,
    manifest_path: Path,
    eligibility_ledger_path: Path,
    environment_ledger_path: Path,
) -> InteractionRenderer:
    """Render one admitted private workload without persisting prompt content."""
    receipt = verify_interaction_receipt(
        receipt_path,
        epoch_path,
        manifest_path,
        eligibility_ledger_path,
        environment_ledger_path,
    )
    try:
        epoch, manifest = verify_epoch_manifest(epoch_path, manifest_path)
        candidate = _redesign_candidate(manifest, receipt.candidate_id)
        record_redesign_access(
            epoch_path,
            manifest_path,
            candidate.candidate_id,
            "interaction_render",
            timestamp=_audit_timestamp(),
        )
        session = extract_native(candidate)
        validate_confirmatory_evidence(candidate, session)
    except (EpochError, NativeEvidenceError, OSError) as error:
        raise InteractionReceiptError(
            f"candidate {receipt.candidate_id}: cannot render executable interaction"
        ) from error
    _verify_session_matches_receipt(receipt, session)
    environment = verify_environment(
        epoch_path, manifest_path, eligibility_ledger_path, environment_ledger_path
    )
    record = _environment_record(environment.records, candidate.candidate_id)
    snapshot: SnapshotToolResolver | None = None
    if record.mode == "snapshot":
        if record.snapshot_root is None or record.environment_digest is None:
            raise InteractionReceiptError(
                f"candidate {candidate.candidate_id}: snapshot receipt is incomplete"
            )
        try:
            snapshot = SnapshotToolResolver(
                SnapshotEnvironment(Path(record.snapshot_root), record.environment_digest)
            )
        except EnvironmentError as error:
            raise InteractionReceiptError(
                f"candidate {candidate.candidate_id}: snapshot resolver is invalid"
            ) from error
    return InteractionRenderer(receipt, session, snapshot)


def schema_for_json(value: JsonValue, depth: int = 0) -> dict[str, JsonValue]:
    """Return a closed JSON Schema document for one native JSON value shape."""
    if depth > 32:
        raise InteractionReceiptError("input schema nesting exceeds maximum depth")
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        return cast(
            dict[str, JsonValue],
            {
                "items": [schema_for_json(item, depth + 1) for item in value],
                "maxItems": len(value),
                "minItems": len(value),
                "type": "array",
            },
        )
    return cast(
        dict[str, JsonValue],
        {
            "additionalProperties": False,
            "properties": {
                key: schema_for_json(item, depth + 1) for key, item in sorted(value.items())
            },
            "required": sorted(value),
            "type": "object",
        },
    )


def write_interaction_receipt(path: Path, receipt: InteractionReceipt) -> None:
    """Atomically write a private interaction receipt as 0600 in a 0700 directory."""
    temporary: Path | None = None
    try:
        _ensure_private_directory(path.parent)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(json.dumps(receipt.to_document(), indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as error:
        raise InteractionReceiptError(
            f"cannot write interaction receipt {path}: {error}"
        ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_interaction_receipt(path: Path) -> InteractionReceipt:
    """Read and authenticate a private interaction receipt."""
    _require_private_path(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise InteractionReceiptError(f"cannot read interaction receipt {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise InteractionReceiptError(
            f"interaction receipt is not valid JSON: {error.msg}"
        ) from error
    if not isinstance(document, dict) or set(document) != {
        "audit_head_digest",
        "candidate_id",
        "digest",
        "eligibility_ledger_digest",
        "environment_digest",
        "environment_ledger_digest",
        "environment_mode",
        "epoch_digest",
        "events",
        "manifest_digest",
        "schema_version",
        "source_sha256",
    }:
        raise InteractionReceiptError("interaction receipt has invalid fields")
    version = document["schema_version"]
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != INTERACTION_RECEIPT_SCHEMA_VERSION
    ):
        raise InteractionReceiptError(f"unsupported interaction receipt schema {version!r}")
    digest = document["digest"]
    if not isinstance(digest, str) or not is_sha256(digest):
        raise InteractionReceiptError("interaction receipt digest must be 64 lowercase hex")
    raw_events = document["events"]
    if not isinstance(raw_events, list):
        raise InteractionReceiptError("interaction receipt events must be an array")
    receipt = InteractionReceipt(
        _required_text(document, "epoch_digest"),
        _required_text(document, "manifest_digest"),
        _required_text(document, "eligibility_ledger_digest"),
        _required_text(document, "environment_ledger_digest"),
        _required_text(document, "audit_head_digest"),
        _required_text(document, "candidate_id"),
        _required_text(document, "source_sha256"),
        _required_text(document, "environment_digest"),
        cast(Literal["recorded_tool", "snapshot"], _required_text(document, "environment_mode")),
        tuple(_event_from_document(index, event) for index, event in enumerate(raw_events)),
    )
    if not hmac.compare_digest(digest, receipt.digest):
        raise InteractionReceiptError("interaction receipt digest mismatch")
    return receipt


def _event_from_document(index: int, payload: object) -> InteractionEvent:
    if not isinstance(payload, dict) or set(payload) != {
        "authority",
        "call_digest",
        "input_schema",
        "kind",
        "native_index",
        "payload_digest",
        "tool_name",
    }:
        raise InteractionReceiptError(f"interaction event {index}: invalid fields")
    native_index = payload["native_index"]
    if not isinstance(native_index, int) or isinstance(native_index, bool):
        raise InteractionReceiptError(f"interaction event {index}: native_index must be an integer")
    raw_schema = payload["input_schema"]
    if raw_schema is not None and not isinstance(raw_schema, dict):
        raise InteractionReceiptError(
            f"interaction event {index}: input_schema must be an object or null"
        )
    schema = ToolInputSchema(raw_schema) if raw_schema is not None else None
    return InteractionEvent(
        native_index,
        cast(InteractionKind, _required_text(payload, "kind")),
        _required_text(payload, "payload_digest"),
        _optional_text(payload, "call_digest"),
        _optional_text(payload, "tool_name"),
        schema,
        cast(ActionAuthority | None, _optional_text(payload, "authority")),
    )


def _redesign_candidate(manifest: Manifest, candidate_id: str) -> Candidate:
    for candidate in manifest.candidates:
        if candidate.candidate_id == candidate_id:
            if candidate.split != "redesign":
                raise InteractionReceiptError(
                    f"candidate {candidate_id!r} is sealed holdout and cannot enter M3E"
                )
            return candidate
    raise InteractionReceiptError(f"candidate {candidate_id!r} is absent from sealed manifest")


def _require_admitted_candidate(
    candidate_id: str,
    source_sha256: str,
    eligibility: EligibilityLedger,
    environment: EnvironmentLedger,
) -> None:
    eligibility_record = next(
        (record for record in eligibility.records if record.candidate_id == candidate_id), None
    )
    if eligibility_record is None or eligibility_record.disposition != "confirmatory":
        raise InteractionReceiptError(
            f"candidate {candidate_id}: eligibility ledger does not admit confirmatory replay"
        )
    if eligibility_record.source_sha256 != source_sha256:
        raise InteractionReceiptError(f"candidate {candidate_id}: eligibility source hash mismatch")
    record = _environment_record(environment.records, candidate_id)
    if (
        record.source_sha256 != source_sha256
        or record.status != "valid"
        or record.mode is None
        or record.environment_digest is None
    ):
        raise InteractionReceiptError(
            f"candidate {candidate_id}: environment ledger does not admit executable replay"
        )


def _environment_record(
    records: tuple[EnvironmentRecord, ...], candidate_id: str
) -> EnvironmentRecord:
    for record in records:
        if record.candidate_id == candidate_id:
            return record
    raise InteractionReceiptError(f"candidate {candidate_id}: environment record is absent")


def _verify_receipt_audit_head(
    epoch_digest: str, audit_path: Path, receipt: InteractionReceipt
) -> None:
    try:
        audit = read_access_audit(audit_path)
    except EpochError as error:
        raise InteractionReceiptError(str(error)) from error
    if audit.epoch_digest != epoch_digest:
        raise InteractionReceiptError("interaction receipt audit belongs to another epoch")
    for record in audit.records:
        if (
            record.digest == receipt.audit_head_digest
            and record.candidate_id == receipt.candidate_id
            and record.operation == "interaction_build"
        ):
            return
    raise InteractionReceiptError("interaction receipt audit head has no build authorization")


def _audit_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _authority_for(
    environment_mode: Literal["recorded_tool", "snapshot"], call: ToolCall
) -> ActionAuthority:
    if environment_mode == "recorded_tool":
        return "recorded_exact"
    if call.name == "Read" and set(call.input) == {"path"} and isinstance(call.input["path"], str):
        return "rooted_read"
    raise InteractionReceiptError("snapshot environment cannot authorize this native tool action")


def _call_reference(call_id: str) -> str:
    return _digest({"call_id": call_id})


def _verify_session_matches_receipt(receipt: InteractionReceipt, session: NativeSession) -> None:
    if (
        session.candidate_id != receipt.candidate_id
        or session.source_sha256 != receipt.source_sha256
    ):
        raise InteractionReceiptError("native session does not match interaction receipt source")
    derived = derive_interaction_receipt(
        session,
        epoch_digest=receipt.epoch_digest,
        manifest_digest=receipt.manifest_digest,
        eligibility_ledger_digest=receipt.eligibility_ledger_digest,
        environment_ledger_digest=receipt.environment_ledger_digest,
        audit_head_digest=receipt.audit_head_digest,
        environment_digest=receipt.environment_digest,
        environment_mode=receipt.environment_mode,
    )
    if not hmac.compare_digest(derived.digest, receipt.digest):
        raise InteractionReceiptError("native interaction chronology does not match receipt")


def _recorded_actions(
    receipt: InteractionReceipt, session: NativeSession
) -> tuple[_RecordedAction, ...]:
    calls: dict[str, ToolCall] = {}
    results: dict[str, ToolResult] = {}
    for event in session.events:
        if event.kind == "assistant":
            calls.update({call.call_id: call for call in event.tool_calls})
        elif event.tool_result is not None:
            results[event.tool_result.call_id] = event.tool_result
    receipt_calls = (event for event in receipt.events if event.kind == "tool_call")
    actions: list[_RecordedAction] = []
    for receipt_event in receipt_calls:
        call_reference = cast(str, receipt_event.call_digest)
        matching = [
            call for call in calls.values() if _call_reference(call.call_id) == call_reference
        ]
        if len(matching) != 1:
            raise InteractionReceiptError(
                "receipt tool call does not resolve to exactly one native action"
            )
        call = matching[0]
        result = results.get(call.call_id)
        if result is None:
            raise InteractionReceiptError("receipt tool call has no native result")
        actions.append(_RecordedAction(receipt_event, call.input, result.output))
    return tuple(actions)


def _resolve_rooted_read(
    resolver: SnapshotToolResolver | None, name: str, tool_input: dict[str, JsonValue]
) -> InteractionActionResolution:
    if resolver is None:
        raise InteractionReceiptError("snapshot interaction resolver is absent")
    resolved = resolver.resolve(name, tool_input)
    if resolved.status == "unsupported":
        return InteractionActionResolution("unsupported", None, resolved.reason)
    return InteractionActionResolution("recorded", resolved.output, resolved.reason)


def _matches_schema(schema: dict[str, JsonValue], value: JsonValue) -> bool:
    schema_type = schema["type"]
    if schema_type == "null":
        return value is None
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, float)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "array":
        items = cast(list[object], schema["items"])
        return (
            isinstance(value, list)
            and len(value) == len(items)
            and all(
                isinstance(item_schema, dict) and _matches_schema(item_schema, item)
                for item_schema, item in zip(items, value, strict=True)
            )
        )
    if schema_type == "object":
        properties = cast(dict[str, object], schema["properties"])
        return (
            isinstance(value, dict)
            and set(value) == set(properties)
            and all(
                isinstance(property_schema, dict) and _matches_schema(property_schema, value[name])
                for name, property_schema in properties.items()
            )
        )
    raise InteractionReceiptError("receipt schema has unsupported type")


def _copy_json(value: JsonValue) -> JsonValue:
    return cast(JsonValue, json.loads(_canonical_json(value)))


def _tool_call_digest(call: ToolCall) -> str:
    return _digest({"input": call.input, "name": call.name})


def _tool_result_digest(result: ToolResult) -> str:
    return _digest({"output": result.output})


def _validate_schema(schema: dict[str, JsonValue], depth: int = 0) -> None:
    if depth > 32:
        raise InteractionReceiptError("input schema nesting exceeds maximum depth")
    schema_type = schema.get("type")
    if schema_type in {"null", "boolean", "integer", "number", "string"}:
        if set(schema) != {"type"}:
            raise InteractionReceiptError("scalar input schema has invalid fields")
        return
    if schema_type == "array":
        if set(schema) != {"type", "items", "minItems", "maxItems"}:
            raise InteractionReceiptError("array input schema has invalid fields")
        items = schema["items"]
        min_items = schema["minItems"]
        max_items = schema["maxItems"]
        if (
            not isinstance(items, list)
            or not isinstance(min_items, int)
            or isinstance(min_items, bool)
            or not isinstance(max_items, int)
            or isinstance(max_items, bool)
            or min_items < 0
            or min_items != max_items
            or min_items != len(items)
        ):
            raise InteractionReceiptError("array input schema must be a closed tuple schema")
        for item in items:
            if not isinstance(item, dict):
                raise InteractionReceiptError("array input schema item must be an object")
            _validate_schema(item, depth + 1)
        return
    if schema_type == "object":
        if set(schema) != {"type", "properties", "required", "additionalProperties"}:
            raise InteractionReceiptError("object input schema has invalid fields")
        properties = schema["properties"]
        required = schema["required"]
        if (
            schema["additionalProperties"] is not False
            or not isinstance(properties, dict)
            or not isinstance(required, list)
            or sorted(properties) != required
            or not all(isinstance(name, str) and name for name in required)
        ):
            raise InteractionReceiptError("object input schema must be closed and complete")
        for child in properties.values():
            if not isinstance(child, dict):
                raise InteractionReceiptError("object input schema property must be an object")
            _validate_schema(child, depth + 1)
        return
    raise InteractionReceiptError("input schema has an unsupported or generic type")


def _required_text(payload: dict[str, object], field_name: str) -> str:
    value = payload[field_name]
    if not isinstance(value, str) or not value.strip():
        raise InteractionReceiptError(f"{field_name} must be a non-empty string")
    return value


def _optional_text(payload: dict[str, object], field_name: str) -> str | None:
    value = payload[field_name]
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InteractionReceiptError(f"{field_name} must be a non-empty string or null")
    return value


def _canonical_json(value: JsonValue) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise InteractionReceiptError("interaction value is not canonical JSON") from error


def _digest(payload: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise InteractionReceiptError("interaction value is not canonical JSON") from error
    return hashlib.sha256(encoded).hexdigest()


def _require_private_path(path: Path) -> None:
    try:
        entry_stat = path.lstat()
    except OSError as error:
        raise InteractionReceiptError(f"cannot stat interaction receipt {path}: {error}") from error
    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISREG(entry_stat.st_mode):
        raise InteractionReceiptError("interaction receipt must be a non-symlink regular file")
    if entry_stat.st_uid != os.getuid():
        raise InteractionReceiptError("interaction receipt must be owned by the current user")
    if entry_stat.st_mode & 0o777 != 0o600:
        raise InteractionReceiptError("interaction receipt must have mode 0600")
    _validate_private_directory(path.parent)


def _ensure_private_directory(path: Path) -> None:
    missing: list[Path] = []
    ancestor = path
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
    _validate_private_directory(path)


def _validate_private_directory(path: Path) -> None:
    try:
        entry_stat = path.lstat()
    except OSError as error:
        raise InteractionReceiptError(
            f"cannot stat interaction receipt directory {path}: {error}"
        ) from error
    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
        raise InteractionReceiptError(
            "interaction receipt directory must be a non-symlink directory"
        )
    if entry_stat.st_uid != os.getuid():
        raise InteractionReceiptError(
            "interaction receipt directory must be owned by the current user"
        )
    if entry_stat.st_mode & 0o777 != 0o700:
        raise InteractionReceiptError("interaction receipt directory must have mode 0700")
