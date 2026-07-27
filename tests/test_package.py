"""Package installation smoke tests."""

from __future__ import annotations

from importlib.metadata import version
from types import ModuleType


def test_package_is_importable(laconic_module: ModuleType) -> None:
    assert laconic_module.__version__ == version("laconic")
