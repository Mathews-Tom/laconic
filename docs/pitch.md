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

## What Laconic does now

**Scopes what enters the context.** A `Read` averages 6,147 characters, and the distribution is brutal: the largest 7% of reads carry 38.7% of all read volume; the top 10% carry 47.3%. Laconic returns a structural outline plus the span you actually asked about, with everything else one expansion away:

```
F3 src/auth/tokens.py  1,284 lines  sha 4a9c21e8
  outline: TokenError:12  decode_token:31-58  check_token:61-94  refresh:97-140  [+9 more]
  span 61-94:
    def check_token(token_exp: float, now: float | None = None) -> bool:
        ...
```

**Stops re-billing what is no longer in focus.** Observations collapse to handles (`F3`, `B7`) and expand on demand. Because rewriting a cached prefix costs a full cache write, Laconic does the arithmetic before it acts — compaction that shrinks the prefix by Δ down to `P_new` pays back only after `12.5 × P_new / Δ` turns, and Laconic declines when your session will not last that long.

**Emits actions as deltas.** Tool arguments are 25% of context, dominated by content restatement — `Write` averages 5,624 characters per call, `Edit` 1,441. Edits become anchored patches against ledger handles instead of re-transmitted file regions.

**Renders prose when you want it.** The compact trace is genuinely unreadable, which is exactly why rendering it earns its place. Structural facts render deterministically — they cannot be hallucinated. Only genuinely generative text touches a model, and the raw trace is always one keystroke away.

---

## What we are not claiming

**Not a 44% saving.** We will publish the net session-level number the replay harness produces, including any follow-up reads the codec induces. If it comes in under 15%, we will say the codec is not worth its complexity.

**Not deployment-ready.** The executable K1 gate currently measures 8.53% net savings on the committed fixture corpus, below the 15% kill threshold. Hook-based deployment is the intended first runtime surface and MCP is secondary, but both remain blocked. The fixture validates the gate pipeline rather than real-world savings magnitude; a representative-corpus K1 decision is the only path that can reopen integration.

**Not the biggest lever available.** Model and scaffold choice spans >100× in cost at comparable accuracy on public leaderboards. Laconic is orthogonal to that and will not pretend otherwise.

**Not free.** A codec that adds more input tokens than it saves is a tax. Our gate is under 500 added tokens per turn, and we test for it.

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

**The main way this fails is compensating reads.** If scoping a read just makes the agent read three more times, the saving evaporates. That is why K1 measures net cost on replayed real traces, not gross bytes removed.

**Vendors will likely build this.** Native context compaction is an obvious roadmap item. If it ships, the codec's value transfers to whoever measured it properly — and K3 is the part that does not get built by default.

---

## Where to go deeper

| Document | What's in it |
|---|---|
| `docs/overview.md` | Full what/why/how, measurements, positioning, prior work, gates |
| `docs/system-design.md` | Architecture, handle ledger, codecs, replay harness, data model |
| `scripts/measure_session_composition.py` | Every number in this document, reproducible on your own sessions |

---

*Built by Tom Mathews. The first version's headline number was right about its benchmark and wrong about the world; this document exists because we measured the world instead. Run the script on your own sessions — if your numbers disagree with ours, we want the transcript.*
