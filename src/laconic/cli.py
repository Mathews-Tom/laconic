"""Command-line entrypoint for Laconic."""

from __future__ import annotations

import argparse

from laconic import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the currently available CLI surface."""
    parser = argparse.ArgumentParser(description="A context-loop codec for coding agents.")
    parser.add_argument("--version", action="version", version=f"laconic {__version__}")
    return parser


def main() -> None:
    """Run the Laconic CLI."""
    build_parser().parse_args()
