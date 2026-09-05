"""Bounded Observe hook subprocess entrypoint.

``python -m laconic.observe.entrypoint --client <claude-code|omp>`` is the
one program a client's hook configuration invokes: Claude Code natively
via its command-hook contract, and a future OMP shim via ``pi.exec``. It
reads exactly one JSON event from stdin, builds and privacy-validates a
receipt, appends it to the local hash-chained audit log, and always
exits 0 with empty stdout on every path -- success, malformed input, an
unsupported event, or a storage failure -- because H-46/H-48
(`.docs/DEVELOPMENT_PLAN_HISTORY.md`) found that any non-zero exit or
stdout content surfaces to Claude or a user, which the design's
no-agent-visible-output invariant forbids unconditionally.

This is **not** a ``laconic diagnostics observe`` CLI subcommand: it is
machine-invoked and never run by a person. M3 owns the user-facing
``install``/``remove``/``status``/``report`` subcommands under
``laconic diagnostics observe``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from laconic.observe.audit import DEFAULT_AUDIT_PATH, append_to_file
from laconic.observe.contracts import ClientId
from laconic.observe.privacy import validate_receipt_json
from laconic.observe.receipt import Receipt, build_claude_code_receipt, build_omp_receipt

#: Overrides the audit file location. Exists only so this repository's
#: own tests can point at a scratch directory instead of the real
#: `.laconic/observe/audit.jsonl`; a real client installer never sets it.
AUDIT_PATH_ENV_VAR = "LACONIC_OBSERVE_AUDIT_PATH"

_ReceiptBuilder = Callable[..., Receipt]

_BUILDERS: dict[ClientId, _ReceiptBuilder] = {
    ClientId.CLAUDE_CODE: build_claude_code_receipt,
    ClientId.OMP: build_omp_receipt,
}


def _audit_path() -> Path:
    override = os.environ.get(AUDIT_PATH_ENV_VAR)
    return Path(override) if override else DEFAULT_AUDIT_PATH


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="laconic-observe-entrypoint", add_help=False)
    parser.add_argument("--client", choices=[member.value for member in ClientId], required=True)
    return parser.parse_args(argv)


def run(client: ClientId, payload: dict[str, Any], *, audit_path: Path) -> Receipt:
    """Build, validate, and persist one receipt.

    Raises on any failure; :func:`main` is the only caller responsible
    for swallowing it and staying silent.
    """
    receipt = _BUILDERS[client](payload, now=time.time())
    receipt_json = receipt.to_json()
    validate_receipt_json(receipt_json)
    append_to_file(audit_path, receipt_json)
    return receipt


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None) -> int:
    """Entry point. Always returns 0.

    Never writes to stdout on any path. Diagnostics, if any, go to
    stderr only: an exit-0 hook's stderr goes to a client's debug log,
    never to the agent or the user, so it is safe to write freely there.
    """
    try:
        args = _parse_args(argv)
        client = ClientId(args.client)
        raw = (stdin or sys.stdin).read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        run(client, payload, audit_path=_audit_path())
    except SystemExit as error:
        print(f"laconic-observe: argument parsing failed: {error}", file=sys.stderr)
    except Exception as error:  # noqa: BLE001 -- fail-open by design; see module docstring
        print(f"laconic-observe: {type(error).__name__}: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
