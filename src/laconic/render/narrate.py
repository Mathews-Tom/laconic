"""Optional, read-only narration providers for human-facing trace views."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from http.client import HTTPException
from typing import Literal, Protocol
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from laconic.render.view import TraceEntry

type ProviderName = Literal["none", "ollama"]


class NarrationConfigurationError(ValueError):
    """Raised when an optional narration provider lacks required settings."""


class NarrationUnavailableError(RuntimeError):
    """Raised when a configured narration service cannot be reached."""


class NarrationResponseError(ValueError):
    """Raised when a reachable narration service returns an invalid response."""


@dataclass(frozen=True, slots=True)
class Narration:
    """Generated connective prose and the structural facts supplied to it."""

    text: str
    source_handles: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise NarrationResponseError("narration response is empty")
        if not self.source_handles:
            raise NarrationResponseError("narration response has no source handles")


class NarrationProvider(Protocol):
    """Produce connective prose from structural trace entries only."""

    def narrate(self, entries: Sequence[TraceEntry]) -> Narration | None:
        """Return optional prose without mutating its input or external state."""


@dataclass(frozen=True, slots=True)
class NarrationConfig:
    """Explicit configuration for an optional local narration provider."""

    provider: ProviderName = "none"
    endpoint: str | None = None
    model: str | None = None
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise NarrationConfigurationError(
                f"timeout_seconds must be positive: {self.timeout_seconds}"
            )
        if self.provider == "none":
            if self.endpoint is not None or self.model is not None:
                raise NarrationConfigurationError(
                    "provider 'none' does not accept an endpoint or model"
                )
            return
        if self.provider != "ollama":
            raise NarrationConfigurationError(f"unsupported narration provider: {self.provider!r}")
        if not self.endpoint:
            raise NarrationConfigurationError("provider 'ollama' requires an endpoint")
        if not self.model:
            raise NarrationConfigurationError("provider 'ollama' requires a model")
        try:
            endpoint = urlsplit(self.endpoint)
        except ValueError as error:
            raise NarrationConfigurationError(
                "provider 'ollama' requires an absolute HTTP(S) endpoint"
            ) from error
        if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
            raise NarrationConfigurationError(
                "provider 'ollama' requires an absolute HTTP(S) endpoint"
            )


@dataclass(frozen=True, slots=True)
class NoneProvider:
    """The default provider, which always leaves the trace deterministic."""

    def narrate(self, entries: Sequence[TraceEntry]) -> None:
        """Return no generated prose."""
        return None


@dataclass(frozen=True, slots=True)
class OllamaProvider:
    """Call a configured local Ollama endpoint for connective prose."""

    config: NarrationConfig

    def narrate(self, entries: Sequence[TraceEntry]) -> Narration | None:
        """Generate prose from structural metadata, never raw ledger content."""
        if not entries:
            return None
        payload = {
            "model": self.config.model,
            "prompt": _prompt(entries),
            "stream": False,
        }
        try:
            request = Request(
                self.config.endpoint or "",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                body = response.read()
        except HTTPError as error:
            if 400 <= error.code < 500:
                raise NarrationResponseError(
                    f"narration provider rejected request with HTTP {error.code}"
                ) from error
            raise NarrationUnavailableError(
                f"narration provider {self.config.provider!r} is unavailable "
                f"({type(error).__name__})"
            ) from error
        except (HTTPException, OSError, ValueError) as error:
            raise NarrationUnavailableError(
                f"narration provider {self.config.provider!r} is unavailable "
                f"({type(error).__name__})"
            ) from error
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as error:
            raise NarrationResponseError("narration provider returned invalid JSON") from error
        if not isinstance(decoded, dict):
            raise NarrationResponseError("narration provider returned a non-object response")
        text = decoded.get("response")
        if not isinstance(text, str):
            raise NarrationResponseError("narration provider response has no text")
        stripped = text.strip()
        if not stripped:
            return None
        return Narration(
            text=stripped,
            source_handles=tuple(entry.record.handle for entry in entries),
        )


def _prompt(entries: Sequence[TraceEntry]) -> str:
    """Create a request using renderer-visible fields only."""
    facts = "\n".join(
        f"- Turn {entry.turn}: {entry.record.kind.value} {entry.record.subject!r} "
        f"[{entry.record.handle}] ({entry.record.raw_chars} chars)"
        for entry in entries
    )
    return (
        "Write at most two sentences of connective prose between these structural facts. "
        "Do not restate facts, infer hidden causes, give advice, or claim outcomes. "
        "Return an empty response when no useful connective prose exists.\n\n"
        f"Structural facts:\n{facts}"
    )


def provider_for(config: NarrationConfig) -> NarrationProvider:
    """Construct the configured narration provider."""
    if config.provider == "none":
        return NoneProvider()
    return OllamaProvider(config)
