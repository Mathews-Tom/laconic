# Laconic Observe — Automatic Measurement Surface Design

**Status:** released supporting diagnostic surface in Laconic 0.8.0.

**Authority:** `docs/grounding.md` and `docs/research-disposition.md`.

## Purpose

Laconic Observe gives Laconic automatic interaction with coding agents without changing what an agent sees or does. It records local, content-free context-economics receipts so the CLI can report whether a project has enough evidence to consider a codec intervention.

It is not a compression hook. It is not a session primer, MCP tool, project-state manager, transcript archive, or deployment bypass.

## Contract

```text
Input:
  One supported client lifecycle/tool-result event.

Output:
  One local, content-free observation receipt and audit link.

Agent-visible effect:
  None.
```

Every hook invocation writes no context augmentation, replacement tool result, decision, model-facing message, prompt, or tool directive. A malformed event, unavailable storage, timeout, or internal error produces no agent-visible output and exits successfully.

## Product position

- **First product surface:** planned OMP runtime codec, delivered under its separate safety and dogfood gate.
- **Supporting diagnostic surface:** Observe's content-free hooks, CLI installation, status, report, and removal commands.
- **Later host adapter:** Claude Code, after the canonical runtime protocol survives OMP dogfood.
- **Deferred surface:** MCP, because it cannot intercept OMP's built-in tool results and is not needed for the first beta.
- **Separation:** Observe records metadata only. It does not transform content, satisfy the runtime gate, or provide representative savings evidence.

## Client adapters

V1 supports two independent adapter contracts:

| Client | Adapter responsibility | Shared assumptions prohibited |
| --- | --- | --- |
| Claude Code | Parse only its verified lifecycle/tool-result hook payloads; install/remove only its owned configuration entries. | Do not assume OMP event names, fields, or configuration shape. |
| OMP | Parse only its verified lifecycle/tool-result hook payloads; install/remove only its owned configuration/module entries. | Do not assume Claude Code event names, fields, or configuration shape. |

Both adapters normalize into a common receipt only after local schema validation. Each receipt includes adapter identity and schema version.

## Compatibility spike

Before implementing an adapter, run a fixture-backed compatibility spike for that client:

1. Verify which hook lifecycle event carries the completed tool result or session-close signal.
2. Verify the subprocess input and allowed output schema.
3. Verify project and user configuration locations plus non-destructive install/remove behavior.
4. Verify timeout and failure behavior from the client’s own contract.
5. Produce synthetic fixtures only; do not install a hook or read a real session.

A client that lacks a usable post-result event may use a separately designed session-close adapter. PreToolUse alone is insufficient because it cannot measure observed result size or residency.

## Receipt and privacy boundary

A receipt may contain only:

- opaque session identifier;
- adapter identifier and schema version;
- tool category;
- result-size and argument-size bands;
- success/error class;
- timestamp;
- receipt schema version;
- local audit-chain link.

A receipt must not contain prompt text, tool argument values, result bodies, source paths, command text, credentials, provider requests, model-visible content, or raw transcript data.

Receipts are local measurement artifacts. They may support a later metadata feasibility ledger but are neither paired codec-on evidence nor proof that a codec is economically worthwhile.

## Installer and runtime behavior

The CLI owns installation and removal:

```text
laconic observe install --client claude-code|omp --scope project|user --dry-run
laconic observe remove --client claude-code|omp --scope project|user
laconic observe status
laconic observe report
```

Install and remove must be idempotent, preserve unrelated client configuration, identify only Laconic-owned entries, and support a dry-run preview. The subprocess contract has a bounded wall-clock budget, local diagnostics outside agent flow, and exit-success/no-output behavior on every failure path.

## Evidence boundary

Observe receipts do not:

- enable or exercise the OMP codec-transformation surface;
- change the committed fixture's 8.53% K1 result;
- prove token, cost, cache, or behavior savings;
- authorize provider replay, real-session corpus collection, external data, or prospective-capture successors;
- supply K2 action equivalence, K3 human-study evidence, or the runtime beta's exact-recovery and fail-open proof.

## Verification contract

Implementation must prove:

1. Each adapter accepts only its verified synthetic event schema.
2. Malformed, unknown, or unsupported events produce a silent no-op and local diagnostic only.
3. No runtime path writes stdout/context augmentation or modifies a tool result.
4. Timeout and storage failure exit successfully without delaying the agent.
5. Receipts contain no prohibited content fields or values.
6. Install/remove is idempotent and preserves unrelated client configuration.
7. CLI status/report reflects local receipts without contacting a provider.
8. No Observe command changes codec configuration, research-gate status, runtime state, or provider state.

## Relationship to runtime delivery

Observe's shipped adapter research and ownership-safe installer patterns are implementation inputs for the OMP runtime, not deployment authorization or runtime proof. The runtime gets its own extension, storage, operator controls, failure containment, and qualification evidence. Observe remains available as a separate content-free diagnostic after that integration ships.