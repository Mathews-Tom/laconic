# Laconic M18 Beta Qualification — Operator Runbook

M18 (`.docs/DEVELOPMENT_PLAN.md` §6; refocus design §§9–10) qualifies the
OMP runtime candidate against real, ordinary OMP sessions before the
`v0.9.0` beta may release. This runbook is the exact operator sequence for
running that campaign with `python -m laconic.beta`: freezing the contract,
capturing evidence, and generating the report that decides GO, NO-GO, or
`human_review_required`.

Nothing this tool writes to a file contains raw tool output, subjects,
commands, paths, prompts, credentials, tool arguments, or a real session ID.
That boundary covers written artifacts; terminal output is deliberately
outside it, since a command may name a path you yourself typed on the same
command line. No command echoes a value read out of an evidence file, and
none echoes a session ID at all. Local campaign state — the frozen manifest,
per-session receipts, and any generated report before it is copied into
`docs/runtime-beta-report.md` — lives under the git-ignored `.laconic/beta/`
directory. Only the finished, aggregate-only report is ever committed.

## 1. Freeze the manifest before looking at results

The campaign's minimums, eligible OMP version, and required scenarios are
fixed by `.docs/DEVELOPMENT_PLAN.md` §6 M18 and cannot change once a campaign
has started. Neither can its *population*: which candidate build is under
test, and which of the 10 sessions runs against which of the 3 repositories.
Build the candidate wheel (§4) first, then freeze both before running a
single session:

```text
uv build
python -m laconic.beta manifest generate \
  --candidate-wheel dist/laconic-0.8.0-py3-none-any.whl \
  --out .laconic/beta/manifest.json \
  /path/to/repo-one /path/to/repo-one /path/to/repo-one /path/to/repo-one \
  /path/to/repo-two /path/to/repo-two /path/to/repo-two \
  /path/to/repo-three /path/to/repo-three /path/to/repo-three
python -m laconic.beta manifest validate .laconic/beta/manifest.json
python -m laconic.beta manifest hash .laconic/beta/manifest.json
```

The 10 positional roots are the campaign's slots in order: the first is slot
1, the last is slot 10. Each must be a canonical Git root — a directory with
a real `.git` directory, so a linked worktree or submodule is refused — and
the 10 must name exactly 3 distinct repositories. `generate` refuses anything
else, so the report's repository count always means what the plan's
acceptance criterion says it means. Only their SHA-256 digests reach the
manifest; no path is ever written.

`generate` also refuses to overwrite an existing manifest. Re-freezing over a
campaign already in flight invalidates every receipt collected so far, so
replacing one takes an explicit `--force` and means discarding and restarting
the campaign.

`validate` fails loudly on any manifest that does not reproduce the frozen
contract exactly — a lowered minimum, a renamed scenario, a relaxed eligible
OMP version, a 9-slot population, or a second candidate wheel all refuse
rather than silently pass. Copy `hash`'s printed digest into the design-gate
history entry required by the plan's "Record a hash of the frozen campaign
manifest/schema" step before collecting any session. Every receipt and report
below binds to this exact hash; a manifest edited after this point produces a
`stale` rejection at report time, not a silently accepted change.

## 2. Run real sessions and collect scenario evidence

Install the candidate build (`docs/omp-runtime.md`) and drive ordinary OMP
work through the 10 sessions the manifest froze, across its 3 canonical Git
repositories, each session reaching a clean `session_shutdown`. Across the
campaign, exercise every pre-signoff scenario at least once:

```text
engine_absence, spawn_failure, process_crash, malformed_response, timeout,
pause, resume, session_switch, branch_tree_navigation, resumed_session,
inherited_reference_expansion, candidate_wheel_install, actual_omp_load,
status, full_expansion, span_expansion, disablement,
candidate_wheel_uninstall, purge_session_preview, purge_older_than_preview,
tool_error_passthrough, unsupported_tool_passthrough,
mixed_content_passthrough, details_preserved
```

Note, per session, which of these scenarios that session's evidence covers
(a session may cover more than one), whether OMP reached a clean shutdown,
wall-clock start/end timestamps, and any result corruption you observed at
the OMP boundary (`docs/omp-runtime.md` "Safety and evidence boundary" —
this should always be `0`; a real observation goes into the receipt's
`observed_corruption` count and blocks GO).

## 3. Derive one receipt per session

Each session's Python runtime ledger already holds its exact decision,
latency, and expansion history (`docs/system-design.md` §5.1). Derive a
receipt directly from it — this reopens the session through `RuntimeStorage`,
never a second store, and independently re-verifies every emitted reference
byte-for-byte against the stored raw record, through the same expansion path
`laconic_expand` uses:

```text
python -m laconic.beta receipt derive \
  --data-dir "$LACONIC_DATA_DIR" \
  --session '<real-omp-session-id>' \
  --manifest .laconic/beta/manifest.json \
  --omp-version 18.1.10 \
  --candidate-wheel dist/laconic-0.8.0-py3-none-any.whl \
  --slot 1 \
  --repository /path/to/repo-one \
  --clean-shutdown \
  --started-at 1699999000 \
  --ended-at 1699999600 \
  --scenarios pause,resume,full_expansion,span_expansion \
  --out .laconic/beta/receipts/slot-01.json
```

