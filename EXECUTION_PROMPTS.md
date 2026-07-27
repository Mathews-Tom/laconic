# Execution Prompts — Laconic

One `/goal` block per milestone in `DEVELOPMENT_PLAN.md` §6. Each block is designed to be run in an independent session with no prior context, so the design gate and review contract are restated in full every time.

## Global execution rules (apply to every goal)

- Use `stacked-prs`; each implementation PR is based on the preceding stack branch until that base merges.
- Use Conventional Commits, atomic commits, no attribution, and independently reviewable PRs.
- Run the mandatory pre-implementation design gate before creating product-code branches or changing product code.
- The committed root files `DEVELOPMENT_PLAN.md` and `EXECUTION_PROMPTS.md` are authoritative. The local ignored `.docs/DEVELOPMENT_PLAN_HISTORY.md` ledger is evidence; rebuild it from committed artifacts, merged PRs, CI, and current code when absent.
- A material plan change must update the current milestone and every affected future milestone before implementation. Rebuild the DAG and release trains after the update.
- A docs-only reconciliation PR is required for a material revision. It must be reviewed, green, and externally merged before code begins.
- A shared mismatch in a proposed parallel wave blocks product-code work in every affected lane. Do not continue scaffolding, partial implementation, or isolated ledger writes while reconciliation is pending.
- `GO` only makes the milestone stack merge-eligible. Release preparation remains deferred until every milestone in its train is externally merged.
- M1's static PEP 621 metadata version `0.0.0` is an internal package-build identifier for installation and `laconic --version`, not a release version. It remains fixed until an explicit release policy is supplied; do not add release-version, tag, publication, or changelog work.
- **M1 bootstrap exception.** Before M1 PR-4 adds the first workflow, any M1 stack PR whose head lacks that workflow cannot have a successful GitHub Actions run. Its green-CI requirement instead means no configured workflow or failed GitHub check run, passing PR-specific local verification, and passing the fixed plan/prompt structural harness from `plan-artifact-gate-verification` plus the Mermaid 11 `mermaid.parse()` harness from `validate-mermaid-diagrams` over every `DEVELOPMENT_PLAN.md` diagram, all recorded in the PR. This covers the reconciliation PR and M1 PR-1 through PR-3. M1 PR-4 requires actual green CI; later prerequisites require actual green CI.
- Never solicit a review from an external or bot reviewer. Address whatever the repository's configured reviewer posts on its own.

**Project-specific rule.** `docs/overview.md` §6.3 pre-registers five gates with kill conditions. A gate breach is a material mismatch by definition. A K1 result below 15% after M9 invalidates M12 and M13; K1 from 15% through less than 25% is a non-kill failed target that still blocks both until a docs-only reconciliation resolves the target miss.

---

### M1 — Repository scaffold and verification surface

```text
/goal Deliver milestone M1 (Repository scaffold and verification surface) from DEVELOPMENT_PLAN.md as a reviewed stack of PRs.

CONTEXT: DEVELOPMENT_PLAN.md §6 M1 + docs/system-design.md §7-8. Preconditions: none. Repo: design-stage Laconic; no pyproject.toml, no src tree, no CI; Python 3.12+ per docs/system-design.md §8.1; this plan establishes uv, ruff, mypy --strict, and pytest; one existing script at scripts/measure_session_composition.py. M1's static PEP 621 metadata version `0.0.0` is an internal package-build identifier required for installation and `laconic --version`, not a release version.
OBJECTIVE: Establish the packaging, typing, lint, test, and CI surface every later milestone verifies against, plus an importable laconic package with a CLI entrypoint. Acceptance: uv sync succeeds from a clean checkout; laconic --version emits the fixed internal build identifier; laconic --help exits 0 and advertises no unimplemented verbs; mypy --strict src reports zero errors; CI green on M1 PR-4.
RELEASE TRAIN: target=unversioned; included milestones=M1-M14; preparation trigger=all included milestones externally merged; required artifacts=none per the release-policy GAP in DEVELOPMENT_PLAN.md §2; release verification=uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest -q && uv run laconic gates --corpus tests/corpus; publication=not requested.

PRE-IMPLEMENTATION DESIGN GATE:
1. Read this milestone, its source-map rows, current prompt, and `.docs/DEVELOPMENT_PLAN_HISTORY.md` when present.
2. Inspect the current codebase plus merged predecessor diffs, merged predecessor PR outcomes, CI/check evidence, and predecessor verification output.
3. Revalidate objective, interfaces, dependencies, acceptance, verification, risks, release train, and every listed dependent milestone: M2, M3, M4, M5, M6, M7, M8, M9, M10, M11, M12, M13, M14.
4. Append one ledger entry: timestamp, milestone, decision, trigger, evidence, plan/prompt sections changed, downstream impact, and implementation authorization.
5. If no material mismatch exists, report `DESIGN GO — PLAN REVISION: none`; this authorizes implementation.
6. If a mismatch exists, update both authoritative artifacts for M1 and every affected future milestone, append the revision ID, and report `DESIGN GO — PLAN REVISION: <entry IDs>`. This records a completed diagnosis but blocks product-code work until the reconciliation prerequisite merges.
7. If validity cannot be established, report `DESIGN NO-GO — REASON: <evidence>` and stop. After a reconciliation PR merges, repeat this gate and require `DESIGN GO — PLAN REVISION: none` before implementation.

RECONCILIATION RULE: A material revision opens `docs(plan): reconcile M1 design` as a docs-only prerequisite PR. It contains no product code, must be reviewed, green, and externally merged before any code PR, and must not be folded into an implementation PR.

PLANNED STACK (refine only to keep PRs reviewable):
0. Conditional prerequisite `docs(plan): reconcile M1 design` — scope: authoritative plan/prompt updates only; gate: reviewed, green, and merged before the implementation stack.
1. PR-1 packaging foundation — scope: pyproject.toml, uv.lock, src layout, Python floor; commits: build(uv): add project manifest and lockfile, build(pkg): add src layout; verification: uv sync && uv run python -c "import laconic"
2. PR-2 lint and type configuration, on PR-1 — scope: ruff and mypy --strict config; commits: build(lint): configure ruff, build(types): configure mypy strict; verification: uv run ruff check . && uv run mypy --strict src
3. PR-3 test surface, on PR-2 — scope: pytest layout, conftest, one real smoke test; commits: test: add pytest layout and smoke test; verification: uv run pytest -q
4. PR-4 CI and CLI entrypoint, on PR-3 — scope: GitHub Actions workflow, laconic console script with --help and --version; commits: ci: add lint type and test workflow, feat(cli): add laconic entrypoint; verification: uv run laconic --help && uv run laconic --version | grep -Fx 'laconic 0.0.0'

CONSTRAINTS: no scope leakage into codec, ledger, renderer, or measurement behavior; minimal dependencies; repo style; keep the M1 internal build identifier fixed; no release-version, tag, publication, or changelog updates before the release-train trigger and an explicit policy.
VERIFICATION (must pass): `uv sync && uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest -q && uv run laconic --help && uv run laconic --version | grep -Fx 'laconic 0.0.0'` — all exit 0; CI green on M1 PR-4.
REVIEW:
Per PR:
- Scope matches its purpose; contracts match the reconciled plan; behavior is meaningfully tested.
- Failures are loud; security, data safety, and rollback requirements are addressed where relevant.
- History is atomic, conventional, attribution-free, and free of unrelated formatting churn.
- PR-specific verification output is captured.
Whole stack:
- Bases form one valid stack; cumulative acceptance and integration hold; M1 PR-4 CI is green; no regression coverage is removed without replacement.
- The docs-only root is reviewed and satisfies the M1 bootstrap exception before code; M1 PR-1 through PR-3 satisfy that exception before M1 PR-4 establishes actual CI.
- Report PR URLs, bases, verification, risks, manual gates, and review completion.
FINAL VERDICTS:
- Report the design verdict before the merge verdict.
- Then report exactly one merge verdict: `GO — RELEASE: unversioned — RELEASE PREP: pending` or `NO-GO — RELEASE: unversioned — REASON: <blocking gate>`.
- `GO` requires `DESIGN GO`, every PR correctly based/reviewed/green, local verification, and full milestone acceptance. `NO-GO` applies to pending or failed checks, incomplete review, scope drift, ambiguous readiness, manual gates, or unresolved release target.
DONE: design verdict with evidence; when authorized, a reviewed stack with a release-aware merge verdict and evidence.
```

---

### M2 — Cost accounting and corpus ingest

