# Laconic Grounding Charter

## The contract

**Laconic is a private, local runtime codec for existing coding agents. It reduces model-visible tool observations while preserving exact, on-demand access to omitted content.**

The primary product experience is automatic: an explicitly installed host adapter intercepts an eligible successful textual tool result, stores the exact raw result locally, and replaces it only when the complete recovery-bearing envelope is smaller. The agent can expand the full result or a line span. The operator can inspect, pause, disable, uninstall, and purge Laconic.

The first product surface is an OMP extension backed by one canonical Python engine. Claude Code follows only after the protocol survives OMP dogfood. Observe, replay, gates, rendering, and K1 remain supporting diagnostics and research infrastructure.

## Explicit non-goals

Laconic is not:

- a replacement coding agent, model provider, model router, or agent orchestrator;
- a prompt-style or prose-compression product;
- a transcript archive, provenance platform, WORM store, project-state manager, or handoff system;
- a monitoring dashboard whose only effect is reporting;
- an MCP-only gateway that misses built-in tool results;
- a history-rewriting residency compactor or action/edit rewriter in the first runtime beta;
- a source of universal token, cost, cache, or behavior claims from character reduction.

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

- Package version: `0.8.0` (`pyproject.toml`). The published package has no live codec integration.
- The repository's unreleased runtime candidate includes the canonical session engine, owner-only namespaced recovery storage, an ownership-safe native OMP extension, a 250 ms fail-open boundary, a three-failure circuit breaker, model/operator expansion, pause/resume controls, content-free status, and explicit purge.
- The runtime adapter transforms only successful single-text `read`, `bash`, `grep`, and `glob` results, and only when the complete recovery-bearing envelope is strictly smaller. M18 real-OMP qualification and human sign-off still block release.
- Laconic Observe (`laconic.observe`, `laconic diagnostics observe install/remove/status/report`) remains a released local, content-free diagnostic surface for Claude Code and OMP. It does not transform agent-visible tool results.
- The committed fixture reports K1 net savings of **8.53%**, K2 action equivalence of **100%**, K4 overhead of **26.8 tokens**, K5 difference of **0.0pp**, and K3 as manual/not evaluated. The fixture validates the gate machinery; it is not representative product-economics evidence.
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
- does not expand the first beta into action rewriting, history compaction, MCP, hosted services, project state, routing, or orchestration without new runtime evidence and an explicit design decision.