`--session` and `--repository` take the real session ID and filesystem path
as CLI input only; the written receipt carries only their SHA-256 digest
(`repository_id`) or nothing at all (the session ID is never serialized).
`--slot` names one of the 10 slots the manifest already froze, and
`--repository` must be exactly the root the manifest bound to that slot —
deriving slot 1 against slot 5's repository fails, so the population cannot
drift once results start arriving. `--candidate-wheel` must likewise hash to
the frozen wheel. Use `--no-clean-shutdown` for a session that did not reach
a clean `session_shutdown`; it is still recorded, but does not count toward
the 10-session minimum. Validate a receipt on its own at any time:

```text
python -m laconic.beta receipt validate .laconic/beta/receipts/slot-01.json
```

## 4. The candidate-wheel workflow

The manifest and every receipt bind the exact SHA-256 of one candidate
distribution. M18 qualifies the current `0.8.0` build; the version bump to
`v0.9.0` happens at release preparation, after this campaign returns `go`, so
the artifact under test is a `0.8.0` wheel:

```text
uv build
ls dist/laconic-0.8.0-py3-none-any.whl
```

Build it once, freeze it into the manifest (§1), and reuse that exact file
for every session and every install/uninstall exercise. A report refuses to
aggregate receipts binding a different wheel hash than the manifest's —
mixing evidence from two candidate builds is exactly the inconsistency the
freeze exists to prevent. Rebuilding mid-campaign invalidates every
already-collected receipt: finish the campaign on one build, or discard and
restart with the new one.

## 5. Preview before applying purge

`purge_session_preview` and `purge_older_than_preview` (`laconic purge
--session ... --dry-run` / `laconic purge --older-than ... --dry-run`,
`docs/omp-runtime.md` "Purge retained ledgers") are pre-signoff scenarios:
exercise both previews during ordinary qualification and tag the covering
receipt's `--scenarios` accordingly. Previews never delete anything, so
they may run at any time before the human review gate is signed.

## 6. Post-signoff: apply both purge forms

`.docs/DEVELOPMENT_PLAN.md` §6 M18's human review gate is explicit: real
ledger deletion (`laconic purge --session ...` and `laconic purge
--older-than ...`, applied, not previewed) may only run after a human has
reviewed the campaign manifest, aggregate report, raw-data exclusion check,
fail-open evidence, latency distribution, and install/uninstall evidence.
Do not run `purge_session_apply` or `purge_older_than_apply` before that
review. Once signed, apply both forms once each against real retained
ledgers, and derive or amend a receipt tagging both scenarios. A report
generated before both post-signoff scenarios are covered reports
`human_review_required` even when every other criterion already passed;
it is not a hard failure, and it is not `go`.

## 7. Generate and check the report

Once every receipt is collected:

```text
python -m laconic.beta report generate \
  --receipts-dir .laconic/beta/receipts \
  --manifest .laconic/beta/manifest.json \
  --out .laconic/beta/report.md
```

The generator refuses outright — before producing any report — on empty (no
receipts), partial (a receipt set that is not exactly the manifest's 10
declared slots), duplicate (a repeated slot), stale (a receipt bound to a
different manifest, receipt schema, or candidate wheel than the frozen one),
mutated (a receipt whose own numbers do not add up, or whose repository does
not match its frozen slot — the signature of a hand-edited file), or
privacy-invalid (an extra/missing key, or a value outside its allowed enum)
receipt set. A successful report states the frozen contract pin (manifest
hash, receipt schema hash, candidate wheel hash, eligible OMP version, and
the frozen minimums), sessions/repositories/observations against those
minimums, decision and expansion totals, nearest-rank p50/p95 latency, every
safety counter (all must be `0` for `go`), scenario coverage against both
closed vocabularies, the observed character reduction (informational only —
no minimum threshold gates the verdict), and the verdict itself.

Two composition counts are reported separately, and only one of them gates
the campaign. *Recorded decisions* is every observation the extension
intercepted. *Eligible observations* is the subset Laconic actually evaluated
for compression — the engine records no candidate reference for an
unsupported tool or a failed one, and those never count. The 100-observation
minimum applies to the eligible count, so a campaign cannot clear it on
traffic the codec never looked at.

Expect two reports, in this order:

1. **Before the human review gate is signed**, every pre-signoff scenario is
   covered but neither post-signoff purge has run, so the report reads
   `human_review_required`. This is the report a human reviews at the gate
   (§6); it is not a failure, and it cannot read `go` yet.
2. **After signoff**, once a receipt covers both applied-purge scenarios,
   regenerate. Only that regenerated report can read `go`.

Copy the post-signoff `go` report's contents into the tracked
`docs/runtime-beta-report.md` (a later PR's deliverable) verbatim; never
hand-edit the aggregate figures. Whenever that file exists, check it has not
drifted from the current receipt evidence:

```text
python -m laconic.beta report check \
  --receipts-dir .laconic/beta/receipts \
  --manifest .laconic/beta/manifest.json \
  --report docs/runtime-beta-report.md
```

`check` exits non-zero the moment the committed report's bytes no longer
match what the current manifest and receipts would render — for example,
after a receipt is added, corrected, or a stale one is discovered — so a
stale published report cannot survive unnoticed.