```text
/goal Deliver milestone M2 (Cost accounting and corpus ingest) from DEVELOPMENT_PLAN.md as a reviewed stack of PRs.

CONTEXT: DEVELOPMENT_PLAN.md §6 M2 + docs/overview.md §2 + docs/system-design.md §2.3. Preconditions: M1 merged. Repo: uv, ruff, mypy --strict, pytest, CI established by M1; scripts/measure_session_composition.py holds the reference implementation.
OBJECTIVE: Make the four-channel measurement a first-class tested package capability and define the committed fixture-corpus format the evaluation harness consumes. Acceptance: cost split sums to 100.00% within tolerance; channel decomposition matches a committed expected-values file exactly; redaction removes source content while preserving channel sizes; empty corpus exits non-zero with a clear message.
RELEASE TRAIN: target=unversioned; included milestones=M1-M14; preparation trigger=all included milestones externally merged; required artifacts=none per the release-policy GAP in DEVELOPMENT_PLAN.md §2; release verification=uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest -q && uv run laconic gates --corpus tests/corpus; publication=not requested.

PRE-IMPLEMENTATION DESIGN GATE:
1. Read this milestone, its source-map rows, current prompt, and `.docs/DEVELOPMENT_PLAN_HISTORY.md` when present.
2. Inspect the current codebase plus merged predecessor diffs, merged predecessor PR outcomes, CI/check evidence, and predecessor verification output.
3. Revalidate objective, interfaces, dependencies, acceptance, verification, risks, release train, and every listed dependent milestone: M4, M5, M7, M8, M9, M10, M11, M12, M13, M14. Confirm provider pricing and cache multipliers are current, and confirm the fixture corpus is representative enough for K1 to be meaningful.
4. Append one ledger entry: timestamp, milestone, decision, trigger, evidence, plan/prompt sections changed, downstream impact, and implementation authorization.
5. If no material mismatch exists, report `DESIGN GO — PLAN REVISION: none`; this authorizes implementation.
6. If a mismatch exists, update both authoritative artifacts for M2 and every affected future milestone, append the revision ID, and report `DESIGN GO — PLAN REVISION: <entry IDs>`. This blocks product-code work until the reconciliation prerequisite merges.
7. If validity cannot be established, report `DESIGN NO-GO — REASON: <evidence>` and stop. After a reconciliation PR merges, repeat this gate and require `DESIGN GO — PLAN REVISION: none` before implementation.

RECONCILIATION RULE: A material revision opens `docs(plan): reconcile M2 design` as a docs-only prerequisite PR. It contains no product code, must be reviewed, green, and externally merged before any code PR, and must not be folded into an implementation PR.

PLANNED STACK (refine only to keep PRs reviewable):
0. Conditional prerequisite `docs(plan): reconcile M2 design` — scope: authoritative plan/prompt updates only; gate: reviewed, green, and merged before the implementation stack.
1. PR-1 cost model — scope: costs.py pricing table, cache read/write multipliers, session-level accounting; commits: feat(costs): add pricing and cache-aware cost model; verification: uv run pytest tests/test_costs.py -q
2. PR-2 corpus ingest and redaction, on PR-1 — scope: replay/corpus.py transcript parsing, channel attribution, redaction; commits: feat(corpus): add transcript ingest, feat(corpus): add redaction; verification: uv run pytest tests/test_corpus.py -q
3. PR-3 fixture corpus, on PR-2 — scope: synthetic corpus under tests/corpus plus expected.json; commits: test(corpus): add synthetic fixture corpus and expected values; verification: uv run pytest tests/test_corpus.py -q -k fixture
4. PR-4 measure command, on PR-3 — scope: laconic measure and a thin compatibility shim at scripts/measure_session_composition.py; commits: feat(cli): add measure command, refactor(scripts): reduce standalone script to a shim; verification: uv run laconic measure tests/corpus --expect tests/corpus/expected.json && uv run python scripts/measure_session_composition.py tests/corpus

CONSTRAINTS: no raw private session data may be committed — fixtures are synthetic or redacted; no replay, gate, or encoding behavior; minimal dependencies; repo style.
VERIFICATION (must pass): `uv run pytest tests/test_costs.py tests/test_corpus.py -q && uv run laconic measure tests/corpus --expect tests/corpus/expected.json && uv run python scripts/measure_session_composition.py tests/corpus` — exit 0, script output equals `laconic measure`, and reported cost split sums to 100.00% within tolerance.
REVIEW:
Per PR:
- Scope matches its purpose; contracts match the reconciled plan; behavior is meaningfully tested.
- Failures are loud; security, data safety, and rollback requirements are addressed where relevant. Confirm no real transcript content is committed.
- History is atomic, conventional, attribution-free, and free of unrelated formatting churn.
- PR-specific verification output is captured.
Whole stack:
- Bases form one valid stack; cumulative acceptance and integration hold; CI is green; no regression coverage is removed without replacement.
- The docs-only root, when present, is reviewed and green before dependent code PRs.
- Report PR URLs, bases, verification, risks, manual gates, and review completion.
FINAL VERDICTS:
- Report the design verdict before the merge verdict.
- Then report exactly one merge verdict: `GO — RELEASE: unversioned — RELEASE PREP: pending` or `NO-GO — RELEASE: unversioned — REASON: <blocking gate>`.
- `GO` requires `DESIGN GO`, every PR correctly based/reviewed/green, local verification, and full milestone acceptance. `NO-GO` applies to pending or failed checks, incomplete review, scope drift, ambiguous readiness, manual gates, or unresolved release target.
DONE: design verdict with evidence; when authorized, a reviewed stack with a release-aware merge verdict and evidence.
```

---

### M3 — Handle ledger and the recoverability invariant

```text
/goal Deliver milestone M3 (Handle ledger and the recoverability invariant) from DEVELOPMENT_PLAN.md as a reviewed stack of PRs.

CONTEXT: DEVELOPMENT_PLAN.md §6 M3 + docs/system-design.md §2.1 and §5.1 + docs/overview.md §3.1 and §4. Preconditions: M1 merged. Repo: uv, ruff, mypy --strict, pytest, CI from M1.
OBJECTIVE: Provide the content-addressed store that makes every elision reversible and prove the two invariants the system rests on: recoverability and deterministic encoding. Acceptance: expand returns exact registered bytes for arbitrary generated payloads and exact lines for spans; byte-identical content under the same subject reuses its handle; identical inputs produce identical handles and encodings across processes; unknown handle raises rather than returning empty.
RELEASE TRAIN: target=unversioned; included milestones=M1-M14; preparation trigger=all included milestones externally merged; required artifacts=none per the release-policy GAP in DEVELOPMENT_PLAN.md §2; release verification=uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest -q && uv run laconic gates --corpus tests/corpus; publication=not requested.

PRE-IMPLEMENTATION DESIGN GATE:
1. Read this milestone, its source-map rows, current prompt, and `.docs/DEVELOPMENT_PLAN_HISTORY.md` when present.
2. Inspect the current codebase plus merged predecessor diffs, merged predecessor PR outcomes, CI/check evidence, and predecessor verification output.
3. Revalidate objective, interfaces, dependencies, acceptance, verification, risks, release train, and every listed dependent milestone: M4, M5, M6, M7, M8, M9, M10, M11, M12, M13, M14. Confirm the schema in docs/system-design.md §5.1 is still the intended shape and that the handle format remains model-typeable.
4. Append one ledger entry: timestamp, milestone, decision, trigger, evidence, plan/prompt sections changed, downstream impact, and implementation authorization.
5. If no material mismatch exists, report `DESIGN GO — PLAN REVISION: none`; this authorizes implementation.
6. If a mismatch exists, update both authoritative artifacts for M3 and every affected future milestone, append the revision ID, and report `DESIGN GO — PLAN REVISION: <entry IDs>`. This blocks product-code work until the reconciliation prerequisite merges.
7. If validity cannot be established, report `DESIGN NO-GO — REASON: <evidence>` and stop. After a reconciliation PR merges, repeat this gate and require `DESIGN GO — PLAN REVISION: none` before implementation.

RECONCILIATION RULE: A material revision opens `docs(plan): reconcile M3 design` as a docs-only prerequisite PR. It contains no product code, must be reviewed, green, and externally merged before any code PR, and must not be folded into an implementation PR.

PLANNED STACK (refine only to keep PRs reviewable):
0. Conditional prerequisite `docs(plan): reconcile M3 design` — scope: authoritative plan/prompt updates only; gate: reviewed, green, and merged before the implementation stack.
1. PR-1 schema and storage — scope: SQLite schema for observations and compactions, zstd raw storage, connection lifecycle; commits: feat(ledger): add schema and storage layer; verification: uv run pytest tests/test_ledger.py -q -k schema
2. PR-2 handle minting and dedup, on PR-1 — scope: register, get, handle format, dedup by subject and content_sha; commits: feat(ledger): add handle minting and content dedup; verification: uv run pytest tests/test_ledger.py -q
3. PR-3 expansion, on PR-2 — scope: expand for bare and spanned handles, loud failure on unknown handle; commits: feat(ledger): add handle expansion with span support; verification: uv run pytest tests/test_ledger.py -q -k expand
4. PR-4 invariant suites, on PR-3 — scope: property-based recoverability tests and determinism tests; commits: test(ledger): add property-based recoverability suite, test(ledger): add determinism suite; verification: uv run pytest tests/test_recoverability.py tests/test_determinism.py -q

CONSTRAINTS: no encoder, residency policy, or rendering behavior; the recoverability and determinism suites are release-blocking from this milestone onward and must not be marked xfail or skipped; minimal dependencies; repo style.
VERIFICATION (must pass): `uv run pytest tests/test_ledger.py tests/test_recoverability.py tests/test_determinism.py -q` — exit 0.
REVIEW:
Per PR:
- Scope matches its purpose; contracts match the reconciled plan; behavior is meaningfully tested.
- Failures are loud; security, data safety, and rollback requirements are addressed where relevant.
- History is atomic, conventional, attribution-free, and free of unrelated formatting churn.
- PR-specific verification output is captured.
Whole stack:
- Bases form one valid stack; cumulative acceptance and integration hold; CI is green; no regression coverage is removed without replacement.
- Confirm the property-based suites actually fail against a deliberately lossy ledger; a suite that cannot fail is not evidence.
- The docs-only root, when present, is reviewed and green before dependent code PRs.
- Report PR URLs, bases, verification, risks, manual gates, and review completion.
FINAL VERDICTS:
- Report the design verdict before the merge verdict.
- Then report exactly one merge verdict: `GO — RELEASE: unversioned — RELEASE PREP: pending` or `NO-GO — RELEASE: unversioned — REASON: <blocking gate>`.
- `GO` requires `DESIGN GO`, every PR correctly based/reviewed/green, local verification, and full milestone acceptance. `NO-GO` applies to pending or failed checks, incomplete review, scope drift, ambiguous readiness, manual gates, or unresolved release target.
DONE: design verdict with evidence; when authorized, a reviewed stack with a release-aware merge verdict and evidence.
```

---

### M4 — File observation encoder

