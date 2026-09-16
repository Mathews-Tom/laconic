# Laconic M18 Runtime Beta Qualification — Aggregate Report

- Manifest hash: `f1d650811c55d61779540b894b373e97cb757b48c187b0b84f0364a5086958a3`
- Receipt schema hash: `f3ac05c99db3266463f5c6f8c1a6b6cd4d18e397d598b25e31ccc89a4bee2bf1`
- Candidate wheel SHA-256: `5c8d532b2bd87195357569b7fdd163bed328eff415778a95a5d1fbe0a1a60330`
- Eligible OMP version: 18.1.10
- Frozen minimums: 10 sessions, 3 repositories, 100 eligible observations
- Generated at (epoch seconds): 1789591298.030
- Verdict: **go**

## Campaign composition

| Metric | Value |
| --- | --- |
| Sessions total | 10 |
| Sessions completed (clean shutdown) | 10 |
| Distinct repositories | 3 |
| Recorded decisions | 162 |
| Eligible observations (compression attempted) | 162 |

## Decisions and expansions

| Metric | Value |
| --- | --- |
| Emitted | 72 |
| Pass-through: not_smaller | 90 |
| Raw characters | 822731 |
| Visible characters | 396773 |
| Characters avoided | 425958 |
| Observed reduction | 51.77% |
| Full expansions | 12 |
| Span expansions | 3 |
| Latency p50 (ms, nearest-rank) | 2.16 |
| Latency p95 (ms, nearest-rank) | 19.87 |

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
