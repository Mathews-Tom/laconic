"""K1 Stage C -- deny-overlay generator: the tool-approval config overlay
that lets a replayed live-model session propose an action without ever
executing it for real.

Governed by `.docs/K1_STAGE_C_LIVE_REPLAY_DESIGN.md` SS10.2/SS14 and
`.docs/K1_STAGE_C_DEVELOPMENT_PLAN.md` M1. H-64/H-65/H-66
(`.docs/DEVELOPMENT_PLAN_HISTORY.md`) empirically confirmed the mechanism:
a `tools.approval.<tool>: deny` config overlay blocks a proposed tool call
from actually executing while the `toolcall_end` event still surfaces the
model's full proposed `{id, name, arguments}` before the denial. H-68
records this module's own design gate, including two findings that
revise the design doc's SS10.2 worked example against the real oh-my-pi
source (checked out locally at `/Users/druk/WorkSpace/AetherForge/oh-my-pi`
for this gate):

- OMP's own approval-tier declaration is *not* a safe proxy for "has no
  real side effect". `memory_edit` and `retain`
  (`packages/coding-agent/src/tools/memory-edit.ts:18`,
  `.../memory-retain.ts:19`) are each declared a static, unconditional
  `readonly approval = "read" as const` -- OMP's tier governs local
  filesystem/exec risk, not whether a tool mutates external state (here,
  the memory vault). Both stay in this module's deny set despite their
  `"read"` tier; `recall`/`reflect` (`.../memory-recall.ts:16`,
  `.../memory-reflect.ts:17`), which are pure queries with no vault
  mutation, do not.
- `lsp`'s own approval declaration (`packages/coding-agent/src/lsp/tool.ts:154`)
  is per-argument: `LSP_READONLY_ACTIONS.has(action) ? "read" : "write"`,
  and the non-read-only branch is a bare tier with no `policy` override.
  A single tool-level `tools.approval.lsp` entry cannot selectively gate
  its actions, so `lsp` is denied wholesale here rather than left enabled
  for its read-only actions (design doc SS10.2's "read-only lsp actions
  stay enabled" is not realized in this generator) -- a small capability
  loss traded for a hole-free guarantee.

Consequently this generator does **not** implement "deny every write/exec
-tier tool" (a naive per-tool tier lookup); it is an *allowlist-complement*:
:data:`SAFE_READONLY_TOOLS` is a small, explicitly reviewed set of tools
verified to (a) always resolve `"read"` tier for every reachable argument
shape and (b) have no real-world side effect when they run, and every
other name in :data:`BUILTIN_TOOL_NAMES` is denied -- including any tool
this module has never seen before. `tools.approvalMode` defaults to
`"yolo"` (auto-approve every tier; `docs/settings.md` "Tool settings"
table, oh-my-pi source), so an unclassified tool would otherwise execute
for real by default. The overlay additionally forces
`tools.approvalMode: always-ask` so that a future, not-yet-classified
tool at write/exec tier stalls on an approval prompt no RPC-mode listener
ever answers (fails safe -- a hang, never a silent real mutation) rather
than silently auto-executing under the library default.

The canonical built-in tool name list in :data:`BUILTIN_TOOL_NAMES` is
mirrored, not independently hand-typed, from oh-my-pi's own registry:
`packages/coding-agent/src/tools/builtin-names.ts` `BUILTIN_TOOL_NAMES`
(order and membership, checked out locally at H-68's design gate).
Hidden tools (`HIDDEN_TOOL_NAMES` there: `yield`, `goal`, `think`) are out
of scope -- they are "excluded unless explicitly listed in --tools or
agent's tools field" (`packages/agent/src/types.ts` `ToolLoadMode`/
`hidden` docs), and Stage C's RPC invocation (design doc SS10.2) never
opts into them.

oh-my-pi is a fast-moving fork the `oh-my-pi-tool-approval-architecture`
skill warns "drifts by 1000+ commits". :func:`denied_tools`'s
allowlist-complement computation is this module's fail-closed guarantee
-- a tool added to `BUILTIN_TOOL_NAMES` without a corresponding entry in
`SAFE_READONLY_TOOLS` is denied automatically, never silently unguarded
-- but the name list itself still needs periodic re-verification against
a current oh-my-pi checkout before any real-spend milestone (M3); a unit
test running without that checkout cannot close this residual risk, only
this module's own internal completeness.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

#: Every built-in OMP tool name exposed to the model by default. Mirrors
#: oh-my-pi's `packages/coding-agent/src/tools/builtin-names.ts`
#: `BUILTIN_TOOL_NAMES` verbatim (order and membership), checked at H-68.
BUILTIN_TOOL_NAMES: tuple[str, ...] = (
    "read",
    "bash",
    "edit",
    "ast_grep",
    "ast_edit",
    "ask",
    "debug",
    "eval",
    "github",
    "glob",
    "grep",
    "lsp",
    "inspect_image",
    "browser",
    "computer",
    "checkpoint",
    "rewind",
    "security_scan",
    "task",
    "hub",
    "todo",
    "web_search",
    "write",
    "memory_edit",
    "retain",
    "recall",
    "reflect",
    "learn",
    "manage_skill",
)

#: The only tools this module leaves reachable during a Stage C replay --
#: verified, by direct inspection of each tool's own OMP source file (see
#: module docstring), to always resolve `"read"` tier and to have no
#: real-world side effect when executed. Every other name in
#: :data:`BUILTIN_TOOL_NAMES` is denied by :func:`denied_tools`, including
#: tools statically declared `"read"` tier by OMP that still mutate real
#: state (`memory_edit`, `retain`) and tools whose write-capable branch
#: cannot be gated at the tool-name level (`lsp`).
SAFE_READONLY_TOOLS: frozenset[str] = frozenset({"read", "grep", "glob", "recall", "reflect"})

#: Default local, gitignored output location, mirroring
#: `laconic.k1corpus.stage_b.DEFAULT_SESSION_MANIFEST_PATH`'s
#: `.laconic/k1/<stage>/` convention.
DEFAULT_DENY_OVERLAY_PATH = Path(".laconic/k1/stage_c/deny-overlay.yml")


class UnclassifiedToolError(ValueError):
    """Raised when ``safe`` names a tool absent from ``tool_names`` --
    a configuration error in the caller's inputs, not a real registry
    drift (a real registry drift instead widens ``tool_names`` beyond
    ``safe``, which :func:`denied_tools` handles by denying the new
    name, not by raising)."""


def denied_tools(
    tool_names: Sequence[str] = BUILTIN_TOOL_NAMES, safe: frozenset[str] = SAFE_READONLY_TOOLS
) -> tuple[str, ...]:
    """Return every name in ``tool_names`` not in ``safe``, sorted --
    the deny-overlay's tool list.

    This is the allowlist-complement computation: the deny set is
    ``tool_names - safe``, so a name absent from ``safe`` is denied
    automatically, including one this module has never seen before (the
    "a newly-added tool cannot land unguarded" property the design gate
    requires). Raises :class:`UnclassifiedToolError` if ``safe`` names a
    tool that is not even present in ``tool_names`` -- a caller error,
    since an allowlist entry for a nonexistent tool cannot be verified
    safe.
    """
    names = set(tool_names)
    unknown_safe = safe - names
    if unknown_safe:
        raise UnclassifiedToolError(f"safe names not present in tool_names: {sorted(unknown_safe)}")
    return tuple(sorted(names - safe))


def render_deny_overlay_yaml(
    tool_names: Sequence[str] = BUILTIN_TOOL_NAMES, safe: frozenset[str] = SAFE_READONLY_TOOLS
) -> str:
    """Render the `config.yml`-style overlay `omp --config <file>` loads
    (`docs/cli-reference.md`).

    Emits `tools.approvalMode: always-ask` (SS defense-in-depth note in
    the module docstring) and one bare `tools.approval.<tool>: deny`
    entry per :func:`denied_tools` result -- the literal syntax H-64/H-65/
    H-66's real spikes used and empirically confirmed blocks execution
    while still surfacing the proposed `tool_use` action
    (`docs/settings.md` "SS Tool approval mode": `tools.approval` is "a
    record keyed by tool name" whose entries are the bare
    `allow`/`deny`/`prompt` policy, not the object form).
    """
    denied = denied_tools(tool_names, safe)
    lines = ["tools:", "  approvalMode: always-ask", "  approval:"]
    lines.extend(f"    {tool}: deny" for tool in denied)
    return "\n".join(lines) + "\n"


def write_deny_overlay(
    path: Path = DEFAULT_DENY_OVERLAY_PATH,
    *,
    tool_names: Sequence[str] = BUILTIN_TOOL_NAMES,
    safe: frozenset[str] = SAFE_READONLY_TOOLS,
) -> None:
    """Atomically write the deny overlay to ``path`` under a
    mode-restricted directory (`0o700`) and file (`0o600`), mirroring
    `laconic.k1corpus.stage_b.write_session_manifest`'s writer."""
    content = render_deny_overlay_yaml(tool_names, safe)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".deny_overlay_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