```text
/goal Deliver milestone M4 (File observation encoder) from DEVELOPMENT_PLAN.md as a reviewed stack of PRs.

CONTEXT: DEVELOPMENT_PLAN.md §6 M4 + docs/system-design.md §2.2 + docs/overview.md §2.3 and §3.2. Preconditions: M2 and M3 merged. Repo: fixture corpus, measurement command, and ledger with recoverability and determinism suites in place.
OBJECTIVE: Deliver the single largest lever — replace whole-file reads with a structural outline plus the requested span, recoverably. Acceptance: encoded read volume on the fixture corpus is materially below raw with every elided region recoverable; a file type with no available grammar degrades to head/tail scoping and never raises; encoding is deterministic; a declared edit-target region is never elided.
RELEASE TRAIN: target=unversioned; included milestones=M1-M14; preparation trigger=all included milestones externally merged; required artifacts=none per the release-policy GAP in DEVELOPMENT_PLAN.md §2; release verification=uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest -q && uv run laconic gates --corpus tests/corpus; publication=not requested.

PRE-IMPLEMENTATION DESIGN GATE:
1. Read this milestone, its source-map rows, current prompt, and `.docs/DEVELOPMENT_PLAN_HISTORY.md` when present.
2. Inspect the current codebase plus merged predecessor diffs, merged predecessor PR outcomes, CI/check evidence, and predecessor verification output.
3. Revalidate objective, interfaces, dependencies, acceptance, verification, risks, release train, and every listed dependent milestone: M5, M8, M9, M12, M13, M14. Confirm the whale-read distribution in docs/overview.md §2.3 still motivates span scoping against the current corpus. Resolve the tree-sitter grammar-coverage GAP in DEVELOPMENT_PLAN.md §2 by fixing a documented minimum grammar set.
4. Append one ledger entry: timestamp, milestone, decision, trigger, evidence, plan/prompt sections changed, downstream impact, and implementation authorization.
5. If no material mismatch exists, report `DESIGN GO — PLAN REVISION: none`; this authorizes implementation.
6. If a mismatch exists, update both authoritative artifacts for M4 and every affected future milestone, append the revision ID, and report `DESIGN GO — PLAN REVISION: <entry IDs>`. This blocks product-code work until the reconciliation prerequisite merges.
7. If validity cannot be established, report `DESIGN NO-GO — REASON: <evidence>` and stop. After a reconciliation PR merges, repeat this gate and require `DESIGN GO — PLAN REVISION: none` before implementation.

RECONCILIATION RULE: A material revision opens `docs(plan): reconcile M4 design` as a docs-only prerequisite PR. It contains no product code, must be reviewed, green, and externally merged before any code PR, and must not be folded into an implementation PR.

PLANNED STACK (refine only to keep PRs reviewable):
0. Conditional prerequisite `docs(plan): reconcile M4 design` — scope: authoritative plan/prompt updates only; gate: reviewed, green, and merged before the implementation stack.
1. PR-1 outliner interface and fallback — scope: codec/outline.py protocol plus head/tail fallback that always succeeds; commits: feat(outline): add outliner protocol and safe fallback; verification: uv run pytest tests/test_outline.py -q -k fallback
2. PR-2 tree-sitter outlining, on PR-1 — scope: grammar loading for the documented minimum set, symbol extraction; commits: feat(outline): add tree-sitter symbol extraction; verification: uv run pytest tests/test_outline.py -q
3. PR-3 span resolution, on PR-2 — scope: resolve requested span from the request, outline, and file length; commits: feat(encoders): add span resolution; verification: uv run pytest tests/test_encoders.py -q -k span
4. PR-4 file encoder, on PR-3 — scope: codec/encoders/file.py emitting outline plus span, ledger registration; commits: feat(encoders): add file observation encoder; verification: uv run pytest tests/test_encoders.py -q -k file
5. PR-5 edit-target protection and reduction reporting, on PR-4 — scope: never-elide-target rule, reduction reporting via laconic measure --codec on; commits: feat(encoders): never elide declared edit targets, feat(cli): report codec reduction; verification: uv run laconic measure tests/corpus --codec on --report reduction

CONSTRAINTS: do not claim net savings in this milestone — induced-read accounting belongs to M8; no command, search, or action encoding; the fallback path must never raise; minimal dependencies; repo style.
VERIFICATION (must pass): `uv run pytest tests/test_encoders.py -q -k file && uv run pytest tests/test_recoverability.py -q && uv run laconic measure tests/corpus --codec on --report reduction` — exit 0, recoverability suite green.
REVIEW:
Per PR:
- Scope matches its purpose; contracts match the reconciled plan; behavior is meaningfully tested.
- Failures are loud except on the fallback path, which must degrade silently and safely; security, data safety, and rollback requirements are addressed where relevant.
- History is atomic, conventional, attribution-free, and free of unrelated formatting churn.
- PR-specific verification output is captured.
Whole stack:
- Bases form one valid stack; cumulative acceptance and integration hold; CI is green; no regression coverage is removed without replacement.
- Confirm recoverability holds for every elision the encoder produces, including outline-only encodings.
- The docs-only root, when present, is reviewed and green before dependent code PRs.
- Report PR URLs, bases, verification, risks, manual gates, and review completion.
FINAL VERDICTS:
- Report the design verdict before the merge verdict.
- Then report exactly one merge verdict: `GO — RELEASE: unversioned — RELEASE PREP: pending` or `NO-GO — RELEASE: unversioned — REASON: <blocking gate>`.
- `GO` requires `DESIGN GO`, every PR correctly based/reviewed/green, local verification, and full milestone acceptance. `NO-GO` applies to pending or failed checks, incomplete review, scope drift, ambiguous readiness, manual gates, or unresolved release target.
DONE: design verdict with evidence; when authorized, a reviewed stack with a release-aware merge verdict and evidence.
```

---

### M5 — Command, search, and fallback encoders

```text
/goal Deliver milestone M5 (Command, search, and fallback encoders) from DEVELOPMENT_PLAN.md as a reviewed stack of PRs.

CONTEXT: DEVELOPMENT_PLAN.md §6 M5 + docs/system-design.md §2.2. Preconditions: M3 and M4 merged. Repo: ledger and file encoder in place.
OBJECTIVE: Cover the remaining observation shapes with error-salient elision and provide a dispatch layer that always has a safe default. Acceptance: stderr, non-zero exit context, tracebacks, and failing assertions are never elided, enforced by property test; every elided region emits a visible marker and an addressable handle; an unrecognized tool encodes via fallback rather than raising; duplicate-line collapse preserves ordering of surviving lines.
RELEASE TRAIN: target=unversioned; included milestones=M1-M14; preparation trigger=all included milestones externally merged; required artifacts=none per the release-policy GAP in DEVELOPMENT_PLAN.md §2; release verification=uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest -q && uv run laconic gates --corpus tests/corpus; publication=not requested.

PRE-IMPLEMENTATION DESIGN GATE:
1. Read this milestone, its source-map rows, current prompt, and `.docs/DEVELOPMENT_PLAN_HISTORY.md` when present.
2. Inspect the current codebase plus merged predecessor diffs, merged predecessor PR outcomes, CI/check evidence, and predecessor verification output.
3. Revalidate objective, interfaces, dependencies, acceptance, verification, risks, release train, and every listed dependent milestone: M9, M12, M13. Confirm the three elision rules in docs/system-design.md §2.2 are unchanged and that measured command-output duplication has not shifted enough to reprioritize.
4. Append one ledger entry: timestamp, milestone, decision, trigger, evidence, plan/prompt sections changed, downstream impact, and implementation authorization.
5. If no material mismatch exists, report `DESIGN GO — PLAN REVISION: none`; this authorizes implementation.
6. If a mismatch exists, update both authoritative artifacts for M5 and every affected future milestone, append the revision ID, and report `DESIGN GO — PLAN REVISION: <entry IDs>`. This blocks product-code work until the reconciliation prerequisite merges.
7. If validity cannot be established, report `DESIGN NO-GO — REASON: <evidence>` and stop. After a reconciliation PR merges, repeat this gate and require `DESIGN GO — PLAN REVISION: none` before implementation.

RECONCILIATION RULE: A material revision opens `docs(plan): reconcile M5 design` as a docs-only prerequisite PR. It contains no product code, must be reviewed, green, and externally merged before any code PR, and must not be folded into an implementation PR.

PLANNED STACK (refine only to keep PRs reviewable):
0. Conditional prerequisite `docs(plan): reconcile M5 design` — scope: authoritative plan/prompt updates only; gate: reviewed, green, and merged before the implementation stack.
1. PR-1 fallback encoder and dispatch — scope: codec/encoders/fallback.py, codec/observe.py name dispatch; commits: feat(encoders): add fallback encoder, feat(observe): add encoder dispatch; verification: uv run pytest tests/test_encoders.py -q -k fallback
2. PR-2 command encoder, on PR-1 — scope: error-salient head/tail elision, duplicate-line collapse, error preservation; commits: feat(encoders): add error-salient command encoder; verification: uv run pytest tests/test_encoders.py -q -k command
3. PR-3 structured recognizers, on PR-2 — scope: test-runner and build-log summarization; commits: feat(encoders): summarize recognized command output; verification: uv run pytest tests/test_encoders.py -q -k recognizer
4. PR-4 search encoder, on PR-3 — scope: codec/encoders/search.py path interning and tabular output; commits: feat(encoders): add search encoder with path interning; verification: uv run pytest tests/test_encoders.py -q -k search

CONSTRAINTS: error preservation is non-negotiable and must be property-tested, not example-tested; every elision addressable; no residency, action encoding, or surface work; minimal dependencies; repo style.
VERIFICATION (must pass): `uv run pytest tests/test_encoders.py -q && uv run pytest tests/test_recoverability.py -q` — exit 0.
REVIEW:
Per PR:
- Scope matches its purpose; contracts match the reconciled plan; behavior is meaningfully tested.
- Failures are loud; confirm no encoder can drop an error line under any generated input.
- History is atomic, conventional, attribution-free, and free of unrelated formatting churn.
- PR-specific verification output is captured.
Whole stack:
- Bases form one valid stack; cumulative acceptance and integration hold; CI is green; no regression coverage is removed without replacement.
- The docs-only root, when present, is reviewed and green before dependent code PRs.
- Report PR URLs, bases, verification, risks, manual gates, and review completion.
FINAL VERDICTS:
- Report the design verdict before the merge verdict.
- Then report exactly one merge verdict: `GO — RELEASE: unversioned — RELEASE PREP: pending` or `NO-GO — RELEASE: unversioned — REASON: <blocking gate>`.
- `GO` requires `DESIGN GO`, every PR correctly based/reviewed/green, local verification, and full milestone acceptance. `NO-GO` applies to pending or failed checks, incomplete review, scope drift, ambiguous readiness, manual gates, or unresolved release target.
DONE: design verdict with evidence; when authorized, a reviewed stack with a release-aware merge verdict and evidence.
```

