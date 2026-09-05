# K1 Stage A — Operator Guide

`laconic research k1 stage-a scan` is the only command this tool provides. It is a
metadata feasibility screen governed by
`.docs/K1_REPRESENTATIVE_CORPUS_PROTOCOL.md` § Stage A and authorized by
`.docs/DEVELOPMENT_PLAN_HISTORY.md` H-53: it enumerates historical Claude
Code, Codex, and OMP session files under two source roots the project owner
explicitly authorized, admits only closed and unambiguously in-scope files,
and writes a body-free metadata ledger plus a stop-condition disposition.

It never reads a transcript body, prompt, tool argument/result, assistant
response, source file, credential, or title. It never contacts a provider,
installs a hook, or changes a codec setting or K1 threshold. A
`proceed_to_stage_b_request` disposition is not itself an authorization to
proceed to Stage B (corpus-design freeze) -- that remains a separate,
explicit decision by the project owner after reviewing the ledger.

## Command

```text
laconic research k1 stage-a scan [--out PATH]
```

`--out` defaults to `.laconic/k1/stage_a/ledger.json` (already covered by
the repository's gitignored `.laconic/k1/` convention; nothing under it is
ever committed). The output directory is created `0o700` and the ledger
file is written `0o600`, atomically (temp file plus rename).

## Scope (fixed, not a CLI parameter)

- **Source roots:** `~/WorkSpace/AetherForge` and
  `~/WorkSpace/Retailogists/GitHub`, and every project nested under either.
  No flag broadens this.
- **Providers:** Claude Code, Codex, and OMP, read from their real local
  session-storage roots (`~/.claude/projects`, `~/.codex/sessions`,
  `~/.omp/agent/sessions`).

## What gets admitted

A session file is admitted only if, in order:

1. it is not a symlink;
2. it is a regular file;
3. it has not been modified in the last 30 minutes (the documented
   "closed" proxy -- see `.docs/K1_STAGE_A_DESIGN.md` §6.3);
4. its filename matches the provider's expected shape closely enough to
   extract an opaque session identifier;
5. a `cwd` value can be found within the first 50 lines of the file
   (or, for Codex, nested under that line's `payload.cwd`);
6. that `cwd`, resolved, is one of the two authorized roots or nested
   under one.

Every file that fails any of these checks is excluded and counted by
reason -- never silently dropped, never guessed into inclusion.

## Ledger contents

One row per admitted file: an opaque `provider:<uuid>` session ID, an
opaque truncated-hash project lineage ID (the same project always yields
the same ID; the real path is never stored), `closure_status` (always
`"closed"` -- an active file is excluded before a row is ever built),
`size_band`/`age_band` (coarse buckets, never exact values), and a
`provenance_hash` (a re-derivable audit fingerprint of the file's path,
size, and mtime -- not a concealment mechanism).

The ledger header also carries `roots` (the opaque `root_a`/`root_b`
labels, never the literal paths), `providers`, `admitted_count`, an
`exclusions` breakdown by provider and reason, the four Stage A stop
conditions with their fired/not-fired state, and the resulting
`disposition`.

## Reading the disposition

```text
$ laconic research k1 stage-a scan
K1 Stage A scan -- 41 admitted session(s) across 2 authorized root(s) and 3 authorized provider(s).
Ledger written to .laconic/k1/stage_a/ledger.json

Exclusions:
  claude-code     12  (active=3, outside_allowlist=9)
  codex            4  (cwd_not_found_within_scan_bound=4)
  omp              2  (active=2)

Stop conditions:
  [ok   ] distinct_lineage_count: 9 distinct project lineage(s) observed; minimum 3 required.
  [ok   ] single_provider_surface: 3 of 3 authorized provider(s) contributed an admitted session.
  [ok   ] no_closed_sessions: 41 closed, in-scope session(s) admitted.
  [ok   ] ambiguous_association: Stage A performs no directory-name inference and admits a session only on an explicit, unambiguous cwd match; review the exclusion breakdown for any count that suggests a project's self-owned status could not be classified confidently.

Disposition: proceed_to_stage_b_request
```

Exit code `0` means `proceed_to_stage_b_request`; exit code `22`
(`EXIT_K1_STAGE_A_STOP`) means `stop` -- at least one stop condition
fired. Either way, review the printed summary and the written ledger
before deciding whether to request Stage B authorization; this command
never makes that request itself.

## What this CLI does not do

- It does not compute, report, or imply a K1 percentage.
- It does not select, freeze, or design a corpus (Stage B).
- It does not collect paired codec-on evidence or contact a provider
  (Stage C).
- It does not read or store a session's prompt, tool result, assistant
  response, source code, credential, or title, at any point.
