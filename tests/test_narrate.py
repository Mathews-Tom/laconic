"""Tests for optional narration-provider configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from laconic.ledger import Ledger, ObservationKind
from laconic.render.narrate import (
    NarrationConfig,
    NarrationConfigurationError,
    NoneProvider,
    provider_for,
)
from laconic.render.view import assemble


def test_none_provider_returns_no_narration(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db", "narrate") as ledger:
        ledger.register(ObservationKind.FILE, "a.py", "alpha", "a.py", turn=0)
        entries = assemble(ledger, 1, 1)

    assert NoneProvider().narrate(entries) is None


def test_default_configuration_selects_none_provider() -> None:
    assert isinstance(provider_for(NarrationConfig()), NoneProvider)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"provider": "ollama"}, "requires an endpoint"),
        (
            {"provider": "ollama", "endpoint": "http://127.0.0.1"},
            "requires a model",
        ),
        ({"provider": "none", "model": "local"}, "does not accept"),
        ({"timeout_seconds": 0}, "must be positive"),
    ],
)
def test_configuration_rejects_incomplete_or_invalid_values(
    kwargs: dict[str, str | float], message: str
) -> None:
    with pytest.raises(NarrationConfigurationError, match=message):
        NarrationConfig(**kwargs)  # type: ignore[arg-type]


def test_unimplemented_provider_fails_loudly() -> None:
    config = NarrationConfig(
        provider="ollama",
        endpoint="http://127.0.0.1",
        model="local",
    )

    with pytest.raises(NarrationConfigurationError, match="configured but unavailable"):
        provider_for(config)