---

### M6 — Action codec with symbol anchoring

```text
/goal Deliver milestone M6 (Action codec with symbol anchoring) from DEVELOPMENT_PLAN.md as a reviewed stack of PRs.

CONTEXT: DEVELOPMENT_PLAN.md §6 M6 + docs/system-design.md §2.4 + docs/overview.md §3.4. Preconditions: M3 merged. Repo: ledger in place.
OBJECTIVE: Re-express edits as deltas anchored to ledger handles and symbol names so they survive line drift from earlier edits in the same session. Acceptance: an anchored edit applies correctly after earlier edits shift line numbers; an ambiguous anchor resolves via explicit occurrence index or fails loudly; a stale anchor fails loudly; round-tripping an edit produces a byte-identical result to the direct edit.
RELEASE TRAIN: target=unversioned; included milestones=M1-M14; preparation trigger=all included milestones externally merged; required artifacts=none per the release-policy GAP in DEVELOPMENT_PLAN.md §2; release verification=uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest -q && uv run laconic gates --corpus tests/corpus; publication=not requested.

PRE-IMPLEMENTATION DESIGN GATE:
1. Read this milestone, its source-map rows, current prompt, and `.docs/DEVELOPMENT_PLAN_HISTORY.md` when present.
2. Inspect the current codebase plus merged predecessor diffs, merged predecessor PR outcomes, CI/check evidence, and predecessor verification output.
3. Revalidate objective, interfaces, dependencies, acceptance, verification, risks, release train, and every listed dependent milestone: M9, M12, M13. Confirm the argument-volume distribution in docs/system-design.md §2.4 still justifies anchoring and that the host agent's edit tool contract is unchanged.
4. Append one ledger entry: timestamp, milestone, decision, trigger, evidence, plan/prompt sections changed, downstream impact, and implementation authorization.
5. If no material mismatch exists, report `DESIGN GO — PLAN REVISION: none`; this authorizes implementation.
6. If a mismatch exists, update both authoritative artifacts for M6 and every affected future milestone, append the revision ID, and report `DESIGN GO — PLAN REVISION: <entry IDs>`. This blocks product-code work until the reconciliation prerequisite merges.
7. If validity cannot be established, report `DESIGN NO-GO — REASON: <evidence>` and stop. After a reconciliation PR merges, repeat this gate and require `DESIGN GO — PLAN REVISION: none` before implementation.

RECONCILIATION RULE: A material revision opens `docs(plan): reconcile M6 design` as a docs-only prerequisite PR. It contains no product code, must be reviewed, green, and externally merged before any code PR, and must not be folded into an implementation PR.

PLANNED STACK (refine only to keep PRs reviewable):
0. Conditional prerequisite `docs(plan): reconcile M6 design` — scope: authoritative plan/prompt updates only; gate: reviewed, green, and merged before the implementation stack.
1. PR-1 anchored-edit model — scope: codec/act.py AnchoredEdit dataclass, anchor resolution against a ledger record; commits: feat(act): add anchored edit model; verification: uv run pytest tests/test_act.py -q -k model
2. PR-2 materialization, on PR-1 — scope: to_tool_input against current file state, occurrence disambiguation, loud failure on ambiguity or staleness; commits: feat(act): materialize anchored edits against current state; verification: uv run pytest tests/test_act.py -q
3. PR-3 drift resilience suite, on PR-2 — scope: tests applying sequential edits that shift line numbers, round-trip byte-equality tests; commits: test(act): add line-drift and round-trip suites; verification: uv run pytest tests/test_act.py -q -k drift

CONSTRAINTS: never apply a best-guess anchor — ambiguity must fail loudly; no observation encoding, residency, or surface work; minimal dependencies; repo style.
VERIFICATION (must pass): `uv run pytest tests/test_act.py -q` — exit 0.
REVIEW:
Per PR:
- Scope matches its purpose; contracts match the reconciled plan; behavior is meaningfully tested.
- Failures are loud; confirm no code path silently selects among multiple anchor matches.
- History is atomic, conventional, attribution-free, and free of unrelated formatting churn.
- PR-specific verification output is captured.
Whole stack:
- Bases form one valid stack; cumulative acceptance and integration hold; CI is green; no regression coverage is removed without replacement.
- The docs-only root, when present, is reviewed and green before dependent code PRs.
- Report PR URLs, bases, verification, risks, manual gates, and review completion.
FINAL VERDICTS:
- Report the design verdict before the merge verdict.
- Then report exactly one merge verdict: `GO — RELEASE: unversioned — RELEASE PREP: pending` or `NO-GO — RELEASE: unversioned — REASON: <blocking gate>`.
- `GO` requires `DESIGN GO`, every PR correctly based/reviewed/green, local verification, and full milestone acceptance. `NO-GO` applies to pending or failed checks, incomplete review, scope drift, ambiguous readiness, manual gates, or unresolved release target.
DONE: design verdict with evidence; when authorized, a reviewed stack with a release-aware merge verdict and evidence.
```

---

### M7 — Residency manager

```text
/goal Deliver milestone M7 (Residency manager) from DEVELOPMENT_PLAN.md as a reviewed stack of PRs.

CONTEXT: DEVELOPMENT_PLAN.md §6 M7 + docs/system-design.md §2.3, §5.1, §9.4 + docs/overview.md §3.3. Preconditions: M2 and M3 merged. Repo: cost model and ledger in place.
OBJECTIVE: Manage what stays resident in the prefix, defaulting to append-only and permitting compaction only when the cache arithmetic proves it pays. Acceptance: breakeven_turns reproduces the table in docs/system-design.md §2.3 exactly for every listed pair; compaction is declined and logged when projected turns are below break-even; compaction is declined when no session-length estimate is available; append-only mode never mutates an existing prefix entry.
RELEASE TRAIN: target=unversioned; included milestones=M1-M14; preparation trigger=all included milestones externally merged; required artifacts=none per the release-policy GAP in DEVELOPMENT_PLAN.md §2; release verification=uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest -q && uv run laconic gates --corpus tests/corpus; publication=not requested.

HUMAN REVIEW GATE: Do not merge or run destructive paths unattended until a human reviews dry-run output, rollback notes, and audit/tombstone logging. Compaction rewrites a cached prefix and can raise a real bill.

PRE-IMPLEMENTATION DESIGN GATE:
1. Read this milestone, its source-map rows, current prompt, and `.docs/DEVELOPMENT_PLAN_HISTORY.md` when present.
2. Inspect the current codebase plus merged predecessor diffs, merged predecessor PR outcomes, CI/check evidence, and predecessor verification output.
3. Revalidate objective, interfaces, dependencies, acceptance, verification, risks, release train, and every listed dependent milestone: M9, M12, M13. Confirm published cache read and write multipliers are unchanged, since the entire break-even formula derives from them.
4. Append one ledger entry: timestamp, milestone, decision, trigger, evidence, plan/prompt sections changed, downstream impact, and implementation authorization.
5. If no material mismatch exists, report `DESIGN GO — PLAN REVISION: none`; this authorizes implementation.
6. If a mismatch exists, update both authoritative artifacts for M7 and every affected future milestone, append the revision ID, and report `DESIGN GO — PLAN REVISION: <entry IDs>`. This blocks product-code work until the reconciliation prerequisite merges.
7. If validity cannot be established, report `DESIGN NO-GO — REASON: <evidence>` and stop. After a reconciliation PR merges, repeat this gate and require `DESIGN GO — PLAN REVISION: none` before implementation.

RECONCILIATION RULE: A material revision opens `docs(plan): reconcile M7 design` as a docs-only prerequisite PR. It contains no product code, must be reviewed, green, and externally merged before any code PR, and must not be folded into an implementation PR.

PLANNED STACK (refine only to keep PRs reviewable):
0. Conditional prerequisite `docs(plan): reconcile M7 design` — scope: authoritative plan/prompt updates only; gate: reviewed, green, and merged before the implementation stack.
1. PR-1 break-even arithmetic — scope: residency.py breakeven_turns and cache multipliers sourced from costs.py; commits: feat(residency): add cache break-even arithmetic; verification: uv run pytest tests/test_residency.py -q -k breakeven
2. PR-2 append-only mode, on PR-1 — scope: default policy, immutability of existing prefix entries; commits: feat(residency): add append-only residency mode; verification: uv run pytest tests/test_residency.py -q -k append
3. PR-3 compaction with decline logging, on PR-2 — scope: opt-in compaction, compactions table writes including declines with reasons, session-length estimation; commits: feat(residency): add gated compaction with audit logging; verification: uv run pytest tests/test_residency.py -q -k compact
4. PR-4 dry-run reporting, on PR-3 — scope: dry-run decision reporting through the residency API, showing the arithmetic and projected effect without mutating anything; commits: feat(residency): add compaction dry-run reporting; verification: uv run pytest tests/test_residency.py -q -k dryrun

CONSTRAINTS: append-only is the default and compaction must remain opt-in; a decision without a session-length estimate must decline, never guess; every accept and decline is logged with its arithmetic; no live-session application, which is M12; minimal dependencies; repo style.
VERIFICATION (must pass): `uv run pytest tests/test_residency.py -q` — exit 0 and break-even values matching docs/system-design.md §2.3.
REVIEW:
Per PR:
- Scope matches its purpose; contracts match the reconciled plan; behavior is meaningfully tested.
- Failures are loud; confirm the audit trail records declined compactions with their reasons, not only accepted ones.
- History is atomic, conventional, attribution-free, and free of unrelated formatting churn.
- PR-specific verification output is captured.
Whole stack:
- Bases form one valid stack; cumulative acceptance and integration hold; CI is green; no regression coverage is removed without replacement.
- Human review gate satisfied: dry-run output, rollback notes, and audit logging reviewed by a human before merge.
- The docs-only root, when present, is reviewed and green before dependent code PRs.
- Report PR URLs, bases, verification, risks, manual gates, and review completion.
FINAL VERDICTS:
- Report the design verdict before the merge verdict.
- Then report exactly one merge verdict: `GO — RELEASE: unversioned — RELEASE PREP: pending` or `NO-GO — RELEASE: unversioned — REASON: <blocking gate>`.
- `GO` requires `DESIGN GO`, every PR correctly based/reviewed/green, local verification, full milestone acceptance, and a satisfied human review gate. `NO-GO` applies to pending or failed checks, incomplete review, scope drift, ambiguous readiness, manual gates, or unresolved release target.
DONE: design verdict with evidence; when authorized, a reviewed stack with a release-aware merge verdict and evidence.
```

