# Laconic – Compress what a coding agent carries, not what it says.

> **Status: runtime product in development, package v0.8.0.** The deterministic codec, recovery ledger, replay/gate harness, renderer, and Observe diagnostics are released. No live codec integration ships in 0.8.0. The first integration is a planned opt-in OMP extension backed by a session-owned Python engine and gated by exact recovery, fail-open behavior, bounded latency, operator control, packaging, and real OMP use.

Laconic is a private, local runtime codec for existing coding agents. It reduces eligible model-visible tool observations while preserving exact, on-demand access to omitted content. It integrates with agents rather than replacing them.

> **[`docs/grounding.md`](docs/grounding.md) is the authoritative statement of what Laconic is, what it deliberately is not, and how to detect strategy drift.** Read it before proposing or reviewing changes.

## Install

```bash
uv tool install laconic
laconic --help
```

## Documentation

| Document | What's in it |
| --- | --- |
| [`docs/grounding.md`](docs/grounding.md) | Product boundary, invariants, runtime gate, and drift checks |
| [`docs/research-disposition.md`](docs/research-disposition.md) | Prior evidence, terminal research outcomes, and claims that remain unproven |
| [`docs/pitch.md`](docs/pitch.md) | The short version: problem, measured channel opportunity, product boundary, and limitations |
| [`docs/overview.md`](docs/overview.md) | Full what/why/how, measurements, positioning, and separated product/research gates |
| [`docs/system-design.md`](docs/system-design.md) | OMP-first runtime architecture, recovery, protocol boundaries, and supporting components |
| [`docs/observe-design.md`](docs/observe-design.md) | Observe as a released, automatic, content-free diagnostic surface |
| [`docs/observe-cli.md`](docs/observe-cli.md) | `laconic observe` operator guide: install/remove/status/report |
| [`docs/k1-stage-a-cli.md`](docs/k1-stage-a-cli.md) | `laconic k1 stage-a scan` metadata feasibility guide |
| [`docs/k1-stage-b-manifest-cli.md`](docs/k1-stage-b-manifest-cli.md) | `laconic k1 stage-b build-manifest` guide |

## Current package

Install version 0.8.0 to inspect the released codec, evaluation, rendering, and Observe surfaces:

```bash
uv run laconic measure tests/corpus --expect tests/corpus/expected.json
uv run laconic gates --corpus tests/corpus --format json
laconic --help
```

The committed fixture reports K1 net savings of 8.53% against its pre-registered 15% research threshold. It validates the gate machinery but is not representative deployment evidence. That result does not block the bounded OMP beta, and the beta will not claim general token, cost, cache, or behavior savings from character reduction.

## Product roadmap

1. Build a transport-neutral session engine with namespaced exact recovery and strict-smaller decisions.
2. Package an ownership-safe OMP extension with a 250 ms deadline, fail-open behavior, expansion, and operator controls.
3. Qualify the built package through at least 10 completed real OMP sessions across 3 repositories and at least 100 eligible observations.
4. Release the opt-in OMP beta only when every safety criterion passes.
5. Design the Claude Code adapter separately after the protocol survives OMP dogfood. MCP, action rewriting, and history compaction remain deferred.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
