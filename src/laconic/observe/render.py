"""M3 PR-1: immutable installer-plan render contracts.

Every function here is pure: given an existing configuration document (or
none) it returns a brand-new document with Observe's owned entries added
or removed, and never touches a filesystem path itself. Real file
reading, atomic writing, and default project/user path resolution are
`laconic.observe.installer`'s job (M3 PR-2) -- this module only decides
*what the new content should be*.

Idempotence is structural, not incidental: rendering install against an
already-owned document returns a document identical in the owned
positions (verified by `laconic.observe.preview`'s ownership detection),
and rendering remove against a document with no owned entries returns it
unchanged.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from laconic.observe.contracts import ClientId
from laconic.observe.preview import (
    CLAUDE_CODE_EVENTS,
    CLAUDE_CODE_OWNED_MARKER,
    OMP_OWNED_MARKER,
    claude_code_event_is_owned,
)

#: Bounded per-hook timeout, in seconds, for the installed `PostToolUse`
#: and `PostToolUseFailure` entries. Well under Claude Code's 600s
#: command default; generous for one local file append.
DEFAULT_TOOL_EVENT_TIMEOUT_SECONDS = 10.0

#: Bounded per-hook timeout, in seconds, for the installed `SessionEnd`
#: entry. `SessionEnd` hooks share a 1.5s *default* combined budget
#: across every configured `SessionEnd` hook, raised to match the
#: highest declared per-hook `timeout` up to a 60s ceiling (H-46); this
#: value both bounds Observe's own hook and raises that shared budget
#: enough for it to reliably complete.
DEFAULT_SESSION_END_TIMEOUT_SECONDS = 5.0

#: Module invoked by the installed Claude Code hook entry and by the
#: generated OMP shim, always by absolute interpreter path plus this
#: fixed argv tail.
ENTRYPOINT_MODULE_ARGS = ("-m", "laconic.observe.entrypoint")


def _entrypoint_args(client: ClientId) -> list[str]:
    return [*ENTRYPOINT_MODULE_ARGS, "--client", client.value]


def _owned_hook_entry(*, python: str, timeout: float) -> dict[str, Any]:
    """One Observe-owned Claude Code hook-matcher-group entry.

    The ownership marker lives in `statusMessage`, a purely cosmetic
    spinner-text field, never in `command`/`args`: the entrypoint's own
    `argparse` parser accepts no positional arguments, so any extra
    token appended there would break argument parsing instead of merely
    decorating it (H-50).
    """
    return {
        "matcher": "*",
        "hooks": [
            {
                "type": "command",
                "command": python,
                "args": _entrypoint_args(ClientId.CLAUDE_CODE),
                "timeout": timeout,
                "statusMessage": CLAUDE_CODE_OWNED_MARKER,
            }
        ],
    }


def render_claude_code_settings_installed(
    existing: dict[str, Any],
    *,
    python: str,
    tool_event_timeout: float = DEFAULT_TOOL_EVENT_TIMEOUT_SECONDS,
    session_end_timeout: float = DEFAULT_SESSION_END_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Return a new settings document with one Observe-owned hook entry
    added to each of `CLAUDE_CODE_EVENTS`.

    Idempotent: an event that already carries an owned entry is left
    exactly as it was, so re-rendering an already-installed document is a
    no-op. Every other key, and every other handler within an owned
    event's groups, is preserved unchanged.
    """
    rendered = copy.deepcopy(existing)
    hooks = rendered.setdefault("hooks", {})
    for event in CLAUDE_CODE_EVENTS:
        if claude_code_event_is_owned(rendered, event):
            continue
        timeout = session_end_timeout if event == "SessionEnd" else tool_event_timeout
        groups = hooks.setdefault(event, [])
        groups.append(_owned_hook_entry(python=python, timeout=timeout))
    return rendered


def render_claude_code_settings_removed(existing: dict[str, Any]) -> dict[str, Any]:
    """Return a new settings document with every Observe-owned hook entry
    removed.

    An event group left with no handlers after removal is dropped
    entirely rather than kept as an empty list; every foreign handler,
    and every other top-level key, is preserved unchanged.
    """
    rendered = copy.deepcopy(existing)
    hooks = rendered.get("hooks")
    if not isinstance(hooks, dict):
        return rendered

    for event in CLAUDE_CODE_EVENTS:
        groups = hooks.get(event)
        if not groups:
            continue
        surviving_groups = []
        for group in groups:
            surviving_handlers = [
                handler
                for handler in group.get("hooks", [])
                if CLAUDE_CODE_OWNED_MARKER not in str(handler.get("command", ""))
                and CLAUDE_CODE_OWNED_MARKER not in str(handler.get("statusMessage", ""))
            ]
            if surviving_handlers:
                surviving_groups.append({**group, "hooks": surviving_handlers})
        if surviving_groups:
            hooks[event] = surviving_groups
        else:
            hooks.pop(event, None)

    if not hooks:
        rendered.pop("hooks", None)
    return rendered


#: The OMP shim's timeout race, in milliseconds. Bounds a `pi.exec` call
#: the runner does not itself bound (H-46).
DEFAULT_OMP_TIMEOUT_MS = 5_000


def render_omp_extension_source(*, python: str, timeout_ms: int = DEFAULT_OMP_TIMEOUT_MS) -> str:
    """Return the full source of Observe's owned OMP extension module.

    Registers `tool_result` and `session_shutdown` handlers that forward
    a normalized, content-free JSON envelope to the same bounded Python
    entrypoint Claude Code's installed hook invokes, via `pi.exec` under
    an explicit timeout race. The session identifier is a
    `crypto.randomUUID()` generated once when this factory runs, not
    derived from any session-file API -- M3's design gate did not
    confirm one is available inside this hook context (H-50).
    """
    tool_result_args = json.dumps(_entrypoint_args(ClientId.OMP))
    session_close_args = json.dumps(_entrypoint_args(ClientId.OMP))
    return f'''{OMP_OWNED_MARKER}
// Generated by "laconic observe install --client omp". Do not edit by
// hand -- "laconic observe remove --client omp" deletes this exact file.
//
// Observe-only: this module never returns a value from any handler, so
// it never overrides a tool result, injects context, or blocks a call.
// It forwards a content-free JSON envelope to a local, bounded Python
// subprocess and otherwise has no effect on the agent.

import type {{ HookAPI }} from "@oh-my-pi/pi-coding-agent/extensibility/hooks";

const OBSERVE_TIMEOUT_MS = {timeout_ms};
const sessionId = crypto.randomUUID();

async function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T | undefined> {{
  return Promise.race([
    promise,
    new Promise<undefined>((resolve) => setTimeout(() => resolve(undefined), ms)),
  ]);
}}

export default function laconicObserve(pi: HookAPI): void {{
  pi.on("tool_result", async (event) => {{
    const envelope = JSON.stringify({{
      session_id: sessionId,
      event: "tool_result",
      toolName: event.toolName,
      isError: event.isError,
    }});
    await withTimeout(
      pi.exec("{python}", {tool_result_args}, {{ input: envelope }}),
      OBSERVE_TIMEOUT_MS,
    );
  }});

  pi.on("session_shutdown", async () => {{
    const envelope = JSON.stringify({{ session_id: sessionId, event: "session_shutdown" }});
    await withTimeout(
      pi.exec("{python}", {session_close_args}, {{ input: envelope }}),
      OBSERVE_TIMEOUT_MS,
    );
  }});
}}
'''
