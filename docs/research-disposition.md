# Research Disposition

This document preserves the evidence that shaped Laconic without allowing an incomplete research programme to stand in for the product roadmap.

## Decision boundary

Laconic now separates two questions:

- **Product:** Can a private, opt-in runtime compress eligible tool observations with exact recovery, fail-open behavior, bounded latency, clear operator controls, and no remote telemetry?
- **Research:** Does that mechanism produce general net token or cost savings without changing agent behavior across a representative population?

The first question governs the OMP beta. The second governs general savings and behavior claims. Neither answer substitutes for the other.

## Disposition ledger

| Workstream | Evidence | Disposition |
| --- | --- | --- |
| V1 prose compression | The mechanism reduced output tokens by 44.6% in its benchmark, but human-facing prose represented 2.30% of measured real-session spend. | Retired as a product direction. Preserve the measurement as evidence that channel selection matters. |
| V2 observation codec | Deterministic encoders and exact ledger recovery are implemented. Tool results represented 63.24% of measured context volume; their repeated residency in cached context was estimated at approximately 38.1% of modeled spend. | Active product core. Integrate it first as an observation-only runtime; do not attribute the residency estimate to first-emission compression or infer realized token or cost savings from channel share. |
| Committed K1 fixture | The fixture reports 8.41% net savings against a pre-registered 15% kill threshold. K2 reports 100% action equivalence, K4 23.56 tokens of overhead, K5 0.0pp difference, and K3 remains manual. | Keep as a deterministic fixture and gate-harness check. It is not a representative real-world benchmark and no longer blocks an opt-in runtime beta. It does not support general savings claims. |
| Claude Code codec adapter | A transforming `PostToolUse` hook, shipped in 0.11.0 after the protocol survived OMP dogfood. It replaces `Bash` and `Read` output through the same `RuntimeSession` the OMP transport drives, rather than forking the decision. Replayed over 13,796 eligible tool results from real Claude Code history it transformed 1,189 of them, removing 3,951,218 characters. | Active product surface. The transform rate is a population property: `Bash` results have a median of 442 characters and the codec correctly declines what it cannot shrink. Characters at the tool boundary, never relabelled as tokens or money. |
| Modelled avoided-cost estimate | Shipped in 0.11.0 and made prominent in 0.11.1. A band, not a figure, labelled `modelled_not_measured` in the serialized report and refused by the privacy gate under any other basis. | Retain as a bounded local estimate. Every recorded session ran with the codec enabled, so it has no counterfactual and is not a savings result. |
| Model price registry | 0.11.1 measured that 75.4% of the estimate's own denominator rested on models with no published list price, and gated the dollar figures above a 25% fallback share. 0.12.0 replaced the pricing source with a three-layer registry — local override, downloaded, and a bundled snapshot of 3,134 models pinned to one upstream commit — which brought that share to 0.0%. | Active. The estimate's per-token rates are divided out of the same cost the fallback inflates, so the share is robust where the dollars are not; the gate stays. |
| Modelled-versus-host reconciliation | 0.12.0 found two defects behind a modelled figure that ran 49% above the host's own accounting. Most of it was a reporting artifact: the whole-corpus modelled total and the host total never covered the same sessions, because one host records token counters and no cost. The real residual, +8.12%, was a hand-written price table shadowing the registry with two drifted entries — $2,331.82 of inflation on the development corpus, more than the entire gap. Removing it moved the residual to −3.99%. | Closed as far as the evidence supports. The remainder is recorded, not corrected: −2.89% is a host billing several per-token rates under one model identifier, which a single list price cannot represent; −0.90% is one-hour cache writes, which under-price and therefore point the wrong way; −0.20% is an unpriced floating alias. Agreement with one host's accounting on one corpus does not establish the modelled figure is correct. |
| Laconic Observe | Released content-free receipts, local audit, compatibility reporting, and explicit install/remove/status/report commands for Claude Code and OMP. It never transforms agent-visible results. | Retain as supporting diagnostics and adapter evidence. It is not the primary product and does not satisfy the runtime beta gate. |
| K1 Stage A | A body-free metadata screen admitted 1,063 sessions across 61 project lineages and three providers. | Completed feasibility evidence. Its `proceed_to_stage_b_request` result was not product or spend authorization. |
| K1 Stage B | Produced the frozen manifest and eligibility machinery used by Stage C. | Completed research infrastructure. Preserve it; do not treat it as active product work. |
| K1 Stage C | The final source-mapped replacement cohort selected 24 sessions across 4 self-owned lineages after excluding 10 Retailogists sessions and 3 missing or ambiguous model mappings. All 11 Codex and 13 OMP baselines produced zero replay-engine turns, actions, and observations because the parser accepts Claude-shaped assistant/user tool-use records. Execution stopped before replay client construction. No provider prompt, replay artifact, external annotation, or modeled spend resulted. | Terminally incomplete historical replay path. Do not retry, weaken the gate, infer normalized records, or spend provider budget under its archived plan. Provider-specific normalization requires a separate future research design and explicit authorization. |
| K1 hybrid remeasurement | Available evidence could not support its intended paired claim. | Closed as a product prerequisite. Reopen only under a new research question and evidence contract. |
| External archive manifest | Proposed external-data collection but did not establish an available, authorized source. | Closed. External annotated data is not required for runtime delivery. |
| Prospective snapshot capture | Defined supporting capture infrastructure but could not unblock a runtime that did not exist. | Frozen. A shipped runtime may later generate prospective receipts under its own privacy and qualification contract. |
| Residency management | Decision accounting exists, but no live host applies history rewriting. | Deferred. Admit only after runtime evidence and a host surface justify the cache and correctness risk. |
| Action compression | The core action codec exists, but live edit rewriting has a larger correctness boundary than observation transformation. | Deferred beyond the observation-only beta. |

