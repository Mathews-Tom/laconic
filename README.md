# Laconic – Compress what a coding agent carries, not what it says.

> **Status: early package release.** `laconic` 0.2.0 ships the handle ledger and the file observation encoder — the largest single lever, replacing whole-file reads with a structural outline plus the requested span. Command, search, and action encoding are not yet implemented.

Most of what fills a coding agent's context window is never read by a human — file contents, command output, patches, search hits — and it is re-ingested on every turn of a session. Laconic is a codec for that traffic: it re-encodes observations and actions at the tool boundary, keeps every elision recoverable, and renders prose for a human only on demand.

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

## Measurement

One tool is runnable today. It decomposes real agent session transcripts into observations, actions, prose, and prompts, weighted by model pricing — the analysis the design is built on:

```bash
python3 scripts/measure_session_composition.py
```

Run it against your own sessions before taking any number in `docs/` on faith.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
