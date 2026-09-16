# Laconic Grounding Charter

## The contract

**Laconic is a private, local runtime codec for existing coding agents. It reduces model-visible tool observations while preserving exact, on-demand access to omitted content.**

The primary product experience is automatic: an explicitly installed host adapter intercepts an eligible successful textual tool result, stores the exact raw result locally, and replaces it only when the complete recovery-bearing envelope is smaller. The agent can expand the full result or a line span. The operator can inspect, pause, disable, uninstall, and purge Laconic.

The first product surface is an OMP extension backed by one canonical Python engine. Claude Code followed in 0.11.0, after the protocol survived OMP dogfood, and the sequencing is the standing rule rather than a one-off: a new host adapter ships only after the protocol has survived dogfood on an existing one. Observe, replay, gates, rendering, and K1 remain supporting diagnostics and research infrastructure.

## Explicit non-goals

Laconic is not:

- a replacement coding agent, model provider, model router, or agent orchestrator;
- a prompt-style or prose-compression product;
- a transcript archive, provenance platform, WORM store, project-state manager, or handoff system;
- a monitoring dashboard whose only effect is reporting;
- an MCP-only gateway that misses built-in tool results;
- a history-rewriting residency compactor or action/edit rewriter in the first runtime beta;
- a source of universal token, cost, cache, or behavior claims from character reduction.

An approved static project and evidence site is documentation publication, not a hosted Laconic runtime or service. It may explain the product and publish frozen, privacy-safe aggregate evidence, but it cannot transform results, collect telemetry, read private stores, call providers, or turn observed character reduction into a general savings claim.

## Product and research gates

Two decisions have separate evidence requirements:

1. **Product gate:** Is an opt-in local runtime safe enough for a bounded beta? This requires exact recovery, fail-open behavior, bounded latency, operator control, privacy, packaging, and successful use in real OMP sessions.
2. **Research claim gate:** Does Laconic create general net token or cost savings without changing agent behavior? This requires representative paired evidence, model-specific tokenization and cost accounting, induced-work measurement, cache analysis, and behavior evaluation.

The research claim gate does not block the opt-in beta. Passing the product gate does not satisfy the research claim gate.

## Invariants

1. **Recoverability:** raw content commits before an envelope is emitted, and every emitted reference expands exactly.
2. **Strictly smaller:** Laconic replaces a result only when the complete model-visible envelope is smaller than the original.
3. **Fail open:** unsupported inputs, errors, storage failures, protocol failures, crashes, and latency breaches preserve the original tool result.
4. **Local and private:** raw observations and recovery ledgers stay local; diagnostics and reports exclude content, subjects, tool arguments, prompts, credentials, and paths.
5. **Operator control:** installation is explicit, status is inspectable, pause and uninstall restore native client behavior, and purge is separate and deliberate.
6. **Claims match evidence:** character reduction is not renamed as token, cost, cache, or behavior improvement.
7. **No post-result tuning:** beta criteria and research thresholds freeze before the evidence they judge is visible.
8. **Human outcome is separate:** K3 remains a participant study and is never inferred from renderer quality or a simulated dry run.

## Current state, as measured