## Current authorization

The approved product tranche is:

1. align public product authority;
2. build a canonical session runtime and recovery protocol;
3. integrate that runtime into OMP;
4. ~~qualify it through at least 10 completed real OMP sessions across 3 repositories and at least 100 eligible observations~~ — completed, see `docs/runtime-beta-report.md`;
5. ~~prepare the bounded `v0.9.0` beta only when every safety criterion passes~~ — completed; `v0.9.0` is published, with `v0.9.1` correcting a cold-start defect;
6. ~~measure locally, read-only, where model spend goes and what the codec did in those same sessions~~ — completed as a single-arm composition report containing no savings figure.

7. ~~extend the codec to a second host once the protocol had survived OMP dogfood~~ — completed; the Claude Code adapter shipped in `v0.11.0`, and `laconic setup` now detects and installs per host.
8. ~~resolve model prices from a refreshable source rather than a hand-maintained table~~ — completed in `v0.12.0`, which also reconciled the modelled figure against the host's own accounting.

Still open: earning a general savings claim, or continuing to decline to make one. That requires a real comparison arm, which this corpus does not contain and which M20-v1 and M20-v2 are the only attempts to buy. MCP, action rewriting, and history compaction stay deferred until runtime evidence justifies them.

Item 6 is composition measurement, not savings measurement: every readable session ran with the codec enabled, so the corpus has no counterfactual. M20-v1 separately ran one frozen controlled comparison and ended incomplete with 0 valid cells and null dispersion, correlations, effect, and confirmatory sample size. M20-v2 corrected the operational validity mechanics and then executed once, completing with 24 of 24 valid cells and $0.9975374 of pooled gateway spend. It publishes dispersion, cross-arm cost correlations, mechanism and completion counters, and a confirmatory requirement of 15 tasks at two repetitions. Its arm means and paired effect remain private, so it establishes feasibility and variance, not savings, equivalence, direction, or superiority. Running it, any confirmatory cohort, or any other provider-backed comparison requires fresh explicit owner authorization. The current authorization does not include provider replay spend, external data collection, Claude Code integration, MCP, action rewriting, history compaction, hosted services, or universal savings claims.

## Claims that remain valid

- The measured corpus was dominated by tool-result and tool-argument traffic.
- The existing codec can deterministically reduce selected observation representations while keeping omitted content in a recoverable ledger.
- The committed fixture exercises the replay and gate machinery and reports its own bounded results.
- Observe records content-free local diagnostics without changing what an agent sees.

## Claims that remain unproven

- General token or monetary savings in live sessions.
- Prompt-cache savings from shorter visible results.
- Behavioral equivalence under real runtime use.
- Net benefit after induced expansions or additional agent work.
- Generalization across users, repositories, clients, models, and workloads.

The runtime beta may report observed raw and visible character counts for its own sessions. It must not rename those counts as token, cost, cache, or behavior improvement.
