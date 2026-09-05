# Laconic OMP Runtime — Operator Guide

The OMP runtime is Laconic's first product integration. It is opt-in and local: one native OMP extension intercepts eligible tool results, delegates encoding and recovery to one session-owned Python process, and returns a compact envelope only when that complete envelope is strictly smaller than the original text.

The runtime candidate is present on `main` for M18 qualification and is not part of the published `v0.8.0` package. Install from a source checkout until a later release passes the runtime beta gate.

## Install

From the project where OMP should load Laconic:

```text
uv tool install .
laconic install omp --dry-run
laconic install omp
```

Project scope is the default. It writes one owned file:

```text
.omp/extensions/zz-laconic-runtime.ts
```

The generated extension records the absolute environment interpreter, the isolated `python -I -m laconic.runtime` entrypoint, and the private runtime data directory. Preserving the environment interpreter is required because resolving a virtual-environment or `uv tool` symlink to its base Python loses the installed Laconic package. Isolated mode prevents a project-local `laconic` module or `PYTHONPATH` from shadowing the installed runtime. Installation is atomic and idempotent. It refuses foreign files and symlinked path components and will not coexist with another Laconic OMP adapter in the selected extension directory.

For a user-scoped install:

```text
laconic install omp --scope user --dry-run
laconic install omp --scope user
```

Use `--profile NAME` for an OMP profile or `--user-dir PATH` for an explicit native OMP extension directory. Both require `--scope user`. Use `--python ABSOLUTE_PATH` or `--data-dir PATH` only when the generated extension must pin non-default locations.

`install` never contacts a model provider and never creates a runtime ledger.

## What changes in OMP

Each OMP session owns one bounded Python engine process. Branch and tree navigation remain in the same runtime session. Switching to another OMP session stops the old process and rebinds the runtime to the new session identifier.

Only these completed tool results are eligible:

- `read`;
- `bash`;
- `grep`;
- `glob`.

A result can change only when it is successful and contains exactly one text block. Tool errors, mixed content, non-text content, unsupported tools, storage failures, malformed protocol responses, engine crashes, and requests exceeding 250 ms pass through unchanged. Laconic opens its circuit breaker after three consecutive engine failures; later results continue through native OMP behavior.

Every replacement contains a source-session namespace and a recovery handle, for example:

```text
<omp-session-id>/F1
<omp-session-id>/F1:20-40
```

The engine commits the exact raw observation before returning an emitted envelope. If the full recovery-bearing envelope is not strictly smaller than the original text, the original result remains unchanged.

## Recover content

Inside OMP, the model can call the read-only essential tool `laconic_expand` with a full namespaced reference. It resolves references from the current session and from another locally retained session ledger. Invalid sessions, handles, and spans return an explicit tool error rather than partial or empty content.

Operators can recover the same content without starting OMP:

```text
laconic expand '<session-id>/F1'
laconic expand '<session-id>/F1:20-40'
```

`laconic expand` writes exact recovered content to stdout. Use `--data-dir PATH` when the install pinned a non-default runtime directory.

## Inspect and control

Inspect installed adapters and content-free aggregate ledger health:

```text
laconic status
laconic status --format json
```

The CLI reports project/user adapter state, session and decision counts, pass-through count, character totals, and expansion counts. It does not read raw observation bodies. The Python engine is session-owned, so live engine state, circuit-breaker state, pass-through reasons, and latency percentiles are available inside OMP:

```text
/laconic status
/laconic pause
/laconic resume
```

Pause stops the current engine and restores native pass-through behavior. Resume starts a fresh engine for the current OMP session. A failed resume remains fail-open and reports the unavailable state.

## Uninstall

Preview and remove only Laconic's owned extension:

```text
laconic uninstall omp --dry-run
laconic uninstall omp
```

Use the same `--scope user`, `--profile NAME`, or `--user-dir PATH` selector used at installation. Uninstall refuses to remove a foreign file. It never deletes recovery ledgers.

## Purge retained ledgers

Purge is separate, explicit, and selector-bound. Preview before applying:

```text
laconic purge --session '<session-id>' --dry-run
laconic purge --session '<session-id>'
laconic purge --older-than 30d --dry-run
laconic purge --older-than 30d
```

Exactly one of `--session` or `--older-than` is required. Supported duration suffixes are `s`, `m`, `h`, `d`, and `w`. Purge accepts only ordinary, path-contained ledger files and fails before deletion if the storage root, sessions directory, or a selected ledger is unsafe. JSON previews expose opaque ledger paths and aggregate counts, not stored content.

## Research and diagnostics namespaces

The runtime owns the top-level product verbs. Existing offline and diagnostic commands moved without compatibility aliases:

```text
laconic research measure ...
laconic research replay ...
laconic research gates ...
laconic research expand ...
laconic research view ...
laconic research study ...
laconic research k1 ...
laconic diagnostics observe ...
```

`laconic expand` resolves live runtime references. `laconic research expand` reconstructs handles from a supplied transcript corpus.

## Safety and evidence boundary

The runtime reports character counts because it compares exact strings at the tool boundary. Character reduction is not a token, cost, cache, or behavior claim. General savings remain governed by representative paired research.

## Beta qualification result

The predeclared qualification campaign completed and its report is committed verbatim at [`docs/runtime-beta-report.md`](runtime-beta-report.md). It was generated from per-session receipts by `python -m laconic.beta report generate`, and `report check` refuses a committed report that has drifted from its evidence. Read that report with these facts about how it was produced:

- **Campaign.** Ten OMP 18.1.10 sessions, all reaching a clean `session_shutdown`, across three canonical Git roots, producing 137 observations the codec actually evaluated. Every safety counter is zero: no emitted reference failed exact recovery, no tool error was compressed, no envelope was larger than its raw content, and no result corruption was observed.
- **Sessions were agent-driven.** Nine sessions ran headless (`omp -p`) and one interactively, all performing read-only repository investigation under an automated prompt rather than a human's own coding work. Ledger contents are genuine engine output; the work those sessions performed is narrower than general daily use.
- **Repositories were local clones** of three unrelated projects pinned at fixed commits, not live working checkouts.
- **The observed 35.84% character reduction is descriptive of that workload only.** Read-heavy investigation is the shape the codec handles best; 96 of 137 eligible observations were still passed through unchanged because their envelope would not have been smaller. No minimum savings figure gates the beta, and none is claimed.
- **Faults were injected deliberately** — absent interpreter, non-executable interpreter, engine killed mid-session, one malformed protocol frame, and one response stalled past the 250 ms deadline. In every case the host returned the original observation and the session completed; measured latency was 1.45 ms at p50 and 18.65 ms at p95.
- **The qualified wheel predates one fix.** The campaign's crash scenarios exposed a defect in `laconic status` and `laconic purge --older-than`, which could not read a ledger whose writer had been killed. That fix landed after the candidate wheel was frozen, so the wheel the beta ships must include it.