---

### M8 — Replay engine and action equivalence

```text
/goal Deliver milestone M8 (Replay engine and action equivalence) from DEVELOPMENT_PLAN.md as a reviewed stack of PRs.

CONTEXT: DEVELOPMENT_PLAN.md §6 M8 + docs/system-design.md §2.6 + docs/overview.md §3.6 and §8.1. Preconditions: M2 and M4 merged. Repo: cost model, corpus ingest, ledger, and file encoder in place.
OBJECTIVE: Measure the codec counterfactually against recorded sessions, reporting net cost including induced follow-up reads, and judge whether the agent behaves identically. Acceptance: replay with the codec disabled reproduces recorded baseline cost within a stated tolerance; reported savings are net of induced reads and gross-only reporting is unrepresentable through the public API; structural equivalence is decided without a model; CI uses committed provenance-tagged recorded responses and cannot enter live mode; live replay requires explicit opt-in, configured model identifier, and per-run cost cap, and captures provenance-tagged response artifacts for later committed fixtures; the model judge is off by default with explicit sampling rate and budget.
RELEASE TRAIN: target=unversioned; included milestones=M1-M14; preparation trigger=all included milestones externally merged; required artifacts=none per the release-policy GAP in DEVELOPMENT_PLAN.md §2; release verification=uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest -q && uv run laconic gates --corpus tests/corpus; publication=not requested.

PRE-IMPLEMENTATION DESIGN GATE:
1. Read this milestone, its source-map rows, current prompt, and `.docs/DEVELOPMENT_PLAN_HISTORY.md` when present.
2. Inspect the current codebase plus merged predecessor diffs, merged predecessor PR outcomes, CI/check evidence, and predecessor verification output.
3. Revalidate objective, interfaces, dependencies, acceptance, verification, risks, release train, and every listed dependent milestone: M9, M12, M13, M14. Confirm the corpus is still representative and that induced-read accounting matches the primary risk in docs/overview.md §8.1. Resolve the judge-budget and live-replay CI `> GAP:` entries in DEVELOPMENT_PLAN.md §2 by fixing recorded-response provenance, explicit live opt-in, model identifier, cost cap, sampling rate, budget, and offline default.
4. Append one ledger entry: timestamp, milestone, decision, trigger, evidence, plan/prompt sections changed, downstream impact, and implementation authorization.
5. If no material mismatch exists, report `DESIGN GO — PLAN REVISION: none`; this authorizes implementation.
6. If a mismatch exists, update both authoritative artifacts for M8 and every affected future milestone, append the revision ID, and report `DESIGN GO — PLAN REVISION: <entry IDs>`. This blocks product-code work until the reconciliation prerequisite merges.
7. If validity cannot be established, report `DESIGN NO-GO — REASON: <evidence>` and stop. After a reconciliation PR merges, repeat this gate and require `DESIGN GO — PLAN REVISION: none` before implementation.

RECONCILIATION RULE: A material revision opens `docs(plan): reconcile M8 design` as a docs-only prerequisite PR. It contains no product code, must be reviewed, green, and externally merged before any code PR, and must not be folded into an implementation PR.

PLANNED STACK (refine only to keep PRs reviewable):
0. Conditional prerequisite `docs(plan): reconcile M8 design` — scope: authoritative plan/prompt updates only; gate: reviewed, green, and merged before the implementation stack.
1. PR-1 replay engine core — scope: replay/engine.py turn iteration, codec on/off modes, baseline reproduction, provenance-tagged recorded-response replay for CI, explicit live-mode boundary and artifact capture; commits: feat(replay): add counterfactual replay engine; verification: uv run laconic replay tests/corpus --codec off --assert-baseline
2. PR-2 net cost accounting, on PR-1 — scope: induced-read attribution, net-only public API; commits: feat(replay): account net cost including induced reads; verification: uv run pytest tests/test_replay.py -q -k net
3. PR-3 structural equivalence, on PR-2 — scope: replay/equivalence.py tool, target, and anchor comparison without a model; commits: feat(replay): add structural action equivalence; verification: uv run pytest tests/test_replay.py -q -k equivalence
4. PR-4 opt-in model judge, on PR-3 — scope: semantic fallback off by default, explicit sampling rate and budget, verdict sampling for audit; commits: feat(replay): add opt-in semantic equivalence judge; verification: uv run pytest tests/test_replay.py -q -k judge
5. PR-5 replay CLI and reporting, on PR-4 — scope: laconic replay output format, per-session deltas, explicit live flag and cost-cap validation; commits: feat(cli): add replay command and reporting; verification: uv run laconic replay tests/corpus --format json

CONSTRAINTS: gross-only savings must be unrepresentable through the public API — this is a structural requirement, not a reporting convention; the model judge must be off by default; CI must use recorded-response replay and reject live mode; no threshold enforcement, which is M9; minimal dependencies; repo style.
VERIFICATION (must pass): `uv run pytest tests/test_replay.py -q && uv run laconic replay tests/corpus --codec off --assert-baseline` — exit 0, baseline reproduced within tolerance.
REVIEW:
Per PR:
- Scope matches its purpose; contracts match the reconciled plan; behavior is meaningfully tested.
- Failures are loud; confirm there is no public path that reports savings without induced-read accounting.
- History is atomic, conventional, attribution-free, and free of unrelated formatting churn.
- PR-specific verification output is captured.
Whole stack:
- Bases form one valid stack; cumulative acceptance and integration hold; CI is green; no regression coverage is removed without replacement.
- The docs-only root, when present, is reviewed and green before dependent code PRs.
- Report PR URLs, bases, verification, risks, manual gates, and review completion.
FINAL VERDICTS:
- Report the design verdict before the merge verdict.
- Then report exactly one merge verdict: `GO — RELEASE: unversioned — RELEASE PREP: pending` or `NO-GO — RELEASE: unversioned — REASON: <blocking gate>`.
- `GO` requires `DESIGN GO`, every PR correctly based/reviewed/green, local verification, and full milestone acceptance. `NO-GO` applies to pending or failed checks, incomplete review, scope drift, ambiguous readiness, manual gates, or unresolved release target.
DONE: design verdict with evidence; when authorized, a reviewed stack with a release-aware merge verdict and evidence.
```

---

### M9 — Gate runner

```text
/goal Deliver milestone M9 (Gate runner) from DEVELOPMENT_PLAN.md as a reviewed stack of PRs.

CONTEXT: DEVELOPMENT_PLAN.md §6 M9 + docs/overview.md §6.3 + docs/system-design.md §4. Preconditions: M5, M6, M7, M8 merged. Repo: full codec, residency, and replay harness in place.
OBJECTIVE: Turn the five pre-registered gates into an executable, CI-enforced verdict so a kill condition is detected automatically. Acceptance: K1, K2, K4, K5 each report value, threshold, and pass/fail; a kill condition exits non-zero; thresholds come from a single declared source; K3 is reported as manual and not silently omitted; K5 recorded responses are committed and provenance-tagged, K5 makes no model call in CI, and CI runs the suite against tests/corpus using M8 recorded-response replay only.
RELEASE TRAIN: target=unversioned; included milestones=M1-M14; preparation trigger=all included milestones externally merged; required artifacts=none per the release-policy GAP in DEVELOPMENT_PLAN.md §2; release verification=uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest -q && uv run laconic gates --corpus tests/corpus; publication=not requested.

PRE-IMPLEMENTATION DESIGN GATE:
1. Read this milestone, its source-map rows, current prompt, and `.docs/DEVELOPMENT_PLAN_HISTORY.md` when present.
2. Inspect the current codebase plus merged predecessor diffs, merged predecessor PR outcomes, CI/check evidence, and predecessor verification output.
3. Revalidate objective, interfaces, dependencies, acceptance, verification, risks, release train, and every listed dependent milestone: M12, M13. Re-read the threshold source, confirm the fixture corpus is representative, and confirm CI invokes M8 recorded-response replay while rejecting live mode.
4. Append one ledger entry: timestamp, milestone, decision, trigger, evidence, plan/prompt sections changed, downstream impact, and implementation authorization.
5. If no material mismatch exists, report `DESIGN GO — PLAN REVISION: none`; this authorizes implementation.
6. If a mismatch exists, update both authoritative artifacts for M9 and every affected future milestone, append the revision ID, and report `DESIGN GO — PLAN REVISION: <entry IDs>`. This blocks product-code work until the reconciliation prerequisite merges.
7. If validity cannot be established, report `DESIGN NO-GO — REASON: <evidence>` and stop. After a reconciliation PR merges, repeat this gate and require `DESIGN GO — PLAN REVISION: none` before implementation.

KILL-CONDITION RULE: If the measured K1 net saving is below 15%, the design premise of M12 and M13 is invalidated. If K1 is at least 15% but below the 25% target, report K1 as failed but exit zero because no kill condition occurred; M12 and M13 remain blocked until a docs-only reconciliation resolves the target miss. In either case, report the number plainly, open a docs-only reconciliation PR updating M12, M13, and the critical path, and do not proceed to either milestone until it merges. A low K1 is a valid project outcome, not a failure to hide.

RECONCILIATION RULE: A material revision opens `docs(plan): reconcile M9 design` as a docs-only prerequisite PR. It contains no product code, must be reviewed, green, and externally merged before any code PR, and must not be folded into an implementation PR.

PLANNED STACK (refine only to keep PRs reviewable):
0. Conditional prerequisite `docs(plan): reconcile M9 design` — scope: authoritative plan/prompt updates only; gate: reviewed, green, and merged before the implementation stack.
1. PR-1 threshold source and gate protocol — scope: single declared threshold source, gate result type, machine-readable output; commits: feat(gates): add gate protocol and threshold source; verification: uv run pytest tests/test_gates.py -q -k protocol
2. PR-2 K1 and K2 gates, on PR-1 — scope: net cost reduction and action equivalence wired to the replay harness; commits: feat(gates): add K1 net-cost and K2 equivalence gates; verification: uv run laconic gates --corpus tests/corpus --only K1,K2
3. PR-3 K4 overhead gate and K5 benchmark harness, on PR-2 — scope: added-tokens-per-turn measurement, exact-match reasoning benchmark with codec on and off, committed provenance-tagged K5 recorded responses captured through M8; commits: feat(gates): add K4 overhead gate, feat(gates): add K5 reasoning benchmark; verification: uv run laconic gates --corpus tests/corpus --only K4,K5
4. PR-4 CI wiring and negative-control tests, on PR-3 — scope: workflow step in recorded-response mode, rejection of live mode in CI, tests proving each gate fails against a deliberately broken codec, K3 reported as manual; commits: ci: run gate suite, test(gates): add negative controls; verification: uv run pytest tests/test_gates.py -q && uv run laconic gates --corpus tests/corpus --format json

CONSTRAINTS: every gate needs a negative control proving it can fail; thresholds must not be duplicated per gate; K3 must appear in output as manual rather than being omitted; CI must use recorded-response replay and reject live mode; do not weaken a threshold to make a gate pass; minimal dependencies; repo style.
VERIFICATION (must pass): `uv run laconic gates --corpus tests/corpus --format json && uv run pytest tests/test_gates.py -q` — exit 0 when no kill condition occurs, including a 15%–<25% K1 failed target; exit non-zero on any kill condition.
REVIEW:
Per PR:
- Scope matches its purpose; contracts match the reconciled plan; behavior is meaningfully tested.
- Failures are loud; confirm no gate is green because it is weak — check its negative control.
- History is atomic, conventional, attribution-free, and free of unrelated formatting churn.
- PR-specific verification output is captured.
Whole stack:
- Bases form one valid stack; cumulative acceptance and integration hold; CI is green; no regression coverage is removed without replacement.
- Report the measured K1, K2, K4, and K5 values explicitly in the stack summary, whatever they are.
- The docs-only root, when present, is reviewed and green before dependent code PRs.
- Report PR URLs, bases, verification, risks, manual gates, and review completion.
FINAL VERDICTS:
- Report the design verdict before the merge verdict.
- Then report exactly one merge verdict: `GO — RELEASE: unversioned — RELEASE PREP: pending` or `NO-GO — RELEASE: unversioned — REASON: <blocking gate>`.
- `GO` requires `DESIGN GO`, every PR correctly based/reviewed/green, local verification, and full milestone acceptance. `NO-GO` applies to pending or failed checks, incomplete review, scope drift, ambiguous readiness, manual gates, or unresolved release target.
DONE: design verdict with evidence; the measured gate values; when authorized, a reviewed stack with a release-aware merge verdict and evidence.
```

