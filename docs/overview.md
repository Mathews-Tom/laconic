# Laconic: System Overview

## 1. What Laconic Is

Laconic is a **context-loop codec for coding agents**. It changes the representation an agent uses to perceive its environment and to act on it, so the agent carries less weight through every turn of a session, and renders human-readable prose from that compact representation only when a human actually looks.

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

### What Laconic is NOT

- **Not a model router.** It does not choose models. Model and scaffold choice is a larger cost lever than anything here (>100× spread on public leaderboards); Laconic is orthogonal to it and says so.
- **Not a prompt optimizer.** It does not rewrite what you ask for.
- **Not a fine-tune.** Both the cloud model and the local renderer are used off the shelf.
- **Not an output-style prompt.** Laconic v1 was. It is not any more, and §9 explains why.
- **Not a replacement for your agent.** It sits under Claude Code (or any tool-calling client) at the tool boundary.

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

1. **Span scoping** — a 40,000-character file read to answer a question about one function should not put 40,000 characters into the context permanently. This is a first-emission saving that compounds into a residency saving forever after.
2. **Residency management** — observations that are no longer in focus should collapse to a handle and stop being re-billed on every turn, while remaining expandable.

Redundancy elimination is a cheap add-on, not the headline. We say so because we measured it.

### 2.4 Why now

1. **Agentic sessions became the dominant workload.** In 2024 the shape of an AI coding interaction was a chat turn. In 2026 it is a 200,000-token tool loop. The cost structure inverted; the tooling did not follow.
2. **Prompt caching made residency the meter.** Once a prefix is cached, you stop paying to *write* context and start paying, forever, to *carry* it. That changes the optimisation target from "emit fewer tokens" to "stay small".
3. **Tool boundaries became standardized.** MCP and Claude Code hooks give a clean interception point at exactly the layer where observations enter the context — and, notably, neither MCP nor A2A specifies anything about payload content. That layer is unclaimed.

---

## 3. How Laconic Works

### 3.1 The handle ledger

Every observation that enters the context is registered in a content-addressed ledger and assigned a short, stable, human-typeable handle:

```
F3  src/auth/tokens.py    sha 4a9c21e8   1,284 lines   read 47-92
B7  pytest -q             exit 1         318 lines     err-salient
S2  grep -rn "check_token" src/          14 hits
```

Handles are the vocabulary of the codec. The model refers to `F3:47-92` rather than restating content; the renderer resolves handles for a human; `laconic expand F3` recovers anything elided. The ledger is the mechanism that makes elision reversible, which is the invariant everything else depends on.

### 3.2 Observation codec (attacks 63.24% of context, 38.1% of spend)

**Reads** are encoded as a structural outline plus the requested span:

```
F3 src/auth/tokens.py  1,284 lines  sha 4a9c21e8
  outline: TokenError:12  decode_token:31-58  check_token:61-94  refresh:97-140  [+9 more]
  span 61-94:
    <verbatim source>
```

A later read of an unchanged file resolves to `F3 unchanged`. A read after an edit resolves to a delta against the stored version. A span already shown resolves to `F3:61-94 (shown above)`. A read whose outline suffices never materialises the body at all.

**Bash** is encoded error-salient: exit status, stderr, the head and tail of stdout, and a summary of the stable middle, with duplicate lines counted rather than repeated. Known-shaped output (test runners, installers, build logs) gets a structured summary — pass/fail counts and failing entries only. Every elided region keeps an address.

**Search results** are interned against a path prefix table and returned as a table rather than repeated lines of context.

### 3.3 Residency management

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

### 3.4 Action codec (attacks 24.98% of context)

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

The codec is only worth shipping if it is behaviourally invisible to the agent and honest to the human. Two measurements, run continuously:

- **Action equivalence.** Replay a real session's tool loop with and without the codec. Does the agent take the same next action given the compressed observation as given the raw one? Cheap, automatable, runs at scale over the existing transcript corpus.
- **Human round-trip.** Given the rendered view versus the raw trace, does a developer catch the same bugs, in the same time, with the same confidence?

The second one has never been run by anyone, on any output-compression system, in any published work. It is the reason this project is research and not just tooling.

---

## 4. Design Constraints

Three tensions are structural, and the design is shaped around them rather than around wishful thinking.

**Recoverability beats ratio.** Any elision the agent cannot reverse is a correctness bug waiting for the one task that needed those bytes. Every compaction is addressable through the ledger. We will trade ratio for reach every time.

**Caching punishes rewriting.** The prompt cache is prefix-matched, so shrinking history costs a full cache write. §3.3's break-even formula is a hard gate, not a guideline.

