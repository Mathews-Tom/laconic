"""Shared pytest fixtures."""

from __future__ import annotations

from types import ModuleType

import pytest


@pytest.fixture
def laconic_module() -> ModuleType:
    """Import the installed package under test."""
    import laconic

    return laconic
