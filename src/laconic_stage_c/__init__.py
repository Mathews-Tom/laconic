"""Concrete, locally importable OMP adapter for K1 Stage C.

The sibling package deliberately depends on :mod:`laconic`, but Laconic never
imports this package. Operators pass ``laconic_stage_c:factory`` to the Stage C
CLI when a separately authorized live replay is ready to run.
"""

from __future__ import annotations

import os
from pathlib import Path

from laconic.k1corpus.deny_overlay import write_deny_overlay
from laconic.k1corpus.stage_c import DEFAULT_STAGE_C_ROOT

from .rpc_client import OmpRpcReplayClient

STAGE_C_ROOT = DEFAULT_STAGE_C_ROOT


def _make_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _prepare_state_root(root: Path) -> tuple[Path, Path, Path]:
    _make_private_directory(root)
    deny_overlay_path = root / "deny-overlay.yml"
    write_deny_overlay(deny_overlay_path)

    sandbox = root / "sandbox"
    _make_private_directory(sandbox)
    if any(sandbox.iterdir()):
        raise RuntimeError(f"Stage C sandbox must be empty before replay: {sandbox}")

    session_dir = root / "omp-sessions"
    _make_private_directory(session_dir)
    return deny_overlay_path, sandbox, session_dir


def factory() -> OmpRpcReplayClient:
    """Create the zero-argument Stage C ``ReplayClient`` factory product.

    The default authenticated OMP profile supplies OAuth credentials. All
    replay-local files stay below the mode-restricted Stage C root.
    """
    deny_overlay_path, sandbox, session_dir = _prepare_state_root(STAGE_C_ROOT.resolve())
    return OmpRpcReplayClient(
        deny_overlay_path=deny_overlay_path,
        cwd=sandbox,
        session_dir=session_dir,
    )


def preflight(*, model: str) -> None:
    """Start the factory client, wait for RPC ``ready``, then close it.

    This writes no RPC ``prompt`` command, so it cannot send a provider prompt
    or read a session body.
    """
    client = factory()
    try:
        client.preflight(model)
    finally:
        client.close()


__all__ = ["OmpRpcReplayClient", "factory", "preflight"]
