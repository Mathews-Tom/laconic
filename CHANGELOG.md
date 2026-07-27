# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `laconic measure [PATH ...]` decomposes a corpus of session transcripts into the four context-window channels — tool results, tool call arguments, human-facing prose, and human prompts — and weights the result by real per-model pricing. `--expect FILE` checks the measurement against a committed expected-values file and exits non-zero on any difference.
- Cache-aware cost accounting (`laconic.costs`): provider pricing, the published cache read and write multipliers, and a spend split across uncached input, cache reads, cache writes, and output tokens.
- Session transcript ingest (`laconic.replay.corpus`): sorted transcript discovery, channel attribution, per-model token accounting, and redaction that removes content while preserving channel sizes exactly, so a private transcript can become a committable fixture.
- A synthetic fixture corpus under `tests/corpus` with committed expected values and a documented transcript schema.
- A PEP 561 `py.typed` marker, so the package's types reach consumers.

### Changed

- `scripts/measure_session_composition.py` is now a thin shim over `laconic measure`. It takes the same arguments and prints the same output; the measurement itself lives in the package.

## [0.0.1] — 2026-07-27

### Added

- Initial packaging, lint, strict typing, test, and CI surface, with an importable `laconic` package and a `laconic` console script exposing `--version` and `--help`.

[Unreleased]: https://github.com/Mathews-Tom/Laconic/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/Mathews-Tom/Laconic/releases/tag/v0.0.1
