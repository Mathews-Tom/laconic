# Laconic – Compress what a coding agent carries, not what it says.

> **Status: OMP runtime candidate qualified on `main`, published package v0.8.0.** The repository contains the opt-in OMP extension, session-owned Python engine, exact namespaced recovery, fail-open boundary, and operator CLI. The predeclared real-OMP qualification campaign passed with every safety counter at zero — see [`docs/runtime-beta-report.md`](docs/runtime-beta-report.md). The published v0.8.0 package still does not contain the integration; release awaits the version bump and publication step.

Laconic is a private, local runtime codec for existing coding agents. It reduces eligible model-visible `read`, `bash`, `grep`, and `glob` observations while preserving exact, on-demand access to omitted content. It integrates with OMP rather than replacing it.

> **[`docs/grounding.md`](docs/grounding.md) is the authoritative statement of what Laconic is, what it deliberately is not, and how to detect strategy drift.** Read it before proposing or reviewing changes.

## Install the runtime candidate

From a source checkout:

```bash
uv tool install .
laconic install omp --dry-run
laconic install omp
```

Start OMP normally. Use `/laconic status|pause|resume` in the active session, `laconic status` for content-free aggregate health, and `laconic expand '<session>/F1[:first-last]'` for exact operator recovery. See [`docs/omp-runtime.md`](docs/omp-runtime.md) before installing or purging data.

## Documentation

| Document | What's in it |
| --- | --- |
| [`docs/grounding.md`](docs/grounding.md) | Product boundary, invariants, runtime gate, and drift checks |
| [`docs/omp-runtime.md`](docs/omp-runtime.md) | Runtime installation, interception boundary, recovery, controls, uninstall, and purge |
| [`docs/research-disposition.md`](docs/research-disposition.md) | Prior evidence, terminal research outcomes, and claims that remain unproven |
| [`docs/pitch.md`](docs/pitch.md) | The short version: problem, measured channel opportunity, product boundary, and limitations |
| [`docs/overview.md`](docs/overview.md) | Full what/why/how, measurements, positioning, and separated product/research gates |
| [`docs/system-design.md`](docs/system-design.md) | OMP-first runtime architecture, recovery, protocol boundaries, and supporting components |
| [`docs/observe-design.md`](docs/observe-design.md) | Observe as a released, automatic, content-free diagnostic surface |
| [`docs/observe-cli.md`](docs/observe-cli.md) | `laconic diagnostics observe` guide: install/remove/status/report |
| [`docs/k1-stage-a-cli.md`](docs/k1-stage-a-cli.md) | `laconic research k1 stage-a scan` metadata feasibility guide |
| [`docs/k1-stage-b-manifest-cli.md`](docs/k1-stage-b-manifest-cli.md) | `laconic research k1 stage-b build-manifest` guide |
| [`docs/runtime-beta-report.md`](docs/runtime-beta-report.md) | The qualification campaign's generated aggregate report, committed verbatim |
| [`docs/runtime-beta-runbook.md`](docs/runtime-beta-runbook.md) | How that campaign is frozen, run, and reported with `python -m laconic.beta` |

## Source checkout

Published version 0.8.0 contains the codec, evaluation, rendering, and Observe surfaces but no live runtime integration. It exposes research commands at the top level, such as `laconic measure` and `laconic gates`. The current source checkout moves those commands under the explicit `research` namespace:

```bash
uv run laconic research measure tests/corpus --expect tests/corpus/expected.json
uv run laconic research gates --corpus tests/corpus --format json
laconic --help
```

The committed fixture reports K1 net savings of 8.53% against its pre-registered 15% research threshold. It validates the gate machinery but is not representative deployment evidence. That result does not block the bounded OMP beta, and the beta will not claim general token, cost, cache, or behavior savings from character reduction. The qualification campaign separately measured 35.84% character reduction across ten agent-driven read-heavy investigation sessions; that figure describes that workload only, and no savings threshold gates the beta. See the "Beta qualification result" section of [`docs/omp-runtime.md`](docs/omp-runtime.md) for how the campaign was produced and what it does not establish.

## Product roadmap

1. ~~Build a transport-neutral session engine with namespaced exact recovery and strict-smaller decisions.~~
2. ~~Package an ownership-safe OMP extension with a 250 ms deadline, fail-open behavior, expansion, and operator controls.~~
3. ~~Qualify the built package through at least 10 completed real OMP sessions across 3 repositories and at least 100 eligible observations.~~
4. Release the opt-in OMP beta: bump the version, ship a wheel containing the post-campaign operator fix, and publish.
5. Design the Claude Code adapter separately after the protocol survives OMP dogfood. MCP, action rewriting, and history compaction remain deferred.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