---

### M10 — Deterministic renderer

```text
/goal Deliver milestone M10 (Deterministic renderer) from DEVELOPMENT_PLAN.md as a reviewed stack of PRs.

CONTEXT: DEVELOPMENT_PLAN.md §6 M10 + docs/system-design.md §2.5 + docs/overview.md §3.5 and §8.2. Preconditions: M2 and M3 merged. Repo: fixture corpus and ledger in place.
OBJECTIVE: Render a compact trace into prose from structural facts alone, with every claim traceable to its handle and nothing hallucinable. Acceptance: every rendered claim carries its source handle; with deterministic_only no model call is made, enforced by test; rendering is byte-identical across runs for identical input; laconic expand resolves bare and spanned handles.
RELEASE TRAIN: target=unversioned; included milestones=M1-M14; preparation trigger=all included milestones externally merged; required artifacts=none per the release-policy GAP in DEVELOPMENT_PLAN.md §2; release verification=uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest -q && uv run laconic gates --corpus tests/corpus; publication=not requested.

PRE-IMPLEMENTATION DESIGN GATE:
1. Read this milestone, its source-map rows, current prompt, and `.docs/DEVELOPMENT_PLAN_HISTORY.md` when present.
2. Inspect the current codebase plus merged predecessor diffs, merged predecessor PR outcomes, CI/check evidence, and predecessor verification output.
3. Revalidate objective, interfaces, dependencies, acceptance, verification, risks, release train, and every listed dependent milestone: M11, M14. Confirm the deterministic and generative split in docs/system-design.md §2.5 still bounds the placebic-explanation risk in docs/overview.md §8.2.
4. Append one ledger entry: timestamp, milestone, decision, trigger, evidence, plan/prompt sections changed, downstream impact, and implementation authorization.
5. If no material mismatch exists, report `DESIGN GO — PLAN REVISION: none`; this authorizes implementation.
6. If a mismatch exists, update both authoritative artifacts for M10 and every affected future milestone, append the revision ID, and report `DESIGN GO — PLAN REVISION: <entry IDs>`. This blocks product-code work until the reconciliation prerequisite merges.
7. If validity cannot be established, report `DESIGN NO-GO — REASON: <evidence>` and stop. After a reconciliation PR merges, repeat this gate and require `DESIGN GO — PLAN REVISION: none` before implementation.

RECONCILIATION RULE: A material revision opens `docs(plan): reconcile M10 design` as a docs-only prerequisite PR. It contains no product code, must be reviewed, green, and externally merged before any code PR, and must not be folded into an implementation PR.

PLANNED STACK (refine only to keep PRs reviewable):
0. Conditional prerequisite `docs(plan): reconcile M10 design` — scope: authoritative plan/prompt updates only; gate: reviewed, green, and merged before the implementation stack.
1. PR-1 trace assembly — scope: render/view.py assembling a turn range from the ledger; commits: feat(render): add trace assembly; verification: uv run pytest tests/test_render.py -q -k assembly
2. PR-2 deterministic templates, on PR-1 — scope: render/templates.py structural rendering with handle provenance on every claim; commits: feat(render): add deterministic structural templates; verification: uv run pytest tests/test_render.py -q -k template
3. PR-3 expand command, on PR-2 — scope: laconic expand for bare and spanned handles; commits: feat(cli): add expand command; verification: uv run laconic expand F1 --corpus tests/corpus
4. PR-4 view command and no-model guarantee, on PR-3 — scope: laconic view --turns, deterministic_only enforcement test; commits: feat(cli): add view command, test(render): assert no model call in deterministic mode; verification: uv run laconic view --turns 1-5 --corpus tests/corpus --deterministic-only

CONSTRAINTS: no model-generated text in this milestone; every rendered claim must carry provenance; rendering must never mutate the ledger or enter the agent's context; minimal dependencies; repo style.
VERIFICATION (must pass): `uv run pytest tests/test_render.py -q && uv run laconic view --turns 1-5 --corpus tests/corpus --deterministic-only` — exit 0, byte-identical across two runs.
REVIEW:
Per PR:
- Scope matches its purpose; contracts match the reconciled plan; behavior is meaningfully tested.
- Failures are loud; confirm no rendered assertion lacks a handle reference.
- History is atomic, conventional, attribution-free, and free of unrelated formatting churn.
- PR-specific verification output is captured.
Whole stack:
- Bases form one valid stack; cumulative acceptance and integration hold; CI is green; no regression coverage is removed without replacement.
- The docs-only root, when present, is reviewed and green before dependent code PRs.
- Report PR URLs, bases, verification, risks, manual gates, and review completion.
FINAL VERDICTS:
- Report the design verdict before the merge verdict.
- Then report exactly one merge verdict: `GO — RELEASE: unversioned — RELEASE PREP: pending` or `NO-GO — RELEASE: unversioned — REASON: <blocking gate>`.
- `GO` requires `DESIGN GO`, every PR correctly based/reviewed/green, local verification, and full milestone acceptance. `NO-GO` applies to pending or failed checks, incomplete review, scope drift, ambiguous readiness, manual gates, or unresolved release target.
DONE: design verdict with evidence; when authorized, a reviewed stack with a release-aware merge verdict and evidence.
```

---

### M11 — Generative narration

