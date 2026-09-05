# Laconic: System Overview

## 1. What Laconic Is

Laconic is a **private, local runtime codec for existing coding agents**. It changes the representation used for eligible tool observations so the agent carries less through later turns, while preserving exact on-demand access to omitted content.

The insight is not that models talk too much. It is that **almost nothing in an agent's context window is written for a human, and almost none of it is written efficiently**.

Measured across 19,818 real assistant turns from 179 agent session transcripts:

| Channel | Share of context volume | Who reads it |
|---|---:|---|
| **Tool results** (file reads, command output, search hits) | **63.24%** | The model only |
| **Tool-use arguments** (patches, commands, file writes) | **24.98%** | The model only |
| Assistant prose (explanations) | 6.47% | A human, sometimes |
| Human prompts | 5.32% | The model only |

88.22% of the traffic in a coding agent has no human on either end. It is machine-to-machine communication that happens to be shipped as unstructured text, re-transmitted on every turn, at full price.

Laconic operates on that traffic:

- **Observation codec** — tool results enter the context as typed, span-scoped, handle-addressed records instead of raw dumps.
- **Action codec** — tool arguments are emitted as anchored deltas against those handles instead of restated content.
- **Handle ledger** — a content-addressed store that lets an out-of-focus observation collapse to a reference and be expanded again on demand.
- **Renderer** — turns the compact trace back into prose for a human, on demand, in a side channel.
- **Fidelity harness** — measures whether the agent takes the same actions, and whether a human catches the same bugs, under the codec.

The codec is lossy in presentation and **lossless in reach**: every elision is addressable and expandable. That invariant is what makes the compression safe, and it is the thing the harness exists to verify.

**Current status:** version 0.8.0 releases the codec, ledger, evaluation, rendering, and Observe diagnostic surfaces but no live integration. The repository's unreleased candidate adds the opt-in OMP extension, session-owned Python engine, exact namespaced recovery, fail-open result interception, and operator controls. M18 real-OMP qualification and human sign-off still block release. Claude Code follows after OMP dogfood; action rewriting, history compaction, and MCP remain deferred.

### What Laconic is NOT

- **Not a model router.** It does not choose models. Model and scaffold choice is a larger cost lever than anything here (>100× spread on public leaderboards); Laconic is orthogonal to it and says so.
- **Not a prompt optimizer.** It does not rewrite what you ask for.
- **Not a fine-tune.** Both the cloud model and the local renderer are used off the shelf.
- **Not an output-style prompt.** Laconic v1 was. It is not any more, and §9 explains why.
- **Not a replacement for your agent.** The first integration sits inside OMP at the tool-result boundary.

---

## 2. Why Laconic Exists

### 2.1 The measurement that defines the problem

All numbers below come from `scripts/measure_session_composition.py`, run over 179 real agent session transcripts (19,818 assistant turns, 10,606 tool results, $2,134.27 of modelled spend). Reproduce with:

```bash
uv run python scripts/measure_session_composition.py
```

**Cost decomposition:**

| Component | Share of spend |
|---|---:|
| Cache reads (re-ingesting the resident context every turn) | **60.3%** |
| Cache writes | 26.7% |
| Output tokens (all of them: prose, patches, commands) | 11.3% |
| Uncached input | 1.7% |

Output is 11.3% of the bill. Of the characters the model emits, 20.32% is human-facing prose. **Human-facing prose is therefore 2.30% of total spend.**

Cache reads are the bill. And cache reads are dominated by tool results sitting resident in the prefix, turn after turn:

> **Tool-result residency ≈ 38.1% of total spend — 17× the entire prose channel.**

The mean resident prefix is **205,842 tokens per turn**. The mean assistant turn emits 750 output tokens, of which roughly 29 are prose. The context an agent drags behind it outweighs what it says by more than two orders of magnitude.

### 2.2 Prose is not where the waste is

| Statistic | Value |
|---|---:|
| Turns emitting zero human-facing prose | **80.6%** |
| Median prose per turn | **0 characters** |
| p90 prose per turn | 114 characters |
| Share of all prose in the top 1% of turns | 56.5% |

Compressing prose by 44% — Laconic v1's best measured result — saves **1.01% of a real session bill**. Deleting every word of prose saves 2.30%. That is the ceiling, and it is not a product.

### 2.3 The waste is residency and whale reads, not redundancy

We tested the obvious hypothesis first, and it failed:

