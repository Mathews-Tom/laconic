# K1 Stage B Manifest — Operator Guide

`laconic research k1 stage-b build-manifest` is the only command this tool provides. It
turns H-59's frozen *lineage*-level corpus design
(`.docs/DEVELOPMENT_PLAN_HISTORY.md`, `.docs/K1_REPRESENTATIVE_CORPUS_PROTOCOL.md`
§ Stage B) into a frozen, reviewable, *session*-level manifest.

It never reads a transcript body, prompt, tool argument/result, assistant
response, source file, credential, or title -- every content read is the same
bounded `cwd`-only scan `laconic research k1 stage-a scan` already performs. It never
contacts a provider, invokes a live replay, or spends money. It never
recomputes H-59's frozen design/confirmatory lineage split -- it only checks
lineage-ID membership against it.

## Command

```text
laconic research k1 stage-b build-manifest [--corpus-manifest PATH] [--out PATH]
```

`--corpus-manifest` defaults to `.laconic/k1/stage_b/corpus_manifest.json`
(H-59's frozen decision). `--out` defaults to
`.laconic/k1/stage_b/session_manifest.json`. Both are local, gitignored
(`.laconic/k1/` convention), and written `0o700`/`0o600` atomically.

## What it does

1. Loads the frozen lineage-level decision (`design_set`, `confirmatory_set`,
   and the frozen `time_window_days`/`size_band_exclusion`/`per_lineage_session_cap`
   rules) from `--corpus-manifest`.
2. Re-runs the same admission pipeline `laconic research k1 stage-a scan` uses, anchored
   to the manifest's `frozen_at` timestamp -- **never wall-clock time** -- so
   the result is reproducible regardless of when this command actually runs.
3. Restricts the result to sessions whose opaque `project_lineage_id` is in one
   of the two frozen sets; applies the frozen per-lineage cap (most-recent
   first, ties broken by session ID).
4. Computes the same totals H-59 recorded (eligible lineages/sessions,
   design/confirmatory lineage and session counts) and compares them against
   the frozen manifest's `totals`. **A mismatch is a hard stop**: nothing is
   written, and the command exits non-zero with the exact disagreement printed.
5. On a match, writes the session-level manifest: one record per selected
   session with `set` (`design`/`confirmatory`), opaque `provider`,
   `session_id`, `project_lineage_id`, `size_band`, `age_band`, and a
   `provenance_hash` -- identical fields to Stage A's ledger schema, plus
   `set`. No real path or exact timestamp is ever written.

## Reading a totals mismatch

```text
$ laconic research k1 stage-b build-manifest
laconic research k1 stage-b build-manifest: session-level totals disagree with H-59's frozen totals: confirmatory_sessions_postcap: frozen=285 observed=283
```

This means real files changed since the freeze (e.g. a session was deleted, or
aged past the 180-day boundary between the freeze and this run) -- or a defect
exists. Either way, review before re-running; the command never silently
adjusts the frozen numbers or emits a partial manifest.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success -- manifest written, totals matched H-59 exactly |
| 23 | totals mismatch, or the frozen `--corpus-manifest` could not be read |

## The resolver (not invoked by this command)

`laconic.k1corpus.resolve.resolve_session_path` can re-derive a real file from
one of this manifest's opaque `session_id` values, for a future,
separately-authorized Stage C plan that needs to actually replay a session. It
is exercised only by its own tests -- nothing in this repository invokes it
against a replay engine, provider, or any content-reading code path.

## What this CLI does not do

- It does not select or freeze a corpus -- that is H-59, already recorded.
- It does not read or store a session's prompt, tool result, assistant
  response, source code, credential, or title, at any point.
- It does not replay a session, call a provider, or spend money (Stage C).
