# Laconic Grounding Charter

## The contract

**Laconic is not a tool that promises to compress every coding-agent session. It is a private, evidence-first system that measures context economics and fidelity, then enables reversible tool-boundary compression only where the evidence says it is safe and worthwhile.**

Inputs are local coding-agent observations, actions, and session traces. Outputs are recoverable encoded representations, auditable cost/fidelity measurements, and an explicit decision: measure-only, safe to test, not economically justified, or insufficient evidence.

## Explicit non-goals

Laconic is not:

- a prompt-style or prose-compression product;
- a model router, generic agent orchestrator, project-state manager, or handoff system;
- a system that enables hooks or MCP merely because an encoder exists;
- a provenance, WORM-storage, or transcript-archive product;
- a mechanism for changing K1 thresholds, corpus criteria, or codec behavior after results are visible.

Hooks are the intended primary runtime surface. The CLI is the primary operator, measurement, replay, inspection, and decision surface. MCP is a secondary runtime surface that generalizes a proven hook design.

## Invariants

1. **Recoverability:** every elision remains addressable and expandable from the ledger.
2. **Net, not gross:** a claimed saving includes induced follow-up work and cache effects.
3. **Fail open:** a runtime codec passes raw content through on an error or latency breach.
4. **Evidence before intervention:** a codec is not enabled for a surface without its predeclared cost and fidelity evidence.
5. **No post-result tuning:** thresholds, corpus rules, and codec settings freeze before paired evidence is visible.
6. **Human outcome is separate:** K3 is a participant study, never inferred from renderer quality or a simulated dry run.

## Current state, as measured

- Package version: `0.8.0` (`pyproject.toml`).
- Core codec, CLI, replay/gate harness, renderer, and K3 dry-run harness are released.
- Laconic Observe (`laconic.observe`, `laconic observe install/remove/status/report`) is released: a local, content-free automatic measurement surface for Claude Code and OMP. It is not a codec transform and does not touch K1 status; no hook is installed automatically, only via an explicit operator command.
- `uv run laconic gates --corpus tests/corpus --format json` reports K1 net savings of **8.53%**; the pre-registered kill threshold is **15%**.
- The same gate run reports K2 action equivalence **100%**, K4 overhead **26.8 tokens**, K5 difference **0.0pp**, and K3 as manual/not evaluated.
- Hook/MCP *codec* deployment is blocked. The committed corpus validates the gate pipeline but is not a real-world savings benchmark.

## Stated limitations

- No representative paired codec-on corpus, privacy/consent protocol, lineage split, or authorized counterfactual collection method exists; representative K1 is not currently feasible.
- No K3 participant protocol, trace-material approval, recruitment plan, consent, or data-handling plan exists.
- Prospective capture V1 is local and provisional. It does not authorize real session capture, provider activity, remote sealing, or K1/M4E progression.

## Drift history

| Drift | Symptom | Corrective boundary |
| --- | --- | --- |
| V1 prose compression | A 44.6% output headline looked like a coding-agent cost product. | Real-session composition found prose was only 2.30% of spend; V1 is retired. |
| Mechanism-first V2 work | Encoder/replay/provenance work continued after K1 fired. | K1 is a deployment gate, not a metric to optimize around. |
| Provenance as product | Archive, provider-replay, WORM, and capture work began to dominate discussion. | Provenance is supporting evidence infrastructure; it cannot improve net codec economics. |
| Surface inversion | MCP and prospective infrastructure risked outranking the hook/CLI product path. | Hooks are primary conditional runtime; CLI is primary current surface; MCP is secondary. |

## Drift checks

Run these before proposing work:

```text
uv run laconic gates --corpus tests/corpus --format json
uv run laconic measure tests/corpus --expect tests/corpus/expected.json
git status --short
```

Reject or hold a proposal when it:

- resumes hooks or MCP while K1 is below its unblock threshold;
- adds provenance/capture infrastructure without changing a named evidence gap;
- claims real-session savings from the synthetic fixture corpus;
- treats K3 dry-run data as participant evidence;
- turns Laconic into project-state, handoff, routing, or orchestration software.