```text
/goal Deliver milestone M11 (Generative narration) from DEVELOPMENT_PLAN.md as a reviewed stack of PRs.

CONTEXT: DEVELOPMENT_PLAN.md §6 M11 + docs/system-design.md §2.5 + docs/overview.md §8.2. Preconditions: M10 merged. Repo: deterministic renderer in place.
OBJECTIVE: Add optional local-model connective prose for genuinely generative gaps, visually separated from resolved facts and degrading cleanly when absent. Acceptance: with provider none or unreachable, laconic view degrades to deterministic output and exits 0; generated spans are visually distinguishable from resolved facts; narration never mutates the ledger and never enters the agent's context, enforced by test.
RELEASE TRAIN: target=unversioned; included milestones=M1-M14; preparation trigger=all included milestones externally merged; required artifacts=none per the release-policy GAP in DEVELOPMENT_PLAN.md §2; release verification=uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest -q && uv run laconic gates --corpus tests/corpus; publication=not requested.

PRE-IMPLEMENTATION DESIGN GATE:
1. Read this milestone, its source-map rows, current prompt, and `.docs/DEVELOPMENT_PLAN_HISTORY.md` when present.
2. Inspect the current codebase plus merged predecessor diffs, merged predecessor PR outcomes, CI/check evidence, and predecessor verification output.
3. Revalidate objective, interfaces, dependencies, acceptance, verification, risks, release train, and every listed dependent milestone: M14. Confirm the human-factor risk framing in docs/overview.md §8.2 is unchanged and that K3 will measure this layer, not only the deterministic one.
4. Append one ledger entry: timestamp, milestone, decision, trigger, evidence, plan/prompt sections changed, downstream impact, and implementation authorization.
5. If no material mismatch exists, report `DESIGN GO — PLAN REVISION: none`; this authorizes implementation.
6. If a mismatch exists, update both authoritative artifacts for M11 and every affected future milestone, append the revision ID, and report `DESIGN GO — PLAN REVISION: <entry IDs>`. This blocks product-code work until the reconciliation prerequisite merges.
7. If validity cannot be established, report `DESIGN NO-GO — REASON: <evidence>` and stop. After a reconciliation PR merges, repeat this gate and require `DESIGN GO — PLAN REVISION: none` before implementation.

RECONCILIATION RULE: A material revision opens `docs(plan): reconcile M11 design` as a docs-only prerequisite PR. It contains no product code, must be reviewed, green, and externally merged before any code PR, and must not be folded into an implementation PR.

PLANNED STACK (refine only to keep PRs reviewable):
0. Conditional prerequisite `docs(plan): reconcile M11 design` — scope: authoritative plan/prompt updates only; gate: reviewed, green, and merged before the implementation stack.
1. PR-1 provider abstraction — scope: render/narrate.py provider protocol, configuration, none provider; commits: feat(narrate): add narration provider abstraction; verification: uv run pytest tests/test_narrate.py -q -k provider
2. PR-2 narration and degradation, on PR-1 — scope: connective prose for generative gaps, clean degradation on unreachable provider; commits: feat(narrate): add local-model narration with graceful degradation; verification: uv run laconic view --turns 1-5 --corpus tests/corpus --provider none
3. PR-3 provenance separation, on PR-2 — scope: visual distinction of generated versus resolved spans, isolation tests; commits: feat(render): distinguish generated from resolved spans, test(narrate): assert isolation from ledger and agent context; verification: uv run pytest tests/test_narrate.py -q

CONSTRAINTS: narration is optional and off-path; it must never block a turn, mutate the ledger, or enter the agent's context; do not claim the placebic-explanation risk is mitigated — M14 measures it; minimal dependencies; repo style.
VERIFICATION (must pass): `uv run pytest tests/test_narrate.py -q && uv run laconic view --turns 1-5 --corpus tests/corpus --provider none` — exit 0 with deterministic output.
REVIEW:
Per PR:
- Scope matches its purpose; contracts match the reconciled plan; behavior is meaningfully tested.
- Failures are loud except on the provider-unavailable path, which must degrade cleanly.
- History is atomic, conventional, attribution-free, and free of unrelated formatting churn.
- PR-specific verification output is captured.
Whole stack:
- Bases form one valid stack; cumulative acceptance and integration hold; CI is green; no regression coverage is removed without replacement.
- The docs-only root, when present, is reviewed and green before dependent code PRs.
- Report PR URLs, bases, verification, risks, manual gates, and review completion.
FINAL VERDICTS:
- Report the design verdict before the merge verdict.
- Then report exactly one merge verdict: `GO — RELEASE: unversioned — RELEASE PREP: pending` or `NO-GO — RELEASE: unversioned — REASON: <blocking gate>`.
- `GO` requires `DESIGN GO`, every PR correctly based/reviewed/green, local verification, and full milestone acceptance. `NO-GO` applies to pending or failed checks, incomplete review, scope drift, ambiguous readiness, manual gates, or unresolved release target.
DONE: design verdict with evidence; when authorized, a reviewed stack with a release-aware merge verdict and evidence.
```

---

### M12 — Surface A: hook integration

```text
/goal Deliver milestone M12 (Surface A: hook integration) from DEVELOPMENT_PLAN.md as a reviewed stack of PRs.

CONTEXT: DEVELOPMENT_PLAN.md §6 M12 + docs/system-design.md §3.1 and §6. Preconditions: M5, M6, M7 merged, and M9 reporting K1 at or above the 25% target. Repo: full codec and residency policy in place.
OBJECTIVE: Run the codec inside a real agent session through tool hooks, within a hard latency budget and failing open on any error. Acceptance: p99 encode latency under 40 ms on the fixture corpus; exceeding the budget passes the raw result through unchanged; any codec exception passes the raw result through unchanged, enforced by fault injection; laconic install is idempotent and reversible; laconic status reports ledger size, residency, and projected break-even.
RELEASE TRAIN: target=unversioned; included milestones=M1-M14; preparation trigger=all included milestones externally merged; required artifacts=none per the release-policy GAP in DEVELOPMENT_PLAN.md §2; release verification=uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest -q && uv run laconic gates --corpus tests/corpus; publication=not requested.

HUMAN REVIEW GATE: Do not merge or run destructive paths unattended until a human reviews dry-run output, rollback notes, and audit/tombstone logging. This surface intercepts and replaces real tool results in a live session.

PRE-IMPLEMENTATION DESIGN GATE:
1. Read this milestone, its source-map rows, current prompt, and `.docs/DEVELOPMENT_PLAN_HISTORY.md` when present.
2. Inspect the current codebase plus merged predecessor diffs, merged predecessor PR outcomes, CI/check evidence, and predecessor verification output.
3. Revalidate objective, interfaces, dependencies, acceptance, verification, risks, release train, and dependents: none. Confirm M9 reported K1 at or above 25%. A K1 below 15% invalidates this milestone; K1 from 15% through less than 25% requires a docs-only reconciliation before it proceeds. Confirm the host hook event schema is unchanged.
4. Append one ledger entry: timestamp, milestone, decision, trigger, evidence, plan/prompt sections changed, downstream impact, and implementation authorization.
5. If no material mismatch exists, report `DESIGN GO — PLAN REVISION: none`; this authorizes implementation.
6. If a mismatch exists, update both authoritative artifacts for M12 and every affected future milestone, append the revision ID, and report `DESIGN GO — PLAN REVISION: <entry IDs>`. This blocks product-code work until the reconciliation prerequisite merges.
7. If validity cannot be established, report `DESIGN NO-GO — REASON: <evidence>` and stop. After a reconciliation PR merges, repeat this gate and require `DESIGN GO — PLAN REVISION: none` before implementation.

RECONCILIATION RULE: A material revision opens `docs(plan): reconcile M12 design` as a docs-only prerequisite PR. It contains no product code, must be reviewed, green, and externally merged before any code PR, and must not be folded into an implementation PR.

PLANNED STACK (refine only to keep PRs reviewable):
0. Conditional prerequisite `docs(plan): reconcile M12 design` — scope: authoritative plan/prompt updates only; gate: reviewed, green, and merged before the implementation stack.
1. PR-1 hook adapter — scope: surfaces/hooks.py event parsing and result replacement behind an adapter; commits: feat(hooks): add tool-result hook adapter; verification: uv run pytest tests/test_hooks.py -q -k adapter
2. PR-2 fail-open and latency budget, on PR-1 — scope: budget enforcement, pass-through on timeout or exception, fault-injection tests; commits: feat(hooks): enforce latency budget and fail open, test(hooks): add fault injection; verification: uv run pytest tests/test_hooks.py -q -k latency
3. PR-3 install and uninstall, on PR-2 — scope: laconic install idempotent registration and reversal; commits: feat(cli): add idempotent install and uninstall; verification: uv run laconic install --dry-run
4. PR-4 status reporting, on PR-3 — scope: laconic status with ledger size, residency, projected break-even; commits: feat(cli): report ledger and residency status; verification: uv run laconic status
5. PR-5 live residency application, on PR-4 — scope: apply the M7 policy in-session, dry-run first, audit logging; commits: feat(hooks): apply residency policy in session; verification: uv run laconic status --residency --dry-run

CONSTRAINTS: fail-open and the latency budget are mandatory and must not be configurable away; no MCP transport work, which is M13; install must be reversible; every in-session mutation is audit-logged; minimal dependencies; repo style.
VERIFICATION (must pass): `uv run pytest tests/test_hooks.py -q && uv run laconic status && uv run pytest tests/test_hooks.py -q -k latency` — exit 0, measured p99 under 40 ms reported explicitly.
REVIEW:
Per PR:
- Scope matches its purpose; contracts match the reconciled plan; behavior is meaningfully tested.
- Failures are loud in logs and silent to the agent — verify the codec cannot propagate an exception into the session.
- History is atomic, conventional, attribution-free, and free of unrelated formatting churn.
- PR-specific verification output is captured.
Whole stack:
- Bases form one valid stack; cumulative acceptance and integration hold; CI is green; no regression coverage is removed without replacement.
- Human review gate satisfied: dry-run output, rollback notes, and audit logging reviewed by a human before merge.
- The docs-only root, when present, is reviewed and green before dependent code PRs.
- Report PR URLs, bases, verification, risks, manual gates, and review completion.
FINAL VERDICTS:
- Report the design verdict before the merge verdict.
- Then report exactly one merge verdict: `GO — RELEASE: unversioned — RELEASE PREP: pending` or `NO-GO — RELEASE: unversioned — REASON: <blocking gate>`.
- `GO` requires `DESIGN GO`, every PR correctly based/reviewed/green, local verification, full milestone acceptance, and a satisfied human review gate. `NO-GO` applies to pending or failed checks, incomplete review, scope drift, ambiguous readiness, manual gates, or unresolved release target.
DONE: design verdict with evidence; when authorized, a reviewed stack with a release-aware merge verdict and evidence.
```

---

### M13 — Surface B: MCP proxy

