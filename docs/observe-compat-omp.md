# OMP Observe Compatibility Report (M1)

**Status:** compatibility spike finding; no hook installed, no real event read.
**Source:** `omp://hooks.md`, `omp://skills/authoring-hooks.md`,
`omp://extension-loading.md`, fetched 2026-08-27.
**Verdict:** **GO**

## Structural difference from Claude Code

OMP's hook subsystem is fundamentally different from Claude Code's: hooks are
**in-process JS/TS extension modules** (`export default function(pi: HookAPI)`)
that register handlers via `pi.on(event, handler)`, not subprocesses invoked
per event with JSON on stdin. No shared runtime adapter is possible between
the two clients; each stays independently versioned per
`docs/observe-design.md` § Client adapters.

## Verified event contract

| Requirement | Event | Notes |
| --- | --- | --- |
| Completed tool call | `tool_result` | Fires after tool execution, success or failure. Carries `toolName`, `toolCallId`, `input`, `content`, `details`, `isError`. |
| Session close | `session_shutdown` | Fires on session shutdown. |

`tool_call` (pre-execution) cannot report a completed result, matching the
same exclusion reason as Claude Code's `PreToolUse`.

## Output and failure contract

- Handlers run in-process; there is no stdout/exit-code contract to satisfy.
  A handler's *return value* is read directly by `HookRunner` -- for
  `tool_result`, a returned `{ content, details, isError }` overrides what the
  LLM sees. An Observe handler that returns nothing (or `undefined`) changes
  nothing observable.
- `ExtensionRunner` catches handler exceptions after load and emits them as
  extension errors instead of crashing the runner loop -- fail-open at the
  runner level.
- This fail-open behavior does **not** bound a handler's own child process. A
  handler that shells out (e.g. via `pi.exec`) to a bounded Python subprocess
  must enforce its own hard timeout; nothing in the runner does this for it.

## Timeout contract

No hook-specific timeout exists in the runner; `default_timeout_seconds` and
`max_timeout_seconds` are recorded as `0.0` in
`src/laconic/observe/omp.py::OMP_CONTRACT` to make this explicit rather than
implying a real budget exists. A future M2 adapter shim must implement its
own timeout race around any subprocess call.

## Configuration ownership

| Scope | Path | Shareable | Notes |
| --- | --- | --- | --- |
| Project | `<cwd>/.omp/extensions/laconic-observe.ts` | Yes | `cwd`-only; does not walk ancestor directories. |
| User | active agent directory's `extensions/laconic-observe.ts` | No | Default `~/.omp/agent/extensions`; profile- and `PI_CODING_AGENT_DIR`-aware, so the resolved path is not a fixed constant. |

Install is a **file drop**, not a JSON-array merge: a single `.ts` module is
written to (or removed from) the extensions directory. Ownership cannot be
decided by filename alone -- a user's own file could collide with the chosen
name -- so an installer must verify an embedded content marker before
touching or claiming a file (see
`src/laconic/observe/preview.py::OMP_OWNED_MARKER`).

## GO/NO-GO

**GO.** OMP exposes a verified completed-tool-result event (`tool_result`)
and a verified session-close event (`session_shutdown`), and a file-drop
install location suitable for an owned, content-marked extension module. The
adapter's runtime shape (in-process module forwarding to a shared bounded
subprocess) is structurally distinct from Claude Code's and must be
implemented, versioned, and installed independently.
