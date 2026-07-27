# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-07-27

### Added

- `laconic measure [PATH ...]` decomposes a corpus of session transcripts into the four context-window channels — tool results, tool call arguments, human-facing prose, and human prompts — and weights the result by real per-model pricing. `--expect FILE` checks the measurement against a committed expected-values file and exits non-zero on any difference.
- Cache-aware cost accounting (`laconic.costs`): provider pricing, the published cache read and write multipliers, and a spend split across uncached input, cache reads, cache writes, and output tokens.
- Session transcript ingest (`laconic.replay.corpus`): sorted transcript discovery, channel attribution, per-model token accounting, and redaction that removes content while preserving channel sizes exactly, so a private transcript can become a committable fixture.
- A synthetic fixture corpus under `tests/corpus` with committed expected values and a documented transcript schema.
- A PEP 561 `py.typed` marker, so the package's types reach consumers.
- The handle ledger (`laconic.ledger`): a content-addressed, session-scoped SQLite store that keeps every observation whole while the codec surfaces only part of it. `register` mints a short model-typeable handle (`F1`, `B7`, `S2`) and reuses it when byte-identical content is re-observed under the same subject; `get` resolves a handle; `expand` recovers the whole payload or a `handle:first-last` line range. Unknown handles and out-of-range spans raise instead of returning empty. Raw payloads are stored zstd-compressed.
- `zstandard` is now a runtime dependency: the ledger compresses stored payloads, and the Python 3.12 floor carries no standard-library zstd binding.
- The file observation encoder (`laconic.codec.encoders.file.FileEncoder`): replaces a whole-file read with a structural outline plus the requested span, registers the pair with the handle ledger, and never elides a declared edit-target region. Structural outlining (`laconic.codec.outline`) covers a documented minimum grammar set — Python, JavaScript, TypeScript (including TSX), Go, and Rust — via `tree-sitter`, with a safe head-of-file fallback for any other file type or internal parse failure; the outliner never raises. Span resolution (`laconic.codec.span`) honors an explicit `offset`/`limit` request, defers to the outline when it already answers the request, and otherwise degrades to a bounded positional window.
- `laconic measure --codec on --report reduction` runs the file observation encoder over every `Read` tool result in a corpus and reports gross encoded volume against raw volume. It is deliberately a gross figure — induced-read accounting is `laconic replay`'s job in a later milestone.
- `tree-sitter`, `tree-sitter-go`, `tree-sitter-javascript`, `tree-sitter-python`, `tree-sitter-rust`, and `tree-sitter-typescript` are now runtime dependencies, pinned to an exact, verified-compatible version set: a looser resolution reproducibly corrupted parsed position data under repeated parses with no exception raised.

### Changed

- `scripts/measure_session_composition.py` is now a thin shim over `laconic measure`. It takes the same arguments and prints the same output; the measurement itself lives in the package.

## [0.0.1] — 2026-07-27

### Added

- Initial packaging, lint, strict typing, test, and CI surface, with an importable `laconic` package and a `laconic` console script exposing `--version` and `--help`.

[Unreleased]: https://github.com/Mathews-Tom/Laconic/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Mathews-Tom/Laconic/compare/v0.0.1...v0.2.0
[0.0.1]: https://github.com/Mathews-Tom/Laconic/releases/tag/v0.0.1