| Naive lever | Measured headroom | Verdict |
|---|---:|---|
| Deduplicate byte-identical re-reads | **0.5%** of Read volume (23 of 1,892 calls) | Not a lever |
| Deduplicate repeated Bash output lines | **4.2%** of Bash volume | Marginal |

Agents rarely re-read the identical bytes. What they do is pull in far more than they need, once, and then pay for it on every subsequent turn.

**Read is the single largest line item** — 1,892 calls, 11,631,510 characters, mean 6,147 per call — and it is whale-distributed:

| Read size | Share of calls | Share of Read volume |
|---|---:|---:|
| > 2,000 chars | 62.4% | 94.0% |
| > 5,000 chars | 31.4% | 77.2% |
| > 20,000 chars | **7.0%** | **38.7%** |

The top 10% of reads carry 47.3% of all Read volume; the largest single read is 75,882 characters. Bash is second: 5,708 calls, 6,309,732 characters, mean 1,105.

So the two real levers are:

1. **First-emission scoping** — a 40,000-character file read to answer a question about one function should not put 40,000 characters into the context permanently. This is the observation-only beta's target.
2. **Later residency management** — observations that are no longer in focus could collapse to a handle and stop being re-billed on every turn, but rewriting history is deferred until runtime evidence and host control justify the cache and correctness risk.
Redundancy elimination is a cheap add-on, not the headline. We say so because we measured it.

### 2.4 Why now

1. **Agentic sessions became the dominant workload.** In 2024 the shape of an AI coding interaction was a chat turn. In 2026 it is a 200,000-token tool loop. The cost structure inverted; the tooling did not follow.
2. **Prompt caching made residency the meter.** Once a prefix is cached, you stop paying to *write* context and start paying, forever, to *carry* it. That changes the optimisation target from "emit fewer tokens" to "stay small".
3. **Tool boundaries became programmable.** OMP result middleware provides a direct interception point where built-in tool observations enter model context. Laconic starts there rather than relying on an MCP-only gateway that misses built-in tools.

---

## 3. How Laconic Works

### 3.1 The handle ledger

Every observation selected by the runtime is registered in a session-scoped ledger and assigned a short, stable internal handle:

```
F3  src/auth/tokens.py    sha 4a9c21e8   1,284 lines   read 47-92
B7  pytest -q             exit 1         318 lines     err-salient
S2  grep "check_token" src/              14 hits
```

Internal handles are the ledger vocabulary. Model-visible runtime references also identify the source OMP session, for example `<omp-session-id>/F3:47-92`, so resume and full-fork recovery cannot collide. `laconic_expand` resolves a full reference or span from the owning ledger.

### 3.2 Observation codec (attacks 63.24% of context, 38.1% of spend)

**Reads** are encoded as a structural outline plus the requested span:

```
<omp-session-id>/F3 src/auth/tokens.py  1,284 lines  sha 4a9c21e8
  outline: TokenError:12  decode_token:31-58  check_token:61-94  refresh:97-140  [+9 more]
  span 61-94:
    <verbatim source>
```

A later read of an unchanged file can resolve to `<omp-session-id>/F3 unchanged`. A span already shown can resolve to `<omp-session-id>/F3:61-94 (shown above)`. A read whose outline suffices need not materialise the body in model-visible context.

**Bash** is encoded error-salient: exit status, stderr, the head and tail of stdout, and a summary of the stable middle, with duplicate lines counted rather than repeated. Known-shaped output (test runners, installers, build logs) gets a structured summary — pass/fail counts and failing entries only. Every elided region keeps an address.

**Search results** are interned against a path prefix table and returned as a table rather than repeated lines of context.

### 3.3 Residency management (deferred beyond the first beta)

Append-only encoding is the default: new observations are compact, history is never rewritten, and the prompt cache is preserved perfectly.

Compaction — rewriting out-of-focus observations in the existing prefix down to handles — is **opt-in and gated on arithmetic**, because rewriting history invalidates the cached prefix. Writing a cache costs 1.25× input price; reading one costs 0.10×. So compaction that shrinks the prefix by Δ tokens down to `P_new` pays for itself only after:

```
N_turns  =  12.5 × P_new / Δ
```

| P_new | Δ | Pays back after |
|---:|---:|---:|
| 40,000 | 60,000 | 8.3 turns |
| 60,000 | 40,000 | 18.8 turns |
| 80,000 | 20,000 | 50.0 turns |

Laconic computes this before compacting and declines when the session will not last that long. A tool that silently busts your cache to save context is a tool that raises your bill; the honest version does the division first.

### 3.4 Action codec (implemented core, deferred runtime)

