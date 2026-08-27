# Laconic – Compress what a coding agent carries, not what it says.

> **Status: research and evaluation package, v0.8.0.** The reversible codec core, replay/gate harness, and human-study harness are released. Laconic Observe (`laconic observe`) is also released: a local, content-free automatic measurement surface for Claude Code and OMP, gated entirely behind an operator-run CLI command. Hook/MCP *codec* integration is a separate, still-blocked surface: K1 measures 8.53% net savings on the committed fixture corpus, below the 15% kill threshold. That corpus validates the gate machinery, not real-world deployment economics, and Observe does not change this disposition.

Laconic is a codec for the machine-to-machine traffic of coding agents: it re-encodes observations and actions at the tool boundary, keeps every elision recoverable, and renders prose for a human only on demand.

> **[`docs/grounding.md`](docs/grounding.md) is the authoritative statement of what Laconic is, what it deliberately is not, and how to detect strategy drift.** Read it before proposing or reviewing changes.

## Install

```bash
uv tool install laconic
laconic --help
```

## Documentation

| Document                                         | What's in it                                                                 |
| ------------------------------------------------ | ---------------------------------------------------------------------------- |
| [`docs/pitch.md`](docs/pitch.md)                 | The short version: the problem, the measurement, what is and isn't claimed   |
| [`docs/overview.md`](docs/overview.md)           | What Laconic is, why, the evidence, prior work, and the pre-registered gates |
| [`docs/system-design.md`](docs/system-design.md) | Architecture, components, data model, and the evaluation harness             |
| [`docs/observe-design.md`](docs/observe-design.md) | Laconic Observe: the automatic, content-free measurement surface design    |
| [`docs/observe-cli.md`](docs/observe-cli.md)     | `laconic observe` operator guide: install/remove/status/report              |
| [`docs/k1-stage-a-cli.md`](docs/k1-stage-a-cli.md) | `laconic k1 stage-a scan` operator guide: metadata feasibility screen        |
| [`docs/k1-stage-b-manifest-cli.md`](docs/k1-stage-b-manifest-cli.md) | `laconic k1 stage-b build-manifest` operator guide: session-level manifest |

## Start with evidence

Run the current measurement and gate surfaces before considering deployment:

```bash
uv run laconic measure tests/corpus --expect tests/corpus/expected.json
uv run laconic gates --corpus tests/corpus --format json
```

The K1 fixture verdict blocks live hook/MCP integration. A representative-corpus K1 decision is the next product gate; it is not a reason to tune the codec or relax thresholds after observing the fixture result.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
