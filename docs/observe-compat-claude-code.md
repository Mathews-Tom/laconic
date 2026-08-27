# Claude Code Observe Compatibility Report (M1)

**Status:** compatibility spike finding; no hook installed, no real event read.
**Source:** `https://code.claude.com/docs/en/hooks`, fetched 2026-08-27.
**Verdict:** **GO**

## Verified event contract

| Requirement | Event | Notes |
| --- | --- | --- |
| Completed-tool-result (success) | `PostToolUse` | Fires after a tool call succeeds. Input includes `tool_name`, `tool_input`, `tool_response`, `duration_ms`, `session_id`. |
| Completed-tool-result (failure) | `PostToolUseFailure` | Fires after a tool call fails. Input includes `tool_name`, `tool_input`, `error`, `is_interrupt`, `duration_ms`. |
| Session close | `SessionEnd` | Fires once per session termination. Input includes `reason` (`clear`, `resume`, `logout`, `prompt_input_exit`, `other`). |

`PreToolUse` fires before execution and cannot report a completed result's size --
confirming the design's stated reason for excluding it as a substitute.

## Output and failure contract

- A command hook prints JSON on stdout and communicates via exit code. Exit 0
  with empty stdout is fully silent: no transcript notice, nothing shown to
  Claude or the user.
- Any non-zero exit surfaces stderr: to Claude for `PostToolUse` /
  `PostToolUseFailure`, or to the user for `SessionEnd`. **Observe's hook
  subprocess must exit 0 unconditionally, on every failure path**, to satisfy
  the design's no-agent-visible-output invariant.
- Stderr from an exit-0 hook goes to the debug log only -- never the
  transcript, never to Claude -- so local diagnostics can be written freely
  without violating the privacy boundary.

## Timeout contract

- `PostToolUse` and `PostToolUseFailure` command hooks default to a 600s
  timeout, configurable per hook entry.
- `SessionEnd` hooks share a **1.5s default budget across every configured
  `SessionEnd` hook** on the session. A hook entry can raise its own share by
  setting a longer `timeout` field, up to a 60s combined ceiling. A future
  M2/M3 implementation must budget the Observe `SessionEnd` write for this
  tight default and declare an explicit `timeout` in the installed entry
  rather than relying on the 600s command default.

## Configuration ownership

| Scope | Path | Shareable | Notes |
| --- | --- | --- | --- |
| User | `~/.claude/settings.json` | No | Applies to every project on this machine. |
| Project | `.claude/settings.json` | Yes | Committable; the intended target for a shared install. |

`.claude/settings.local.json` is Claude-Code-managed and gitignored when
Claude Code itself saves a setting there. Observe must not target it as an
owned-entry write surface: a project install belongs in `.claude/settings.json`.

Hook entries are added as JSON objects inside an event's array under a
top-level `hooks` key -- an **entry merge**, not a file drop. An installer
must detect and mutate only its own owned entries (see
`src/laconic/observe/contracts.py::InstallMechanism.JSON_ENTRY_MERGE`) and
leave every other entry, and every other top-level settings key, untouched.

## GO/NO-GO

**GO.** Claude Code exposes a verified completed-tool-result event pair
(`PostToolUse` + `PostToolUseFailure`) and a verified session-close event
(`SessionEnd`), a documented silent-on-success output contract, and a JSON
settings location suitable for a committable, owned, mergeable hook entry.
