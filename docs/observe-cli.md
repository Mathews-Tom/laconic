# Laconic Observe — Operator Guide

`laconic observe` is a measurement-only CLI surface (`docs/observe-design.md`).
It installs a local, content-free receipt hook, never a codec transform: no
`laconic observe` command enables the codec, changes K1 status, injects
context into an agent, exposes an MCP tool, or contacts a provider.

## Commands

```text
laconic observe install --client claude-code|omp [--scope project|user]
                         [--dry-run] [--user-dir PATH] [--python PATH]
                         [--format text|json]
laconic observe remove  --client claude-code|omp [--scope project|user]
                         [--dry-run] [--user-dir PATH] [--format text|json]
laconic observe status  [--audit-path PATH] [--format text|json]
laconic observe report  [--audit-path PATH] [--format text|json]
```

Every `install`/`remove` call is idempotent: running it twice in a row
performs exactly one real write (or zero, on the second call). `--dry-run`
computes and prints the exact change without writing anything.

## What gets written

### Claude Code (`--client claude-code`)

One hook entry is merged into a JSON settings file for each of
`PostToolUse`, `PostToolUseFailure`, and `SessionEnd`:

- Project scope (default): `.claude/settings.json`, relative to the current
  directory. Committable.
- User scope (`--scope user`): `~/.claude/settings.json`.

Every other key, and every other hook entry, in that file is left exactly as
it was. The installed entry is content-free: it runs
`python -m laconic.observe.entrypoint --client claude-code` (using the exact
interpreter active at install time, or `--python` if given) and marks
ownership in the entry's `statusMessage` field, a purely cosmetic
spinner-text field with no effect on execution.

### OMP (`--client omp`)

One extension file, `laconic-observe.ts`, is written:

- Project scope (default): `.omp/extensions/laconic-observe.ts`, relative to
  the current directory.
- User scope (`--scope user`): the active agent directory's `extensions/`.

**Known limitation:** OMP's own user-scope directory is profile- and
`PI_CODING_AGENT_DIR`-aware in ways this installer cannot fully replicate
without invoking `omp` itself. `install --scope user` checks
`PI_CODING_AGENT_DIR` first, then falls back to `~/.omp/agent/extensions`,
and never guesses a `--profile` name. If you run `omp --profile <name>`,
pass `--user-dir` explicitly:

```text
laconic observe install --client omp --scope user \
  --user-dir ~/.omp/profiles/<name>/agent/extensions
```

If a real, unrelated file already occupies the owned path
(`laconic-observe.ts`) without Observe's content marker, `install` refuses to
overwrite it and exits with a dedicated error rather than destroying it.

## Removing

`laconic observe remove` reverses exactly what `install` would add, and
nothing else:

- Claude Code: strips only Observe-owned hook entries from the settings
  file; the file itself is never deleted, even if the result is empty --
  it may predate Observe and belong to the operator.
- OMP: deletes the owned `laconic-observe.ts` file outright (a file-drop
  install owns its whole file, unlike Claude Code's per-entry JSON merge).

## Status and reports

`laconic observe status` and `laconic observe report` read only the local
hash-chained audit log (default: `.laconic/observe/audit.jsonl`, relative to
the current directory; override with `--audit-path`). Neither command
contacts a provider or reads a real client configuration.

`status` answers "is anything here, and does it still verify":

```text
$ laconic observe status
laconic observe status: .laconic/observe/audit.jsonl
  exists: True
  entries: 42
  chain valid: True
```

`report` breaks every receipt down by its allowlisted fields only --
adapter, tool category, result class, and size bands. No receipt field is
ever content-bearing (`docs/observe-design.md` § Receipt and privacy
boundary), so a report never contains a file path, command, or tool result.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 20 | an existing configuration file could not be parsed as JSON/an object |
| 21 | a real, unrelated file occupies Observe's owned OMP install path |

## What this CLI does not do

- It does not enable hooks or MCP as a codec-transformation surface.
- It does not clear the K1 8.53% deployment block.
- It does not read a real session, contact a provider, or collect a
  representative corpus.
- `status`/`report` reflect only local receipts already written by an
  installed hook actually firing -- they are not a substitute for the
  representative K1 study (`.docs/K1_REPRESENTATIVE_CORPUS_PROTOCOL.md`).
