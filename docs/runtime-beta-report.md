# Laconic M18 Runtime Beta Qualification — Aggregate Report

- Manifest hash: `6be01fdd0286137ce53bc7123aa95022b95c7ee4a2f9f093039b6abfcc891f26`
- Receipt schema hash: `f3ac05c99db3266463f5c6f8c1a6b6cd4d18e397d598b25e31ccc89a4bee2bf1`
- Candidate wheel SHA-256: `d9c4f4c191915d86a43aee72cca832e940a8ae360e830f7b815f2a814a2aa0b6`
- Eligible OMP version: 18.1.10
- Frozen minimums: 10 sessions, 3 repositories, 100 eligible observations
- Generated at (epoch seconds): 1788639654.844
- Verdict: **go**

## Campaign composition

| Metric | Value |
| --- | --- |
| Sessions total | 10 |
| Sessions completed (clean shutdown) | 10 |
| Distinct repositories | 3 |
| Recorded decisions | 137 |
| Eligible observations (compression attempted) | 137 |

## Decisions and expansions

| Metric | Value |
| --- | --- |
| Emitted | 41 |
| Pass-through: not_smaller | 96 |
| Raw characters | 610001 |
| Visible characters | 391384 |
| Characters avoided | 218617 |
| Observed reduction | 35.84% |
| Full expansions | 22 |
| Span expansions | 1 |
| Latency p50 (ms, nearest-rank) | 1.45 |
| Latency p95 (ms, nearest-rank) | 18.65 |

## Safety counters (must be zero for a GO verdict)

| Counter | Value |
| --- | --- |
| Exact expansion failures | 0 |
| Compressed tool errors | 0 |
| Oversized envelopes | 0 |
| Observed corruption | 0 |

## Scenario coverage

- Pre-signoff covered: actual_omp_load, branch_tree_navigation, candidate_wheel_install, candidate_wheel_uninstall, details_preserved, disablement, engine_absence, full_expansion, inherited_reference_expansion, malformed_response, mixed_content_passthrough, pause, process_crash, purge_older_than_preview, purge_session_preview, resume, resumed_session, session_switch, span_expansion, spawn_failure, status, timeout, tool_error_passthrough, unsupported_tool_passthrough
- Pre-signoff missing: (none)
- Post-signoff covered: purge_older_than_apply, purge_session_apply
- Post-signoff missing: (none)

## Verdict

**go**

Reasons: (none)

No minimum aggregate savings percentage controls this verdict (`.docs/DEVELOPMENT_PLAN.md` §6 M18; refocus design §9): observed character reduction above is reported for information only.