Tool arguments are the second-largest channel, and they are dominated by content restatement:

| Tool | Arg volume | Calls | Mean |
|---|---:|---:|---:|
| Write | 1,991,201 | 354 | 5,624 |
| Edit | 1,755,266 | 1,218 | 1,441 |
| Bash | 1,736,926 | 5,708 | 304 |

Edits are emitted as deltas anchored to ledger handles and symbol names rather than to restated file content, and commands are interned against the ledger. An edit to `check_token` becomes an anchored patch against `F3`, not a re-transmission of the region around it.

### 3.5 Renderer (the human surface)

The compact trace is genuinely unreadable — which is precisely why rendering it is worth doing, and why v1's hydration layer was solving a problem it had created only cosmetically.

The renderer resolves a span of the trace into prose on demand: deterministic templates for anything structural (what was read, what changed, what failed), a local model only where genuinely generative text helps. It runs out of band, never blocks the agent, and never feeds back into the model's context.

Rendering is a **view**, not a wire format. The human reads it because they asked to, not because the model paid to produce it.

### 3.6 Fidelity harness

The observation-only beta is releasable when runtime evidence shows exact recovery, fail-open behavior, bounded latency, correct result mutation, private storage, operator control, and successful real OMP use.

Broader research claims require separate measurements:

- **Action equivalence.** Does the agent take the same next action given the compressed observation as given the raw one?
- **Human round-trip.** Does a developer catch the same bugs, in the same time, with the same confidence, when reading a rendered view instead of a raw trace?

Neither claim is inferred from a safe runtime or from character reduction.

---

## 4. Design Constraints

Three tensions are structural, and the design is shaped around them rather than around wishful thinking.

**Recoverability beats ratio.** Any elision the agent cannot reverse is a correctness bug waiting for the one task that needed those bytes. Every compaction is addressable through the ledger. We will trade ratio for reach every time.

**Caching punishes rewriting.** The prompt cache is prefix-matched, so shrinking history costs a full cache write. §3.3's break-even formula is a hard gate, not a guideline.

**Format constraints have a documented cost, and we sit next to it.** Prompt-level format instructions are the dominant source of format-induced accuracy loss — larger than constrained decoding's sampling bias (*The Format Tax*, arXiv:2604.03616) — and the penalty scales inversely with a model's spare capacity (*Capacity, Not Format*, arXiv:2606.09410; *Let Me Speak Freely?*, arXiv:2408.02442). Laconic's mitigation is architectural: **the codec constrains the tool-result boundary, not the model's generation.** Observations are re-encoded after a tool returns and before the result reaches the model; the model is never asked to write in a schema while reasoning.

---

## 5. Positioning and Prior Work

Honest placement, because the v1 documents got this wrong and the correction matters.

| Work | What it does | Relationship to Laconic |
|---|---|---|
| **Caveman** (JuliusBrussee, ~93k stars) | Output-style compression across 30+ agents, six levels, MCP tool-description middleware, memory-file compressor, subagents, a Gemma fine-tune, a full terminal agent | **The incumbent for output-style compression, and it is good.** Its `HONEST-NUMBERS.md` independently documents that session-level savings land at 14–21% on output-heavy workloads and go negative on terse ones. Laconic v1 competed with this and lost. Laconic v2 operates on a different channel and is complementary |
| **TRIM** (arXiv:2412.07682) | LLM omits inferable words; a trained smaller model reconstructs them. 19.4% token savings on GPT-4o | **Prior art for v1's exact compress-then-hydrate architecture**, published December 2024. Cited here because v1's docs claimed the pattern was unaddressed. v2 does not use it |
| **LLMLingua family** (2310.05736, 2310.06839, 2403.12968) | Prompt/context compression for documents and RAG payloads, up to 20× | Nearest technical relative. Targets documents and retrieval contexts; **no work in this literature targets agent tool results**. That absence is the gap Laconic v2 occupies |
| **AgentPrune** (2410.02506), AgentTaxo | Multi-agent token cost is dominated by structural redundancy; graph-level pruning recovers 28–86% | Same diagnosis one layer up — cost lives in what gets re-transmitted, not in how tersely each message is written. Complementary; validates the residency framing |
| **TOON** and format-efficiency work | ~42.6% token reduction vs JSON at matched retrieval accuracy | A drop-in encoding win for structured payloads, with no prompting risk. Laconic should adopt it inside the codec rather than compete with it |
| **Chain of Draft** (2502.18600), **TALE** (2412.18547), **Sketch-of-Thought** (2503.05179) | Compress the reasoning scratchpad, 7.6–84% reductions | Frequently mistaken for validation of output compression. They compress a channel the user never sees. Their numbers do not transfer, and we do not cite them as support |
| **MCP**, **A2A** | Standardize discovery, auth, lifecycle, transport | Neither specifies payload content style, but an MCP-only gateway misses built-in tool results. Laconic defers MCP until the direct OMP runtime is proven |
| **Chain of Thought Monitorability** (2507.11473) | Legible-English reasoning is a fragile safety asset | A boundary we guard explicitly: the first runtime compresses only tool observations, never the model's reasoning trace. §8 |

