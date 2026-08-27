# Laconic – Compress what a coding agent carries, not what it says.

> **Status: research and evaluation package, v0.7.0.** The reversible codec core, replay/gate harness, and human-study harness are released. Hook integration is the intended primary runtime surface and MCP is secondary, but both are blocked: K1 measures 8.53% net savings on the committed fixture corpus, below the 15% kill threshold. That corpus validates the gate machinery, not real-world deployment economics.

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

## Start with evidence

Run the current measurement and gate surfaces before considering deployment:

```bash
uv run laconic measure tests/corpus --expect tests/corpus/expected.json
uv run laconic gates --corpus tests/corpus --format json
```

The K1 fixture verdict blocks live hook/MCP integration. A representative-corpus K1 decision is the next product gate; it is not a reason to tune the codec or relax thresholds after observing the fixture result.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
