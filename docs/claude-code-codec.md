# Claude Code transforming codec hook

Laconic's codec runs in Claude Code through a `PostToolUse` hook that returns `updatedToolOutput`, the one hook field that *replaces* what the model sees rather than adding to it. This document describes the adapter, how to enable it, and the limits measured on real sessions.

This is separate from [Observe](observe-design.md). Observe records content-free receipts and compresses nothing; its entrypoint must never write to stdout (H-46/H-48). Transformation delivers its replacement *as* stdout, so the two are different programs with contradictory output contracts. `laconic setup` installs both, as separate owners of one settings file.

## Enable it

```bash
laconic setup            # installs the codec hook and Observe's receipts
```

Or install just the codec hook, without Observe's diagnostics:

```bash
laconic install claude-code --dry-run
laconic install claude-code
```

`--scope user` (the default) writes `~/.claude/settings.json`; `--scope project` writes `.claude/settings.json`. Both are idempotent, preserve every foreign hook entry and every other settings key, and never contact a provider.

The codec hook and Observe's receipt hooks are **separate owners** of the same settings file, so they install side by side and are removed independently:

```bash
laconic uninstall claude-code    # removes the codec hook only
```

Verify with `laconic setup --verify-only`, which counts the decisions the hook has recorded.

Recover any replaced output exactly:

```bash
laconic expand '<session>/B3'
```

The reference is printed on the first line of every replaced result, so the model can recover the original itself through `Bash` without operator help.

## What it transforms

| Tool | Replaced field | Notes |
| --- | --- | --- |
| `Bash` | `stdout` | `stderr`, `interrupted`, and every other key pass through untouched |
| `Read` | `file.content` | Text reads only. `filePath`, `startLine`, and `totalLines` describe the file on disk and stay untouched; `numLines` describes what was *returned*, so it is recounted from the replacement — leaving it alone would tell the model it received more lines than it can see |

`Grep` and `Glob` are absent deliberately: Claude Code does not expose them as tools. Across every Claude Code transcript on the development machine they account for two calls, both of which returned *"No such tool available"*. That work arrives through `Bash`.

`PostToolUseFailure` is not hooked. A failed call's output is short and diagnostic, and Claude Code's own documentation warns that stripping error detail can make the model proceed on a false assumption.

## Why the replacement is a copy, never a construction

Claude Code **silently ignores** a replacement that does not match the tool's output shape and uses the original instead. There is no error and no signal back to the hook, so a schema the adapter merely believes in would degrade into an invisible no-op the moment Claude Code adds a field.

The adapter therefore deep-copies the observed `tool_response` and overwrites only the field carrying the shrinkable text — `stdout` for `Bash`, `file.content` for `Read` — plus `file.numLines` when that key is present, because it counts the returned lines rather than describing the file. Nothing else is touched. Unknown keys survive because they are never enumerated. This is not hypothetical: real transcripts carry `noOutputExpected`, `gitOperation`, `persistedOutputPath`, and `persistedOutputSize` on `Bash` results, and none of them appears in the published example.

## Measured on real sessions

13,796 eligible tool results from the development machine's own Claude Code history, replayed through the shipped adapter:

| Tool | Results | Transformed | Rate | Characters removed |
| --- | ---: | ---: | ---: | ---: |
| `Bash` | 12,612 | 675 | 5.4% | 2,356,770 |
| `Read` | 1,184 | 514 | 43.4% | 1,594,448 |
| **Total** | **13,796** | **1,189** | | **3,951,218** |

**Characters at the tool boundary, not tokens and not money.** The character-to-token-to-cost conversion is lossy and workload-dependent; see [`docs/headroom-comparison.md`](headroom-comparison.md) and the K1 result. No savings, equivalence, direction, or superiority claim follows from this table.

`Bash`'s 5.4% is a population property, not a defect. Claude Code's `Bash` results have a median of 442 characters, and the codec correctly declines payloads it cannot shrink. The same analysis for OMP is recorded in the development history under H-129.

## Failure behaviour

The hook is fail-open in every direction. A malformed payload, unavailable ledger, contended same-session lock, or any unexpected error writes nothing to stdout and exits zero, so Claude Code keeps the original tool result. A crash costs compression, never correctness. Diagnostics go to stderr, which an exit-zero hook routes to the client's debug log rather than to the model.

The adapter never constructs a decision of its own. It drives the same `RuntimeSession` the OMP transport drives, so the strictly-smaller rule, the ledger, reference minting, and exact recovery are shared rather than forked.

For each callback, the adapter takes a private, SHA-256-named POSIX sidecar lock for its Claude session before opening the runtime ledger, and releases it only after shutdown and close. This serializes the complete ledger lifecycle for that session while allowing different sessions to proceed independently. Contention has a monotonic 250 ms limit; a callback that cannot acquire its lock makes no ledger mutation and keeps the original tool result.

## Known limits

- **Long single lines are not compressible.** Elision is line-based. A result with few but enormous lines — a 150,000-character diff over 24 lines appears in the measured corpus — passes through untouched.
- **Same-session contention fails open.** Claude Code invokes every `PostToolUse` hook in a fresh process. Laconic serializes same-session callbacks with a bounded lock so they cannot collide on sequence or handle allocation. A callback that waits more than 250 ms keeps its original output without opening or mutating the ledger; this trades a bounded compression opportunity for the existing fail-open guarantee.
- **Acceptance is not observable from inside the hook.** Claude Code reports nothing back when it rejects a replacement. The adapter's defence is that it never builds a shape; confirmation requires inspecting a session transcript.