**Format constraints have a documented cost, and we sit next to it.** Prompt-level format instructions are the dominant source of format-induced accuracy loss — larger than constrained decoding's sampling bias (*The Format Tax*, arXiv:2604.03616) — and the penalty scales inversely with a model's spare capacity (*Capacity, Not Format*, arXiv:2606.09410; *Let Me Speak Freely?*, arXiv:2408.02442). Laconic's mitigation is architectural: **the codec constrains the tool boundary, not the model's generation.** Observations are re-encoded *before* they reach the model and *after* it emits them; the model is never asked to write in a schema while reasoning. That is the same "decouple reasoning from formatting" prescription the Format Tax paper arrives at, applied at the transport layer. Gate K5 (§6) verifies it holds on our stack rather than assuming it.

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
| **MCP**, **A2A** | Standardize discovery, auth, lifecycle, transport | Neither specifies payload content style. Laconic is a content-layer convention that rides on top; this is confirmed white space |
| **Chain of Thought Monitorability** (2507.11473) | Legible-English reasoning is a fragile safety asset | A boundary we guard explicitly: Laconic compresses the observation and action channels, never the model's reasoning trace. §8 |

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

- That the channel composition of the *resident prefix* matches the channel composition of total volume. Approximation; the replay harness will measure it directly.
- That span-scoped reads do not cause the agent to issue compensating follow-up reads that erase the saving. **This is the main way the design could fail**, and gate K1 measures net, not gross.
- That the corpus (one operator, agentic workloads, terse-response conventions already in force) generalizes. Prose share is understated here relative to chat-style use; observation share is likely representative of agentic use generally.

### 6.3 Pre-registered gates

| # | Gate | Threshold | Kill condition |
|---|---|---|---|
| K1 | Session-level **net** cost reduction on replayed real traces, including follow-up reads the codec induces | ≥ 25% | < 15% → complexity not justified |
| K2 | Action equivalence, compressed vs raw observation | ≥ 95% | < 90% → codec is lossy where it matters |
| K3 | Human bug-catch rate, rendered view vs raw trace | within 5pp | worse by > 10pp → compression harms verification; stop and publish the negative result |
| K4 | Codec overhead in added input tokens per turn | < 500 | above → Caveman's net-negative trap, reproduced |
| K5 | Exact-match reasoning benchmark, codec on vs off | within 2pp | beyond → format tax confirmed on our stack |

K3 is the one nobody has run. If it comes back negative, that is a publishable result and the project has done its job.

---

## 7. Research Contribution

Two contributions, both grounded in confirmed absences in the literature:

**C1 — The first compression scheme for the agent observation channel.** Every published context-compression method targets documents, retrieval payloads, or reasoning traces. None targets tool results in an agentic loop, despite that channel being 63% of context volume and ~38% of spend. Laconic supplies the codec, the residency arithmetic, and a replay-based evaluation over real session traces rather than synthetic benchmarks.

**C2 — The first human-outcome measurement of compressed AI output.** No published work — across prompt compression, output brevity, constrained decoding, notation design, or XAI — measures whether a developer reading compressed model output catches the same bugs as one reading prose. The adjacent HCI literature makes this urgent rather than optional: explanations increase acceptance of AI output independent of correctness (arXiv:2006.14779), and reduce over-reliance only when they genuinely lower verification cost (arXiv:2212.06823). K3 is that experiment.

A longer-term third target: a **bidirectional notation with semantic round-trip fidelity**, which nobody has evaluated. The codec is the substrate that makes it approachable, and the sequencing is deliberate — the observation channel has no adoption tax, because no human has to learn a notation they never write. Every historical attempt at a designed human-facing register (Basic English, Attempto ACE, FIPA-ACL, SynthLang) died on exactly that tax. We earn the human-facing half only if K3 says compression is safe.

---

## 8. Risks

### 8.1 Technical

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Span scoping induces compensating re-reads, erasing the saving | **High** | High | K1 measures net cost on replayed real traces. This is the primary failure mode |
| Compaction busts the prompt cache and raises the bill | Medium | High | Break-even formula computed before every compaction; append-only is the default |
| Elision removes bytes the agent needed | Medium | High | Recoverability invariant: every elision is handle-addressable and expandable. K2 measures action equivalence |
| Outline extraction is language-specific and brittle | High | Medium | Degrade to head/tail span scoping when no parser is available; never fail closed |
| Format tax at the tool boundary | Low | High | Codec never constrains generation while the model reasons. K5 verifies |
| Renderer hallucinates during expansion | Medium | Medium | Deterministic templates for structural facts; the local model only handles genuinely generative text; the raw trace is one keystroke away |
| Provider changes cache semantics or hook APIs | Medium | Medium | Break-even arithmetic is re-derived from published pricing; hook interaction sits behind an adapter |

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
