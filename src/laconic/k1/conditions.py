"""Arm-specific private observation conditions for K1 paired replay."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from laconic.codec.observe import ObservationCodec, subject_for
from laconic.k1.environment import RecordedToolObservation, RecordedToolResolver, ToolResolution
from laconic.k1.evidence import JsonValue, NativeEvidenceError, NativeSession, ToolCall
from laconic.k1.manifest import is_sha256
from laconic.ledger import Ledger

Arm = str


class ObservationConditionError(ValueError):
    """Raised when a native workload cannot form a private arm condition."""


@dataclass(frozen=True, slots=True)
class ConditionObservation:
    """One exact recorded tool action with the arm-specific returned observation."""

    name: str
    tool_input: dict[str, JsonValue]
    raw_output: JsonValue
    rendered_output: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ObservationConditionError("condition tool name must not be empty")

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "raw_output": self.raw_output,
            "rendered_output": self.rendered_output,
            "tool_input": self.tool_input,
        }


@dataclass(frozen=True, slots=True)
class ObservationCondition:
    """A complete raw or codec tool-observation condition for one workload."""

    arm: Arm
    candidate_id: str
    source_sha256: str
    user_prompts: tuple[str, ...]
    observations: tuple[ConditionObservation, ...]

    def __post_init__(self) -> None:
        if self.arm not in {"raw", "codec"}:
            raise ObservationConditionError(f"unknown condition arm {self.arm!r}")
        if not self.candidate_id.strip():
            raise ObservationConditionError("condition candidate_id must not be empty")
        if not is_sha256(self.source_sha256):
            raise ObservationConditionError("condition source_sha256 must be 64 lowercase hex")
        if not self.user_prompts or any(not prompt.strip() for prompt in self.user_prompts):
            raise ObservationConditionError("condition requires non-empty user prompts")
        if not self.observations:
            raise ObservationConditionError("condition requires at least one tool observation")

    @property
    def digest(self) -> str:
        return _digest(self.to_payload())

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "arm": self.arm,
            "candidate_id": self.candidate_id,
            "observations": [observation.to_payload() for observation in self.observations],
            "source_sha256": self.source_sha256,
            "user_prompts": list(self.user_prompts),
        }

    def resolver(self) -> ConditionResolver:
        """Return a fresh exact-call resolver for one contemporary arm replay."""
        return ConditionResolver(self)


class ConditionResolver:
    """Map exact contemporary calls to the rendered output for one arm."""

    def __init__(self, condition: ObservationCondition) -> None:
        self._condition = condition
        self._resolver = RecordedToolResolver(
            tuple(
                RecordedToolObservation(
                    observation.name, observation.tool_input, observation.raw_output
                )
                for observation in condition.observations
            )
        )

    @property
    def condition_digest(self) -> str:
        """Return the immutable condition identity for audit provenance."""
        return self._condition.digest

    @property
    def terminated(self) -> bool:
        """Return whether a divergent contemporary call terminated this arm."""
        return self._resolver.terminated

    @property
    def position(self) -> int:
        """Return the count of exact recorded tool calls resolved so far."""
        return self._resolver.position

    def resolve(self, name: str, tool_input: dict[str, JsonValue]) -> ToolResolution:
        """Resolve only the next exact call and return its arm-specific rendering."""
        position = self._resolver.position
        resolution = self._resolver.resolve(name, tool_input)
        if resolution.status == "unsupported":
            return resolution
        try:
            rendered = self._condition.observations[position].rendered_output
        except IndexError as error:
            raise ObservationConditionError(
                "condition resolver advanced beyond observations"
            ) from error
        return ToolResolution("resolved", rendered, resolution.reason)


def build_observation_condition(
    session: NativeSession,
    arm: Arm,
    *,
    ledger_path: Path,
    ledger_session_id: str,
) -> ObservationCondition:
    """Build one private raw or codec condition from canonical native evidence.

    The ledger is private caller-owned storage. It preserves the codec's recovery
    contract without allowing response or condition content into a receipt/report.
    """
    if arm not in {"raw", "codec"}:
        raise ObservationConditionError(f"unknown condition arm {arm!r}")
    if not session.source_path.is_absolute() or not is_sha256(session.source_sha256):
        raise ObservationConditionError("condition session lacks valid source provenance")
    _create_private_file(ledger_path)
    calls = _calls_by_id(session)
    observations: list[ConditionObservation] = []
    try:
        with Ledger(ledger_path, ledger_session_id) as ledger:
            codec = ObservationCodec(ledger)
            for event in session.events:
                if event.kind != "tool_result" or event.tool_result is None:
                    continue
                call = calls.get(event.tool_result.call_id)
                if call is None:
                    raise ObservationConditionError(
                        f"tool result {event.tool_result.call_id!r} has no recorded call"
                    )
                raw = _raw_text(event.tool_result.output)
                rendered = raw
                if arm == "codec":
                    rendered = codec.encode(
                        call.name,
                        subject_for(call.input),
                        raw,
                        call.input,
                        turn=len(observations),
                    ).encoded
                observations.append(
                    ConditionObservation(call.name, call.input, event.tool_result.output, rendered)
                )
    except (NativeEvidenceError, OSError, ValueError) as error:
        raise ObservationConditionError(
            f"cannot materialize {arm} observation condition: {error}"
        ) from error
    _require_private_file(ledger_path)
    user_prompts = tuple(
        event.text
        for event in session.events
        if event.kind == "user_prompt" and event.text is not None
    )
    return ObservationCondition(
        arm, session.candidate_id, session.source_sha256, user_prompts, tuple(observations)
    )


def _calls_by_id(session: NativeSession) -> dict[str, ToolCall]:
    calls: dict[str, ToolCall] = {}
    for event in session.events:
        if event.kind != "assistant":
            continue
        for call in event.tool_calls:
            if call.call_id in calls:
                raise ObservationConditionError(f"duplicate recorded tool call {call.call_id!r}")
            calls[call.call_id] = call
    return calls


def _raw_text(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise ObservationConditionError("recorded tool output is not canonical JSON") from error


def _create_private_file(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_private_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise ObservationConditionError(f"condition ledger already exists: {path}") from error
    os.close(descriptor)


def _require_private_file(path: Path) -> None:
    try:
        entry_stat = path.lstat()
    except OSError as error:
        raise ObservationConditionError(f"cannot stat condition ledger {path}: {error}") from error
    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISREG(entry_stat.st_mode):
        raise ObservationConditionError("condition ledger must be a non-symlink regular file")
    if entry_stat.st_uid != os.getuid():
        raise ObservationConditionError("condition ledger must be owned by the current user")
    mode = stat.S_IMODE(entry_stat.st_mode)
    if mode != 0o600:
        raise ObservationConditionError(f"condition ledger must have mode 0600, found {mode:04o}")


def _require_private_directory(path: Path) -> None:
    try:
        entry_stat = path.lstat()
    except OSError as error:
        raise ObservationConditionError(
            f"cannot stat condition directory {path}: {error}"
        ) from error
    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
        raise ObservationConditionError("condition directory must be a non-symlink directory")
    if entry_stat.st_uid != os.getuid():
        raise ObservationConditionError("condition directory must be owned by the current user")
    mode = stat.S_IMODE(entry_stat.st_mode)
    if mode != 0o700:
        raise ObservationConditionError(
            f"condition directory must have mode 0700, found {mode:04o}"
        )


def _digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