---

## 6. Evidence and Gates

### 6.1 What is measured

| Claim | Value | Source |
|---|---|---|
| Channel decomposition of agent context | 63.24 / 24.98 / 6.47 / 5.32 | 19,818 turns, this repo |
| Tool-result residency share of spend | ~38.1% | derived, §2.1 |
| Human-facing prose share of spend | 2.30% | 19,818 turns |
| Read volume concentration | top 7% of reads = 38.7% of volume | 1,892 reads |
| Identical-re-read redundancy | 0.5% of Read volume | 1,892 reads |
| Compaction break-even | `12.5 × P_new / Δ` turns | cache pricing arithmetic |
| Prefix-matched cache write ratio for an appended rules block | 0.10× | v1 API-direct experiment |
| Opus 4.6 minimum cacheable prefix | ~4,150 tokens | v1 binary search |
| Injection channel spread for a behavioural directive | 2.1% → 44.6% | v1, Runs 1–8 |

The last three are v1 results that survive the pivot. The injection-channel finding is being written up separately; it generalizes beyond compression to any behavioural directive, with the caveat that per-channel context differed (see *Control Illusion*, arXiv:2502.15851) and that model-version-scoped cache thresholds expire.

### 6.2 What is assumed and not yet measured

Stated plainly, because v1's founding assumption went untested for six experimental runs:

- That the channel composition of the *resident prefix* matches the channel composition of total volume. Approximation; future runtime receipts can measure the visible observation path directly.
- That span-scoped reads do not cause compensating follow-up work that erases the reduction. Representative K1 is the research gate for a general net-economics claim, not the opt-in beta's safety gate.
- That the corpus (one operator, agentic workloads, terse-response conventions already in force) generalizes. Prose share is understated here relative to chat-style use; observation share has not been established as universal.

### 6.3 Runtime beta gate

The OMP beta requires at least 10 completed Laconic-enabled sessions across 3 canonical Git repositories and at least 100 eligible observations. Every emitted reference must recover exactly, errors and unsupported content must pass through, every emitted envelope must be strictly smaller, failure and lifecycle paths must be exercised, and latency plus decision counts must be reported. The beta has no minimum aggregate savings percentage.

### 6.4 Research claim gates

| # | Gate | Threshold | Kill condition |
|---|---|---|---|
| K1 | Session-level **net** cost reduction on representative replay, including follow-up work | ≥ 25% | < 15% means a general economics claim is not justified |
| K2 | Action equivalence, compressed vs raw observation | ≥ 95% | < 90% means the codec is lossy where measured |
| K3 | Human bug-catch rate, rendered view vs raw trace | within 5pp | worse by > 10pp means compression harms verification |
| K4 | Codec overhead in added input tokens per turn | < 500 | above means structural overhead is excessive |
| K5 | Exact-match reasoning benchmark, codec on vs off | within 2pp | beyond means the tested representation changes measured accuracy |

The committed fixture validates this machinery and reports its own bounded results. It is not representative product-economics evidence. Its 8.53% K1 result no longer blocks an opt-in runtime beta, and passing the beta gate does not satisfy these research gates.

---

## 7. Research Contribution

Two contributions, both grounded in confirmed absences in the literature:

**C1 — A compression scheme for the agent observation channel.** Published context-compression methods target documents, retrieval payloads, or reasoning traces rather than built-in coding-agent tool results. Laconic supplies a codec, recovery ledger, residency arithmetic, and fixture-backed replay harness. Representative multi-client evidence remains incomplete, so novelty and general benefit claims stay bounded accordingly.

**C2 — The first human-outcome measurement of compressed AI output.** No published work — across prompt compression, output brevity, constrained decoding, notation design, or XAI — measures whether a developer reading compressed model output catches the same bugs as one reading prose. The adjacent HCI literature makes this urgent rather than optional: explanations increase acceptance of AI output independent of correctness (arXiv:2006.14779), and reduce over-reliance only when they genuinely lower verification cost (arXiv:2212.06823). K3 is that experiment.

