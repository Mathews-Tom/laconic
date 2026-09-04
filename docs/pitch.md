# Laconic: Your Agent Is Paying Rent on 200,000 Tokens

*A practitioner's pitch for compressing what a coding agent carries, not what it says.*

---

## The one-sentence version

In a real coding-agent session, 88% of the context has no human on either end and 60% of the bill is spent re-reading the same tool output turn after turn — Laconic re-encodes that traffic so the agent carries less, and renders prose for you on demand.

---

## The problem, measured

We instrumented 179 real agent session transcripts: 19,818 assistant turns, 10,606 tool results, $2,134 of modelled spend. Here is where the context actually comes from.

| Channel | Share of context | Who reads it |
|---|---:|---|
| Tool results — files read, commands run, searches | **63.24%** | The model |
| Tool arguments — patches, writes, commands | **24.98%** | The model |
| Assistant prose — explanations | 6.47% | You, sometimes |
| Your prompts | 5.32% | The model |

And here is the bill:

| Component | Share of spend |
|---|---:|
| **Cache reads — carrying the resident context, every turn** | **60.3%** |
| Cache writes | 26.7% |
| All output tokens combined | 11.3% |
| Uncached input | 1.7% |

The mean resident prefix is **205,842 tokens per turn**. The mean turn emits 750. Your agent spends most of its money not on thinking or talking, but on remembering — re-ingesting a context window that grew every time it opened a file.

**Tool-result residency is ~38.1% of total spend.** That is the number worth attacking.

---

## The number that killed our first version

Laconic v1 compressed the model's explanatory prose. It worked: 44.6% fewer output tokens, carefully measured.

Then we measured prose against real sessions:

- Human-facing prose: **2.30% of total spend**
- 44% compression of it: **1.01% of your bill**
- Deleting every word of prose: **2.30%**
- Turns that emit no prose at all: **80.6%**
- Median prose per turn: **0 characters**

The 44.6% was a correct measurement of the wrong channel. We published the correction rather than the headline, and rebuilt around what the data said.

---

## What the core can do and the first beta will ship

Version 0.8.0 contains the deterministic codec and recovery ledger but no live codec integration. The first product integration is a planned opt-in OMP extension backed by a session-owned Python engine.

**Scope what enters the context.** A `Read` averages 6,147 characters, and the distribution is brutal: the largest 7% of reads carry 38.7% of all read volume; the top 10% carry 47.3%. For supported successful textual results, the runtime will return a smaller recovery-bearing envelope only when the complete replacement is shorter than the raw result:

```
<omp-session-id>/F3 src/auth/tokens.py  1,284 lines  sha 4a9c21e8
  outline: TokenError:12  decode_token:31-58  check_token:61-94  refresh:97-140  [+9 more]
  span 61-94:
    def check_token(token_exp: float, now: float | None = None) -> bool:
        ...
```

**Keep exact recovery local.** The beta stores the raw result in a private session ledger before emitting an envelope. A namespaced reference expands the full result or a line span on demand, including after resume or full fork.

**Defer history rewriting.** Residency accounting exists, but rewriting an existing cached prefix can cost more than it saves and requires host control the first beta does not assume. Applied compaction remains future work.

**Defer action rewriting.** Tool arguments are 25% of measured context volume, but live edit transformation carries a larger correctness boundary. The first beta changes observations only.

**Keep rendering out of band.** Existing deterministic rendering remains a supporting human view. Runtime expansion itself returns exact ledger content and does not invoke a model.

---

## What we are not claiming

**Not a general savings result.** The beta may report observed raw and visible character counts for its own sessions. General token, cost, cache, and behavior claims require representative paired evidence, model-specific accounting, induced-work measurement, and behavior evaluation.

**Not released as a runtime yet.** The current package is version 0.8.0. The planned OMP beta is gated by exact recovery, fail-open behavior, a 250 ms deadline, private local storage, operator control, correct packaging, and real OMP use. The committed fixture's 8.53% K1 result validates the research gate machinery but no longer blocks this bounded product gate.

