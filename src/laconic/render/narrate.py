"""Optional, read-only narration providers for human-facing trace views."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from laconic.render.view import TraceEntry

type ProviderName = Literal["none", "ollama"]


class NarrationConfigurationError(ValueError):
    """Raised when an optional narration provider lacks required settings."""


class NarrationProvider(Protocol):
    """Produce connective prose from structural trace entries only."""

    def narrate(self, entries: Sequence[TraceEntry]) -> str | None:
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
        if not self.endpoint:
            raise NarrationConfigurationError("provider 'ollama' requires an endpoint")
        if not self.model:
            raise NarrationConfigurationError("provider 'ollama' requires a model")


@dataclass(frozen=True, slots=True)
class NoneProvider:
    """The default provider, which always leaves the trace deterministic."""

    def narrate(self, entries: Sequence[TraceEntry]) -> None:
        """Return no generated prose."""
        return None


def provider_for(config: NarrationConfig) -> NarrationProvider:
    """Construct the configured provider once its implementation is available."""
    if config.provider == "none":
        return NoneProvider()
    raise NarrationConfigurationError(f"provider {config.provider!r} is configured but unavailable")
