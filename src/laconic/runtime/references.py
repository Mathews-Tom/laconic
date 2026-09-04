"""Strict, globally routable references over session-scoped ledger handles."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SESSION_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_HANDLE = r"[FBSWX][1-9][0-9]{0,8}"
_REFERENCE = re.compile(
    rf"(?P<session>{_SESSION_ID})/(?P<handle>{_HANDLE})"
    rf"(?::(?P<first>[1-9][0-9]{{0,8}})-(?P<last>[1-9][0-9]{{0,8}}))?"
)
_SESSION = re.compile(_SESSION_ID)


class InvalidSessionIdError(ValueError):
    """Raised when a session identifier cannot be represented safely."""


class InvalidRuntimeReferenceError(ValueError):
    """Raised when a model-visible runtime reference is malformed."""


def validate_session_id(session_id: str) -> str:
    """Return a valid reference namespace or reject it without coercion."""
    if _SESSION.fullmatch(session_id) is None:
        raise InvalidSessionIdError(f"invalid session id: {session_id!r}")
    return session_id


@dataclass(frozen=True, slots=True)
class RuntimeReference:
    """A source session plus one internal ledger handle and optional span."""

    session_id: str
    handle: str
    first_line: int | None = None
    last_line: int | None = None

    def __post_init__(self) -> None:
        validate_session_id(self.session_id)
        if re.fullmatch(_HANDLE, self.handle) is None:
            raise InvalidRuntimeReferenceError(f"invalid runtime handle: {self.handle!r}")
        if (self.first_line is None) != (self.last_line is None):
            raise InvalidRuntimeReferenceError("runtime reference span must have both bounds")
        if self.first_line is not None and (
            self.first_line < 1
            or self.last_line is None
            or self.last_line < self.first_line
            or self.last_line > 999_999_999
        ):
            raise InvalidRuntimeReferenceError(
                f"invalid runtime reference span: {self.first_line}-{self.last_line}"
            )

    @classmethod
    def parse(cls, value: str) -> RuntimeReference:
        """Parse one complete runtime reference, rejecting partial matches."""
        match = _REFERENCE.fullmatch(value)
        if match is None:
            raise InvalidRuntimeReferenceError(f"invalid runtime reference: {value!r}")
        first_text = match.group("first")
        last_text = match.group("last")
        first = None if first_text is None else int(first_text)
        last = None if last_text is None else int(last_text)
        if first is not None and last is not None and last < first:
            raise InvalidRuntimeReferenceError(f"invalid runtime reference: {value!r}")
        return cls(
            session_id=match.group("session"),
            handle=match.group("handle"),
            first_line=first,
            last_line=last,
        )

    @classmethod
    def from_ledger_reference(cls, session_id: str, reference: str) -> RuntimeReference:
        """Attach a validated source-session namespace to an internal reference."""
        return cls.parse(f"{validate_session_id(session_id)}/{reference}")

    @property
    def ledger_reference(self) -> str:
        """Return the handle/span syntax understood by :class:`Ledger`."""
        if self.first_line is None:
            return self.handle
        return f"{self.handle}:{self.first_line}-{self.last_line}"

    def __str__(self) -> str:
        return f"{self.session_id}/{self.ledger_reference}"