**Not the biggest lever available.** Model and scaffold choice spans >100× in cost at comparable accuracy on public leaderboards. Laconic is orthogonal to it and will not pretend otherwise.

**Not free.** The runtime replaces a result only when the complete model-visible envelope is strictly smaller than the raw input. Low aggregate reduction is reported rather than hidden or repaired by changing a threshold after results are visible.

**Not a dedup tool.** We checked the obvious hypothesis first and it failed: byte-identical re-reads are 0.5% of read volume, duplicate Bash lines 4.2%. Agents don't repeat themselves — they over-fetch once and then carry it forever. The lever is residency, not redundancy.

---

## The part that makes this research

Across prompt compression, output brevity, constrained decoding, notation design, and explainable AI, **nobody has measured whether a developer reading compressed model output catches the same bugs as one reading prose.**

That absence matters, because the adjacent literature is not reassuring. Explanations increase acceptance of AI output independent of correctness (arXiv:2006.14779). They reduce over-reliance only when they genuinely lower the cost of verifying the answer (arXiv:2212.06823). Fluent text produces an illusion of understanding (arXiv:2102.02437). Meanwhile, verification is already a top-three time cost in AI-assisted programming (arXiv:2210.14306), and a pre-registered RCT found experienced developers were 19% *slower* with AI tools while believing they were 20% faster (arXiv:2507.09089).

So we pre-registered the experiment nobody has run:

> **K3 — Given the rendered view versus the raw trace, does a developer catch the same bugs, in the same time, with the same confidence?** Within 5pp passes. Worse by more than 10pp and we stop and publish the negative result.

Compression that makes an engineer feel informed while making them worse at catching bugs is a bad trade at any token price. We would rather find that out and say so.

---

## Also true, and worth stealing

Three findings from v1 survive the pivot and are useful whatever you are building:

**1. The injection channel is a first-order variable.** The same directive, same content, produced 2.1% to 44.6% effect depending only on how it reached the model — project file, appended system prompt, user-message XML, slash command, API system block. Nobody treats this as an experimental variable. They should. (Caveat: per-channel surrounding context differed, and *Control Illusion*, arXiv:2502.15851, shows system/user separation is a less reliable hierarchy than assumed.)

**2. Opus 4.6's minimum cacheable prefix is ~4,150 tokens, not the documented 1,024.** Below it, caching fails silently — no error, `cache_creation_input_tokens` reports 0, and you pay full input price forever. Found by binary search: 4,096 no, 4,150 yes.

**3. The cache is prefix-matched, not per-block.** Appending a rules block to an already-cached prefix writes only the delta — we measured a 0.10× write ratio. It also means rewriting history is expensive, which is why §3 does arithmetic before compacting.

---

## Honest limitations

**One corpus, one operator.** 179 transcripts of agentic work on one machine, with terse-response conventions already in force. That understates the prose channel and probably represents the observation channel well. More corpora are needed and welcome.

**Outline extraction is language-specific.** Where no parser exists, Laconic degrades to head/tail span scoping rather than failing.

**The main way a broad savings claim fails is compensating work.** If scoping one read causes three more reads or repeated expansions, the reduction can evaporate. Runtime receipts report those actions locally; representative K1 remains the separate research gate for a general net-economics claim.

**Vendors will likely build this.** Native context compaction is an obvious roadmap item. If it ships, the codec's value transfers to whoever measured it properly — and K3 is the part that does not get built by default.

---

## Where to go deeper

| Document | What's in it |
|---|---|
| `docs/grounding.md` | Product boundary, invariants, runtime gate, and drift checks |
| `docs/research-disposition.md` | What prior research established, failed to establish, and no longer blocks |
| `docs/overview.md` | Full what/why/how, measurements, positioning, and separated gates |
| `docs/system-design.md` | OMP-first runtime architecture, recovery, protocol boundaries, and supporting components |
| `scripts/measure_session_composition.py` | Reproducible source for the channel-composition figures |

---

*Built by Tom Mathews. The first version's headline number was right about its benchmark and wrong about the world; this document exists because we measured the world instead. Run the script on your own sessions — if your numbers disagree with ours, we want the transcript.*