- Package version: `0.12.0` (`pyproject.toml`). The runtime beta has been published since `v0.9.0`; `v0.9.1` corrected a cold-start defect that only a clean first install could reach. Version 0.10.0 added read-only spend-composition reporting. Version 0.11.0 extends the codec to Claude Code through a transforming `PostToolUse` hook, adds guided `laconic setup` onboarding and a `laconic savings` command, and attaches a modelled avoided-cost estimate to the spend report. Version 0.11.1 makes that estimate prominent in `laconic status` and names the pricing it inherits. Version 0.12.0 makes the price registry the only price source — a hand-written table that shadowed it had drifted on two models — and reports the modelled total for the sessions the host itself priced beside the host's own figure, so the two cover one set. On the development corpus that moved the modelled-versus-host gap from +8.12% to −3.99%, and it moved K1's committed fixture from 8.53% to 8.41%. That estimate is a model and never a measurement: every recorded session ran with the codec enabled, so no counterfactual exists, and K1's committed 8.41% fixture still bounds any general savings claim.
- The runtime surface includes the canonical session engine, owner-only namespaced recovery storage, an ownership-safe native OMP extension, a 250 ms fail-open boundary, a three-failure circuit breaker, model/operator expansion, pause/resume controls, content-free status, and explicit purge.
- The runtime adapter transforms only successful single-text `read`, `bash`, `grep`, and `glob` results, and only when the complete recovery-bearing envelope is strictly smaller. M18 real-OMP qualification has passed and its human review gate is signed — ten completed OMP 18.1.10 sessions across three canonical Git roots, 137 eligible observations, every safety counter zero, latency 1.45 ms at p50 and 18.65 ms at p95, and 35.84% character reduction on that read-heavy agent-driven workload (`docs/runtime-beta-report.md`). The beta is published and collecting ordinary-use evidence.
- `laconic research spend report` measures where model spend went in readable local sessions and joins that usage to runtime decisions without modifying either source. The measurement is single-arm and contains no savings figure. M20-v1's frozen controlled comparison ended incomplete with 0 valid cells and null statistics. M20-v2 corrected the operational validity mechanics and executed once under owner authorization, completing with 24 of 24 valid cells and $0.9975374 pooled gateway spend. It reports dispersion, cross-arm cost correlations, and a confirmatory requirement of 15 tasks at two repetitions; it reports no arm means and no paired effect, so it is not a savings result.
- Laconic Observe (`laconic.observe`, `laconic diagnostics observe install/remove/status/report`) remains a released local, content-free diagnostic surface for Claude Code and OMP. It does not transform agent-visible tool results.
- The committed fixture reports K1 net savings of **8.41%**, K2 action equivalence of **100%**, K4 overhead of **23.56 tokens**, K5 difference of **0.0pp**, and K3 as manual/not evaluated. The fixture validates the gate machinery; it is not representative product-economics evidence.
- The source-mapped K1 Stage C replacement pilot ended before replay client construction. All 11 selected Codex and 13 selected OMP baselines produced zero replay-engine turns, actions, and observations because the historical parser accepts Claude-shaped tool-use records. No provider prompt, replay artifact, external annotation, or modeled spend resulted.

## Runtime beta gate

The OMP beta is releasable only after at least 10 Laconic-enabled sessions complete across at least 3 canonical Git repositories with at least 100 eligible observations, and all of these hold:

- zero emitted references fail exact full expansion;
- zero tool errors are compressed;
- zero result corruption occurs outside the selected text replacement;
- every emitted envelope is strictly smaller than its raw input;
- engine absence, spawn failure, crash, malformed response, timeout, pause, resume, session switch, branch navigation, resumed sessions, and inherited or forked reference expansion are exercised;
- latency p50 and p95, emitted/pass-through counts and reasons, character totals, and full/span expansions are reported;
- a built package installs, loads in actual OMP, reports status, expands content, exercises disablement and uninstall, and exercises both `purge --session` and `purge --older-than`.

There is no minimum aggregate savings percentage in this safety gate. Low observed reduction is reported and informs continuation; it is not repaired by changing the threshold after results are visible.

## Drift history

| Drift | Symptom | Corrective boundary |
| --- | --- | --- |
| V1 prose compression | A 44.6% output headline looked like a coding-agent cost product. | Real-session composition found prose was only 2.30% of spend; V1 is retired. |
| Product/research gate conflation | Fixture or infeasible representative replay prevented any runtime from generating prospective evidence. | Safety gates the opt-in product; representative evidence gates general savings and behavior claims. |
| Provenance as product | Archive, provider replay, WORM, and capture work dominated the roadmap. | Preserve the evidence, but do not treat collection infrastructure as the product. |
| Surface inversion | Observe and prospective infrastructure outranked the transform users need. | OMP runtime is the first product surface; Observe is supporting diagnostics; MCP is deferred. |
| Mechanism expansion | Action compression and residency rewriting appeared alongside observation delivery. | Ship the observation-only boundary first; admit later mechanisms only from runtime evidence. |

## Drift checks

Before proposing work, verify that it:

- advances the OMP runtime, exact recovery, fail-open behavior, operator control, packaging, or bounded dogfood proof;
- does not revive historical replay, external data, or provider spend as a product prerequisite;
- does not claim real-session token, cost, cache, or behavior savings from the synthetic fixture or raw character reduction;
- does not treat K3 dry-run data as participant evidence;
- does not expand the runtime into action rewriting, history compaction, MCP, hosted execution or telemetry services, project state, routing, or orchestration without new runtime evidence and an explicit design decision. Static documentation and frozen aggregate evidence publication remain allowed when they preserve the local-runtime, privacy, and claims boundaries above.
