# Laconic

**Shrink what your coding agent carries. Lose nothing.**

Laconic sits at your agent's tool boundary and replaces bulky tool results with a structural outline plus the lines that were actually asked for — while keeping the exact original bytes on disk, addressable, until you say otherwise.

[![PyPI](https://img.shields.io/pypi/v/laconic)](https://pypi.org/project/laconic/)
[![Python](https://img.shields.io/pypi/pyversions/laconic)](https://pypi.org/project/laconic/)
[![CI](https://github.com/Mathews-Tom/laconic/actions/workflows/ci.yml/badge.svg)](https://github.com/Mathews-Tom/laconic/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

```bash
uv tool install laconic
laconic setup
```

Done. No provider configuration, no proxy, no API key, no account.

## See it work

Your agent reads a 745-line file and wants the `Record` class. Here is what actually lands in its context — real output from Laconic's own codec on its own ledger module:

```text
[laconic 01a0f3c2-.../F1 | full: laconic_expand({"reference":"01a0f3c2-.../F1"})]
src/laconic/ledger.py  745 lines
  outline: UnknownHandleError:150-159  __str__:153-159  InvalidSpanError:162-163
    _select_lines:166-179  ObservationKind:182-189  Record:193-212
    raw_chars:207-208  encoded_chars:211-212  [+30 more]
  span 193-212:
    class Record:
        """One observation, stored whole, surfaced partially."""

        handle: str
        kind: ObservationKind
        subject: str
        content_sha: str
        raw: str
        encoded: str
        created_at: float
        turn: int
        resident: bool

        @property
        def raw_chars(self) -> int:
            return len(self.raw)
```

**29,186 characters in, 810 out — 97% fewer characters at the tool boundary.** The agent still sees the shape of the whole file, gets the exact lines it asked for verbatim, and can pull any other part back by itself, mid-task, without asking you. The handle is right there on the first line.

Asked for nothing in particular, that same file comes back as a 313-character outline.

## What you get

### Nothing is ever lost

The raw result commits to a local ledger **before** a replacement is allowed to exist. Every reference expands exactly — byte for byte, including code points a strict encoder would reject.

```bash
laconic expand '<session>/X1'          # the whole thing, exactly
laconic expand '<session>/X1:40-90'    # just those lines
```

This is why reducing a file read is safe here at all. Headroom's default coding profile, for instance, deliberately protects reads from compression — its own source explains that an agent needs exact bytes to patch a file. Laconic stores those exact bytes first and then reduces what the model carries, which is a different answer to the same constraint rather than a claim to be the only one. [`docs/headroom-comparison.md`](docs/headroom-comparison.md) sets out where each fits.

### Structure, not truncation

File reads come back as a tree-sitter outline plus the span that was requested, so the agent sees the file's shape rather than a guillotined prefix.

Command and search output take the other route — head and tail are kept, the middle is elided, and **lines that look like errors are lifted out of the elided region and preserved**, because a traceback buried in the middle of ten thousand lines of build log is the last thing you want silently dropped.

### It only fires when it wins

A replacement is emitted only when the complete envelope — handle, header and all — is strictly smaller than the original. In qualification it passed **96 of 137** eligible observations straight through untouched. Small results stay small. Nothing is compressed to look busy.

### It fails open, in every direction

Engine missing, spawn failure, crash, malformed response, deadline breach, storage error — you get your original tool result. There is a 250 ms steady-state deadline and a three-consecutive-failure circuit breaker. **A crash costs compression, never correctness.**

### Fast enough to forget about

**p50 1.45 ms. p95 18.65 ms.**

### Entirely yours

No telemetry. No hosted service. No beacon. Raw observations never leave your machine, and exactly one command in the whole tool touches the network — `laconic pricing update` — only when you type it.

Reports are content-free by construction and re-checked by an independent privacy gate before anything is written: a new field that cannot be certified fails loudly rather than shipping.

### You stay in control

```bash
laconic status                 # decisions, counts, storage, recovery ledger
/laconic pause                 # mid-session, from inside your agent
/laconic resume
laconic uninstall omp          # restore native behaviour; keeps your ledgers
laconic purge --older-than 30d # deleting data is a separate, deliberate act
```

## Works with your agent

```bash
laconic setup
```

`setup` detects what you actually have, installs what each host supports, and then tells you whether the codec has recorded a real decision — because installing a file is not proof anything ran.

| Host | Codec | Diagnostics | Covers |
| --- | :---: | :---: | --- |
| **OMP** | yes | yes | Native extension over `read`, `bash`, `grep`, `glob` |
| **Claude Code** | yes | yes | Transforming `PostToolUse` hook over `Bash` and `Read` |
| Codex | — | — | No adapter ships, and Laconic tells you so |

Both adapters are thin. They drive the same Python engine, so the strictly-smaller rule, the ledger, reference minting and exact recovery are shared — not reimplemented per host.

```bash
laconic setup --verify-only    # did it actually run?
```

## Know what your context costs

```bash
laconic savings
```

```text
Modelled cost avoided
  $110.80 to $255.82  (4.58% to 10.58%)
  against a modelled $2,418.27 for the sessions this estimate covers
  A model, not a measurement: no session ran without the codec, so
  this is what the removed characters would have cost, not a saving
  anyone observed. Every assumption is listed in the written report.
```

You also get a full local breakdown of where your model spend actually went — uncached input, cache reads, cache writes, output — joined to what the codec did in those same sessions. Prices resolve through a registry that ships **3,134 models** offline and refreshes on demand.

The figure is deliberately a band, and deliberately labelled. It is modelled from token counters, never billed by a provider, and the report carries its own assumptions rather than burying them. When more than a quarter of the underlying cost comes from models with no published price, the tool stops printing dollars and leads with the percentage instead. **We would rather show you less than show you something we cannot stand behind.**

## Commands

| Command | What it does |
| --- | --- |
| `laconic setup` | Detect hosts, install what each supports, verify the codec ran |
| `laconic status` | Content-free health: decisions, storage, expansions, last cost band |
| `laconic savings` | Spend composition plus the modelled avoided-cost band |
| `laconic expand REF[:A-B]` | Recover an elided observation exactly, whole or by line span |
| `laconic pricing show` \| `update` | Inspect or refresh model list prices |
| `laconic install` \| `uninstall HOST` | Manage a single host adapter |
| `laconic purge` | Delete recovery ledgers, explicitly |
| `laconic research ...` | Offline measurement, replay, evaluation |
| `laconic diagnostics observe ...` | Content-free local diagnostics |

## Documentation

| Document | What's in it |
| --- | --- |
| [`docs/omp-runtime.md`](docs/omp-runtime.md) | OMP install, interception boundary, recovery, controls, uninstall, purge |
| [`docs/claude-code-codec.md`](docs/claude-code-codec.md) | The Claude Code hook, its shape-fidelity constraint, measured limits |
| [`docs/system-design.md`](docs/system-design.md) | Architecture: engine, ledger, codec, price registry, protocol boundaries |
| [`docs/grounding.md`](docs/grounding.md) | What Laconic is, what it deliberately is not, and its invariants |
| [`docs/headroom-comparison.md`](docs/headroom-comparison.md) | Version-pinned comparison with Headroom, including where Headroom fits better |
| [`docs/overview.md`](docs/overview.md) | The measurement behind the design, and the positioning it forces |
| [`docs/research-disposition.md`](docs/research-disposition.md) | How Laconic got here: evidence, shipped tranches, and what stays unproven |
| [`docs/observe-cli.md`](docs/observe-cli.md) | `laconic diagnostics observe`: content-free local diagnostics |
| [`docs/runtime-beta-report.md`](docs/runtime-beta-report.md) | The qualification campaign's generated report, committed verbatim |

Upgrading from 0.8.0 or earlier? Offline research commands moved under an explicit namespace: `laconic measure` and `laconic gates` are now `laconic research measure` and `laconic research gates`.

## What we don't claim

Laconic reduces characters at the tool boundary, and that is what it reports. It does not claim a general token, cost, cache, or behaviour saving, because the conversion from characters to tokens to money is lossy and workload-dependent and we do not have the paired evidence that would license it. Every session Laconic has recorded ran with the codec on, so there is no counterfactual to subtract.

What *is* measured is in [`docs/runtime-beta-report.md`](docs/runtime-beta-report.md), generated and committed verbatim: ten sessions, three repositories, 137 eligible observations, all 26 required failure and lifecycle scenarios exercised, every safety counter at zero, and 35.84% character reduction on that workload.

## Contributing

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src
uv run python -m pytest -q
```

Python 3.12+, `uv`, `mypy --strict`, 1,596 tests. [`docs/grounding.md`](docs/grounding.md) is the charter — read it before proposing a change that widens a claim.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