A longer-term third target: a **bidirectional notation with semantic round-trip fidelity**, which nobody has evaluated. The codec is the substrate that makes it approachable, and the sequencing is deliberate — the observation channel has no adoption tax, because no human has to learn a notation they never write. Every historical attempt at a designed human-facing register (Basic English, Attempto ACE, FIPA-ACL, SynthLang) died on exactly that tax. We earn the human-facing half only if K3 says compression is safe.

---

## 8. Risks

### 8.1 Technical

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Span scoping induces compensating re-reads, erasing the reduction | **High** | High | Runtime receipts report expansions and character outcomes; representative K1 remains the later net-economics claim gate |
| Compaction busts the prompt cache and raises the bill | Medium | High | Applied compaction is deferred beyond the first beta |
| Elision removes bytes the agent needed | Medium | High | Exact full/span recovery, write-before-emit, fail-open handling, and real OMP qualification |
| Outline extraction is language-specific and brittle | High | Medium | Runtime transforms only allowlisted tools and preserves the original on any encoding failure |
| Format tax at the tool boundary | Low | High | Codec never constrains generation while the model reasons; K5 remains a separate research measure |
| Renderer hallucinates during expansion | Medium | Medium | Runtime expansion reads exact ledger content; optional narration remains out of band |
| OMP changes its extension or result middleware contracts | Medium | High | Pin and test the adapter contract independently; preserve original results on any mismatch |

### 8.2 Human-factor risk

The renderer is where this project could quietly do harm. Fluent explanations increase acceptance regardless of correctness (arXiv:2006.14779), produce an illusion of explanatory depth (arXiv:2102.02437), and can function as placebic content (Langer et al., 1978). A generated paraphrase of a compact trace is structurally that artifact.

Mitigations are design-level, not disclaimers: structural facts are rendered deterministically and are therefore not hallucinable; generated text is visually distinguished from resolved facts; every rendered claim links to the handle it came from. And K3 measures the outcome rather than asserting it.

### 8.3 Scope risk

Classical agent communication languages (KQML, FIPA-ACL) failed by specifying semantics and ontologies while under-specifying transport and tooling. Laconic must stay a thin layer at an existing boundary: a codec, a ledger, a renderer, and a harness. If it starts to require an ontology, it has become the thing that failed.

### 8.4 Market

| Risk | Likelihood | Impact | Note |
|---|---|---|---|
| Vendors build observation compaction natively | **High** | High | Context editing and compaction features are an obvious roadmap item. Laconic's durable value is then the measurement methodology and K3, not the codec |
| Model context windows grow enough that residency stops mattering | Medium | Medium | Cache-read pricing scales with residency regardless of window size; the meter does not care how large the window is |
| Agents move to server-side state, removing the client-side boundary | Low | High | The codec would move with the boundary; the ledger design is transport-agnostic |

---

## 9. What Changed From v1, and Why

Laconic v1 injected a 23-line telegraphic output rule into the model's system prompt to compress human-facing prose, with a local model re-expanding it. It was carefully measured: 44.6% output-token savings on API-direct prose-heavy tasks, 29.4% in interactive Claude Code, a compliance-bias study at N=30, and a multi-turn adherence study.

It rested on one assumption that was never stated and therefore never tested: **that human-facing prose is a large share of coding-assistant token spend.** Measured against real agentic sessions, it is 2.30%. The v1 headline translated to roughly 1.0% of a real bill, and 80.6% of turns produce no prose to compress at all.

Three further findings forced the pivot:

1. **The output-style niche is occupied and well-served.** Caveman had already shipped the mechanism across 30+ agents and had already published the honest session-level numbers.
2. **The compress-then-hydrate architecture had prior art** in TRIM (arXiv:2412.07682, December 2024).
3. **The format-restriction literature placed v1's rigid prompt-enforced micro-schema in its documented harm-prone regime** — on lower-capacity models doing open-ended reasoning, which is exactly what v1 recommended shipping.

What survives: the cache-aware prompt construction, the injection-channel study, the measurement discipline, and the founding intuition that English is a poor protocol for talking to models. v2 keeps that intuition and points it at the 88% of traffic that no human reads — where there is 17× more to gain and no register for anyone to learn.

The pivot rests on a full internal review — the devil's-advocate case against v1, the literature sweep, and the competitive re-baseline. Its conclusions are summarized above and in §5; the review itself is kept as an internal working document.