```text
/goal Deliver milestone M13 (Surface B: MCP proxy) from DEVELOPMENT_PLAN.md as a reviewed stack of PRs.

CONTEXT: DEVELOPMENT_PLAN.md §6 M13 + docs/system-design.md §3.2. Preconditions: M5, M6, M7 merged, and M9 reporting K1 at or above the 25% target. Repo: full codec and residency policy in place.
OBJECTIVE: Make the codec available to any MCP-speaking client by wrapping an upstream server and re-encoding tool results in flight. Acceptance: tools/list is forwarded byte-identically except for the added laconic_expand entry; a proxied tools/call returns encoded content whose elisions the model can recover through laconic_expand; upstream errors propagate unchanged; proxy failure falls back to passthrough.
RELEASE TRAIN: target=unversioned; included milestones=M1-M14; preparation trigger=all included milestones externally merged; required artifacts=none per the release-policy GAP in DEVELOPMENT_PLAN.md §2; release verification=uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest -q && uv run laconic gates --corpus tests/corpus; publication=not requested.

HUMAN REVIEW GATE: Do not merge or run destructive paths unattended until a human reviews dry-run output, rollback notes, and audit/tombstone logging. The proxy sits between a client and its real tool server.

PRE-IMPLEMENTATION DESIGN GATE:
1. Read this milestone, its source-map rows, current prompt, and `.docs/DEVELOPMENT_PLAN_HISTORY.md` when present.
2. Inspect the current codebase plus merged predecessor diffs, merged predecessor PR outcomes, CI/check evidence, and predecessor verification output.
3. Revalidate objective, interfaces, dependencies, acceptance, verification, risks, release train, and dependents: none. Confirm M9 reported K1 at or above 25%. A K1 below 15% invalidates this milestone; K1 from 15% through less than 25% requires a docs-only reconciliation before it proceeds. Confirm the targeted MCP specification version is current.
4. Append one ledger entry: timestamp, milestone, decision, trigger, evidence, plan/prompt sections changed, downstream impact, and implementation authorization.
5. If no material mismatch exists, report `DESIGN GO — PLAN REVISION: none`; this authorizes implementation.
6. If a mismatch exists, update both authoritative artifacts for M13 and every affected future milestone, append the revision ID, and report `DESIGN GO — PLAN REVISION: <entry IDs>`. This blocks product-code work until the reconciliation prerequisite merges.
7. If validity cannot be established, report `DESIGN NO-GO — REASON: <evidence>` and stop. After a reconciliation PR merges, repeat this gate and require `DESIGN GO — PLAN REVISION: none` before implementation.

RECONCILIATION RULE: A material revision opens `docs(plan): reconcile M13 design` as a docs-only prerequisite PR. It contains no product code, must be reviewed, green, and externally merged before any code PR, and must not be folded into an implementation PR.

PLANNED STACK (refine only to keep PRs reviewable):
0. Conditional prerequisite `docs(plan): reconcile M13 design` — scope: authoritative plan/prompt updates only; gate: reviewed, green, and merged before the implementation stack.
1. PR-1 proxy transport — scope: surfaces/mcp_proxy.py upstream connection and byte-identical tools/list passthrough; commits: feat(mcp): add proxy transport and tools/list passthrough; verification: uv run pytest tests/test_mcp_proxy.py -q -k passthrough
2. PR-2 result re-encoding, on PR-1 — scope: tools/call interception and encoding via the observation codec; commits: feat(mcp): re-encode tool results in flight; verification: uv run pytest tests/test_mcp_proxy.py -q -k encode
3. PR-3 expand tool, on PR-2 — scope: laconic_expand tool registration and handler; commits: feat(mcp): register laconic_expand recovery tool; verification: uv run pytest tests/test_mcp_proxy.py -q -k expand
4. PR-4 error and failure semantics, on PR-3 — scope: upstream error propagation, passthrough on proxy failure; commits: feat(mcp): propagate upstream errors and fall back to passthrough; verification: uv run pytest tests/test_mcp_proxy.py -q

CONSTRAINTS: never alter tool semantics beyond content encoding; upstream errors must not be swallowed; the model must always be able to recover elided content itself; no hook-surface work, which is M12; minimal dependencies; repo style.
VERIFICATION (must pass): `uv run pytest tests/test_mcp_proxy.py -q` — exit 0.
REVIEW:
Per PR:
- Scope matches its purpose; contracts match the reconciled plan; behavior is meaningfully tested.
- Failures are loud; confirm no upstream error is converted into a success or an empty result.
- History is atomic, conventional, attribution-free, and free of unrelated formatting churn.
- PR-specific verification output is captured.
Whole stack:
- Bases form one valid stack; cumulative acceptance and integration hold; CI is green; no regression coverage is removed without replacement.
- Human review gate satisfied: dry-run output, rollback notes, and audit logging reviewed by a human before merge.
- The docs-only root, when present, is reviewed and green before dependent code PRs.
- Report PR URLs, bases, verification, risks, manual gates, and review completion.
FINAL VERDICTS:
- Report the design verdict before the merge verdict.
- Then report exactly one merge verdict: `GO — RELEASE: unversioned — RELEASE PREP: pending` or `NO-GO — RELEASE: unversioned — REASON: <blocking gate>`.
- `GO` requires `DESIGN GO`, every PR correctly based/reviewed/green, local verification, full milestone acceptance, and a satisfied human review gate. `NO-GO` applies to pending or failed checks, incomplete review, scope drift, ambiguous readiness, manual gates, or unresolved release target.
DONE: design verdict with evidence; when authorized, a reviewed stack with a release-aware merge verdict and evidence.
```

---

### M14 — K3 human-study harness

```text
/goal Deliver milestone M14 (K3 human-study harness) from DEVELOPMENT_PLAN.md as a reviewed stack of PRs.

CONTEXT: DEVELOPMENT_PLAN.md §6 M14 + docs/system-design.md §4.1 + docs/overview.md §7. Preconditions: M8, M10, and M11 merged. Repo: replay harness, renderer, and narration layer in place.
OBJECTIVE: Build the instrumentation and materials for the study no published work has run — whether a developer reading a rendered compressed trace catches the same bugs as one reading the raw trace. Acceptance: a dry run with simulated responses produces an analysis-ready dataset; condition order is randomized and counterbalanced, verified statistically over repeated seeds; all four defect classes are represented; the analysis script is committed before any real data is collected with its equivalence margin fixed in advance.
RELEASE TRAIN: target=unversioned; included milestones=M1-M14; preparation trigger=all included milestones externally merged; required artifacts=none per the release-policy GAP in DEVELOPMENT_PLAN.md §2; release verification=uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest -q && uv run laconic gates --corpus tests/corpus; publication=not requested.

HUMAN REVIEW GATE: Do not merge or run destructive paths unattended until a human reviews dry-run output, rollback notes, and audit/tombstone logging. This milestone additionally involves human participants; recruitment, consent, and data handling require human sign-off before any real session is run.

PRE-IMPLEMENTATION DESIGN GATE:
1. Read this milestone, its source-map rows, current prompt, and `.docs/DEVELOPMENT_PLAN_HISTORY.md` when present.
2. Inspect the current codebase plus merged predecessor diffs, merged predecessor PR outcomes, CI/check evidence, and predecessor verification output.
3. Revalidate objective, interfaces, dependencies, acceptance, verification, risks, release train, and dependents: none. Confirm the protocol in docs/system-design.md §4.1 is unchanged and that M11's narration layer is included in the rendered condition.
4. Append one ledger entry: timestamp, milestone, decision, trigger, evidence, plan/prompt sections changed, downstream impact, and implementation authorization.
5. If no material mismatch exists, report `DESIGN GO — PLAN REVISION: none`; this authorizes implementation.
6. If a mismatch exists, update both authoritative artifacts for M14 and every affected future milestone, append the revision ID, and report `DESIGN GO — PLAN REVISION: <entry IDs>`. This blocks product-code work until the reconciliation prerequisite merges.
7. If validity cannot be established, report `DESIGN NO-GO — REASON: <evidence>` and stop. After a reconciliation PR merges, repeat this gate and require `DESIGN GO — PLAN REVISION: none` before implementation.

RECONCILIATION RULE: A material revision opens `docs(plan): reconcile M14 design` as a docs-only prerequisite PR. It contains no product code, must be reviewed, green, and externally merged before any code PR, and must not be folded into an implementation PR.

PLANNED STACK (refine only to keep PRs reviewable):
0. Conditional prerequisite `docs(plan): reconcile M14 design` — scope: authoritative plan/prompt updates only; gate: reviewed, green, and merged before the implementation stack.
1. PR-1 seeded-defect materials — scope: trace materials covering unhandled error path, incorrect boundary condition, swallowed exception, and wrong-target edit; commits: feat(study): add seeded-defect trace materials; verification: uv run pytest tests/test_study.py -q -k materials
2. PR-2 condition assignment, on PR-1 — scope: within-subjects counterbalanced randomization, seed control, statistical verification of balance; commits: feat(study): add counterbalanced condition assignment; verification: uv run pytest tests/test_study.py -q -k balance
3. PR-3 response capture, on PR-2 — scope: detection, time-to-decision, confidence, and calibration-gap capture; commits: feat(study): capture detection, timing, and confidence; verification: uv run pytest tests/test_study.py -q -k capture
4. PR-4 pre-registered analysis and dry run, on PR-3 — scope: analysis script with fixed equivalence margin, laconic study dry-run; commits: feat(study): add pre-registered analysis script, feat(cli): add study dry-run; verification: uv run laconic study dry-run --seed 0 --out /tmp/k3.json

CONSTRAINTS: the analysis script and its equivalence margin must be committed before any real participant data is collected; no analysis decision may be made after seeing real data; no real participant data may be committed to the repository; do not run real participants as part of this milestone; minimal dependencies; repo style.
VERIFICATION (must pass): `uv run pytest tests/test_study.py -q && uv run laconic study dry-run --seed 0 --out /tmp/k3.json` — exit 0 with an analysis-ready dataset.
REVIEW:
Per PR:
- Scope matches its purpose; contracts match the reconciled plan; behavior is meaningfully tested.
- Failures are loud; confirm the analysis script cannot be parameterized after data collection in a way that changes the pre-registered margin.
- History is atomic, conventional, attribution-free, and free of unrelated formatting churn.
- PR-specific verification output is captured.
Whole stack:
- Bases form one valid stack; cumulative acceptance and integration hold; CI is green; no regression coverage is removed without replacement.
- Human review gate satisfied: dry-run output, rollback notes, and audit logging reviewed by a human before merge; participant handling signed off before any real run.
- The docs-only root, when present, is reviewed and green before dependent code PRs.
- Report PR URLs, bases, verification, risks, manual gates, and review completion.
FINAL VERDICTS:
- Report the design verdict before the merge verdict.
- Then report exactly one merge verdict: `GO — RELEASE: unversioned — RELEASE PREP: pending` or `NO-GO — RELEASE: unversioned — REASON: <blocking gate>`.
- `GO` requires `DESIGN GO`, every PR correctly based/reviewed/green, local verification, full milestone acceptance, and a satisfied human review gate. `NO-GO` applies to pending or failed checks, incomplete review, scope drift, ambiguous readiness, manual gates, or unresolved release target.
DONE: design verdict with evidence; when authorized, a reviewed stack with a release-aware merge verdict and evidence.
```
