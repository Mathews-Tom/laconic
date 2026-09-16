# Laconic and Headroom

Laconic and [Headroom](https://github.com/headroomlabs-ai/headroom) both reduce context sent to language models, but they operate at different boundaries and optimize for different outcomes.

**Headroom is a broad context-compression platform. Laconic is a narrow coding-agent runtime codec.** Headroom provides libraries, a provider proxy, MCP tools, agent wrappers, multiple compressors, cache-aware request handling, memory, and optional output shaping. Laconic integrates directly with a host's tool-result lifecycle — OMP through a native extension, Claude Code through a transforming `PostToolUse` hook — and changes only eligible tool observations, backed by durable exact recovery.

This comparison uses Laconic 0.12.0 and Headroom 0.37.0. It describes product contracts and shipped defaults, not a measured head-to-head performance result.

## Comparison

| Dimension | Laconic | Headroom 0.37.0 |
| --- | --- | --- |
| Product boundary | Runtime codec for tool observations in an existing coding agent | General context-compression layer for agents and applications |
| Primary integration | Native OMP extension at the `tool_result` boundary, plus a Claude Code transforming `PostToolUse` hook; both drive one canonical Python engine | Python and TypeScript libraries, local provider proxy, MCP server, and agent wrappers |
| OMP path | Intercepts OMP tool results without changing provider configuration | `headroom wrap omp` redirects OMP's Anthropic provider endpoint through Headroom's local proxy |
| Provider relationship | Provider- and model-neutral inside OMP | Supports multiple providers overall; its OMP wrapper is Anthropic-specific |
| Host coverage | OMP and Claude Code have shipped adapters; Codex has none, and Laconic says so rather than degrading silently | Broad client, framework, and provider coverage |
| Default coding inputs | Successful single-text `read`, `bash`, `grep`, and `glob` results on OMP; `Bash` and `Read` on Claude Code, which exposes no `Grep`/`Glob` tool | Broad content routing; the default coding profile protects file reads from lossy compression |
| File reads | Structural outline plus the requested span, with exact full or span recovery | Kept byte-exact by the default coding profile because coding agents patch exact source |
| Recovery | Owner-only, namespaced session ledger retained until explicit purge | Hash-keyed CCR cache with a default 30-minute TTL and automatic capacity eviction |
| Replacement rule | Emit only when the complete recovery-bearing envelope is strictly smaller | Compressor- and profile-specific policies, with passthrough when compression is unsuitable |
| Failure behavior | Raw content commits before replacement; unsupported inputs and runtime failures pass through; 250 ms steady-state deadline and a three-failure circuit breaker | Compressors generally fail open and return the original content |
| Prompt-cache policy | Does not rewrite prior history and makes no cache-savings claim | Cache mode preserves an older prefix and compresses the newest eligible delta |
| Operator controls | Install, status, pause, resume, exact expansion, uninstall, and explicit purge | Deploy, wrap, unwrap, doctor, performance reporting, dashboard, and configuration profiles |
| Data handling | Laconic itself sends no telemetry or observations to a Laconic service | Compression and recovery are local; an anonymous content-free session beacon is enabled by default unless disabled |
| Cost reporting | Read-only local spend composition priced through a three-layer model-price registry (3,134 models bundled offline, refreshable by explicit command). The avoided-cost figure is labelled `modelled_not_measured` in code and withholds its own dollar figures when more than 25% of the underlying cost rests on unpublished prices | Performance reporting and a dashboard |
| Evidence posture | Publishes its safety qualification and reports character reduction without relabeling it as token or cost savings | Publishes offline token benchmarks and live-provider demonstrations |

## Where Laconic excels

### Native coding-agent interception

Laconic operates where OMP turns a completed tool call into model-visible context. It knows which host tool produced the observation and can apply tool-specific policy before the provider request is built. It does not require OMP's model traffic to pass through a Laconic proxy and does not alter the configured provider endpoint.

That boundary matters when one OMP installation uses several providers or when an operator wants the provider path left untouched.

### Safe reduction of file reads

File reads are the central difference. Headroom's default coding profile sets `protect_reads=True`; its source explains that an agent must retain exact bytes to patch a file safely. Laconic addresses that constraint through recovery rather than passthrough:

1. store the exact raw read in an owner-only session ledger;
2. emit a structural outline and the requested source span only when that complete envelope is smaller;
3. expose exact full-result and line-span expansion to the model and operator;
4. retain the source until the operator explicitly purges it.

The visible representation is scoped, but the source remains lossless in reach.

### Durable exact recovery

Laconic references are namespaced by session, checked against ownership boundaries, and usable after engine restart, session resume, or branch navigation. Full and span expansion must reproduce the stored content exactly.

Headroom's CCR also supports retrieval of original content, but its default cache lifetime is 1,800 seconds and entries are subject to capacity eviction. Its contract is reversible while an entry remains cached. Laconic's contract is durable recovery until explicit purge.

### A small, auditable failure boundary

The OMP beta changes only successful, single-text results from four named tools. It passes through tool errors, mixed or non-text content, unsupported tools, storage failures, protocol failures, engine failures, malformed responses, latency breaches, and candidates that are not strictly smaller. Raw content commits before an encoded reference becomes visible.

Headroom also implements fail-open behavior. Laconic's advantage is not exclusive possession of that property; it is the smaller policy and integration surface that must uphold it.

### No Laconic telemetry or hosted service

Laconic stores recovery data and content-free diagnostics locally. It has no Laconic telemetry endpoint, anonymous beacon, account, or hosted control plane.

Headroom performs compression locally, but Headroom 0.37.0 separately enables an anonymous content-free session beacon by default. Operators can disable it with `HEADROOM_BEACON=off`, `DO_NOT_TRACK=1`, or offline mode. The distinction is about default data egress, not whether raw tool content is sent to Headroom Labs.

### Claims constrained by evidence

Laconic's real-OMP qualification covered ten sessions across three repositories and 162 eligible observations. Every safety counter was zero; encoding latency was 2.16 ms at p50 and 19.87 ms at p95; the read-heavy workload showed 51.77% character reduction.

That result does not establish token, cost, cache, or behavior savings. Laconic's spend report is single-arm. M20-v1 ran once and returned its pre-registered [incomplete disposition](results/controlled-spend-pilot.md): 0 valid cells, no dispersion, correlation, effect, or confirmatory sample, and no reusable observation. M20-v2 is the mechanically corrected protocol, with a separate manifest, prompts, private root, and authorization gate. Neither result supports a savings, equivalence, direction, or superiority claim. M20-v2 then ran once and completed, publishing only dispersion, cross-arm cost correlations, mechanism and completion counters, pooled gateway spend, and a confirmatory sample size of 15 tasks at two repetitions. It reports no arm means and no paired effect, so it still does not establish that either tool costs less than the other.

## Where Headroom excels

Headroom is the stronger choice when breadth is the requirement. It provides:

- integrations across multiple agents, providers, and application frameworks;
- library, proxy, MCP, and wrapper deployment models;
- compression for a wider range of structured and unstructured content;
- explicit prompt-cache alignment;
- cross-agent memory and retrieval;
- optional output-token and reasoning-effort controls;
- operational commands and dashboards around measured compression.

Laconic does not provide those capabilities. Adding them solely to match Headroom would expand Laconic beyond its measured coding-agent runtime boundary.

## Which one to use

Use **Headroom** when the requirement is broad context optimization across clients, providers, content types, or application frameworks.

Use **Laconic** when the requirement is OMP-native, provider-neutral tool-result interception; deterministic handling of coding-agent observations; exact durable full or span recovery; explicit operator control; and no Laconic data egress.

Do not run both over the same observation path without a separate compatibility qualification. Double transformation, nested recovery references, and interacting cache policies have not been tested as a supported configuration.

## Evidence boundary

No controlled head-to-head coding-agent study currently shows that Laconic saves more tokens or money, preserves behavior better, or completes tasks faster than Headroom. Their published measurements use different workloads, units, profiles, and evaluation designs.

A valid comparison must run native OMP, Laconic, and Headroom on the same pinned tasks; randomize within-task arm order; charge expansions and extra turns to the arm that caused them; use one pinned OMP model/catalog and its provider-counter-derived cost fields; and judge task completion separately from compression ratio. M20-v1 did not satisfy its frozen validity conditions, may not be resumed, and produced no sample-feasibility result. M20-v2 kept the same population and analysis parameters while correcting only operational validity mechanics. It ran once under a single-use owner authorization that has been consumed, and it may not be rerun. A confirmatory comparison still requires its own frozen sample and explicit spend authorization.

## Sources

### Laconic

- [Grounding charter](grounding.md)
- [OMP runtime guide](omp-runtime.md)
- [Runtime beta report](runtime-beta-report.md)
- [Controlled spend comparison design](spend-comparison-design.md)
- [Controlled spend pilot disposition](results/controlled-spend-pilot.md)
- [OMP runtime implementation](../src/laconic/runtime/omp/laconic.ts)

### Headroom 0.37.0

- [README and product surface](https://github.com/headroomlabs-ai/headroom/blob/v0.37.0/README.md)
- [Default coding profile](https://github.com/headroomlabs-ai/headroom/blob/v0.37.0/headroom/agent_savings.py)
- [OMP wrapper](https://github.com/headroomlabs-ai/headroom/blob/v0.37.0/headroom/providers/omp/runtime.py)
- [CCR recovery store](https://github.com/headroomlabs-ai/headroom/blob/v0.37.0/headroom/cache/compression_store.py)
- [Telemetry and beacon policy](https://github.com/headroomlabs-ai/headroom/blob/v0.37.0/headroom/telemetry/beacon.py)
- [Published limitations](https://github.com/headroomlabs-ai/headroom/blob/v0.37.0/docs/content/docs/limitations.mdx)
