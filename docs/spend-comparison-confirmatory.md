# Controlled Spend Confirmatory Study — Draft Pre-Registration

**Status: design selected, remaining decisions open. Not authorized, not funded, not executable.** No confirmatory manifest exists, no manifest digest has been minted, no fixture beyond the four pilot tasks has been authored, and no provider request may be made against this document. Scoping surfaced four blockers that change the cost, external validity, and claims boundary materially; each needs an owner decision before a manifest is committed.

## 1. What the pilot licensed

M20-v2 completed under manifest `0a7cacb9e3970ed578a78eb1363d7e6fa70dec8fd7fbdd45aec1fa15d5efac28` with 24 of 24 valid cells and `$0.9975374` of pooled gateway spend. Its whole purpose was to size a confirmatory study, and it returned a paired log-cost standard deviation of `0.141205388084` and a confirmatory requirement of **15 tasks at two repetitions**.

That number is a faithful output of the frozen calculation. It is not, on its own, a safe basis for funding a study.

## 2. Blocker one — the sample size rests on four observations

The pilot's dispersion estimate comes from **four** paired task differences, one per frozen task. A standard deviation estimated on three degrees of freedom is very imprecise, and the required sample scales with its square.

Using the frozen parameters — two-sided alpha `0.05`, power `0.80`, smallest worthwhile reduction `10%` on the log scale so `Δ = |ln 0.9| = 0.105361`, normal approximation — the required task count is `n = (z_{α/2} + z_β)² σ² / Δ²`:

| Basis for σ | Sidedness | σ | Required tasks | Cells | Estimated spend |
| --- | --- | ---: | ---: | ---: | ---: |
| Pilot point estimate | — | 0.1412 | **15** | 90 | $3.74 |
| 80% upper confidence limit | one-sided | 0.2439 | 43 | 258 | $10.72 |
| 90% upper confidence limit | one-sided | 0.3199 | 73 | 438 | $18.21 |
| 95% upper confidence limit | one-sided | 0.4123 | 121 | 726 | $30.18 |
| Two-sided 95% CI, lower endpoint | two-sided | 0.0800 | 5 | 30 | $1.25 |
| Two-sided 95% CI, upper endpoint | two-sided | 0.5265 | 196 | 1176 | $48.88 |

The sidedness column matters and is easy to misread. The first three upper limits are conventional **one-sided** upper confidence limits on σ, which is the right form for sizing decisions because only under-estimating σ is harmful. The last two rows are the two endpoints of the **two-sided** 95% interval, so the last row's `0.5265` is a one-sided 97.5% bound, not the one-sided 95% bound of `0.4123` above it. Read as a single ladder they would over-provision.

Taking the two-sided endpoints together, the 95% confidence interval for the required task count is roughly **5 to 196**. That is a statement about the interval induced by the σ interval, not a probability statement about a single unknown n; the practically useful figures are the one-sided upper limits, because a study is harmed by under-sizing and merely made more expensive by over-sizing. A fixed 15-task design is powered only if the true σ is at or below the pilot's point estimate; if σ is at the 80% upper confidence limit the study is powered at well under half its nominal 80%, and it would spend real money to produce an inconclusive result — the most expensive possible outcome, because an underpowered null cannot be distinguished from a true absence of effect.

Estimated spend assumes the pilot's observed `$0.041564` per cell holds. That is itself an extrapolation from 24 cells and should be treated as a planning figure, not a cap.

### Selected design

**The owner selected the internal-pilot two-stage design.** Section 2a specifies it. The alternatives are recorded for the record: a single stage sized to the one-sided 80% upper limit at 43 tasks and roughly `$10.72`, or to the one-sided 95% upper limit at 121 tasks and roughly `$30.18`. A fixed 15-task single stage was rejected as the option most likely to spend money and answer nothing.

## 2a. The two-stage effect-withheld protocol

Selecting the design fixes the following. Every rule here is pre-specified and may not be changed once stage one has produced data.

### Why two stages pay for themselves

Sizing must use an upper confidence limit on σ rather than a point estimate, because only under-estimating σ is harmful. The penalty for that uncertainty shrinks as the estimate improves. At the pilot's three degrees of freedom the one-sided 80% upper limit sits `1.7276×` above the point estimate; after a 15-task stage one, at fourteen degrees of freedom, it sits `1.2160×` above. Stage one therefore buys a 30% reduction in the uncertainty tax for `$3.74`, and that is the entire argument for the design.

### Stage one

Fifteen tasks, two repetitions, three arms — 90 cells, an estimated `$3.74`, and roughly 35 minutes of wall clock scaled from the pilot's observed nine minutes over 24 cells. Everything else follows the frozen M20-v2 contract unchanged: model and version pins, per-cell request ceiling, wall limit, price snapshot, gateway cap enforcement, receipt binding, quiescence gate, and privacy allowlist.

### Effect-withheld re-estimation

After stage one completes, σ is re-estimated from its paired task log-cost differences. **Only σ may leave that computation.** The interim effect estimate, arm means, and the sign of any difference must not be inspected, reported, or allowed to influence whether stage two runs, how large it is, or whether the study stops.

The term is *effect-withheld*, not blinded in the classical sense: the three arms are known and cannot be allocation-blinded. What is withheld is the interim effect — the mean and sign of the paired differences — while σ̂₁, a nuisance parameter, is used.

A dedicated re-estimation command makes the sanctioned path safe: it emits an exact, allowlisted output of the re-estimated σ, its degrees of freedom, and the recomputed task total, and cannot serialize an effect, an arm label attached to a cost, or a per-task value. It must exist and be mutation-tested before stage one runs.

Be precise about what that does and does not prevent. It guarantees the sanctioned path is effect-free. It does **not** make the effect unreadable: `analysis-private.json` already holds `paired_task_log_cost_differences` and `task_arm_mean_cost_usd` on disk for stage one, and any operator with read access to the private root can inspect them. The command is a safe alternative to doing that, not a barrier against it. Preventing direct inspection additionally requires operator discipline and access control, and this protocol depends on both. Anyone who does look at the interim effect has broken the design and must say so, because the alpha argument below no longer holds for them.

Effect-withheld, variance-only re-estimation has negligible type I inflation, because under normality the sample standard deviation of the paired differences is independent of their mean, so the resizing decision is independent of the test statistic. **Alpha therefore remains 0.05 two-sided with no adjustment.** Any use of the interim effect would instead require a combination test or a conditional-error rule; this design deliberately forgoes that in exchange for keeping the analysis simple and the alpha honest.

### The re-estimation rule

Let `σ̂₁` be the standard deviation of the stage-one paired differences on fourteen degrees of freedom. The multiplier is fixed at the stage-one degrees of freedom and is not recomputed on the larger final sample: `σ̂₁` is always estimated from the 15-task stage one before stage two exists, and recomputing the limit on the final degrees of freedom would shrink the factor and under-inflate the requirement. The total task requirement is recomputed at the **one-sided 80% upper confidence limit** of `σ̂₁`, not at its point estimate — applying the point estimate again would repeat the pilot's mistake at a smaller scale:

`n_total = ⌈ (z_{α/2} + z_β)² · (σ̂₁ · 1.2160)² / Δ² ⌉`, with `Δ = |ln 0.9|`.

Stage two runs `max(0, n_total − 15)` additional tasks. If `n_total ≤ 15` the study is already complete and stage two does not run.

| Stage-one `σ̂₁` | 80% UCL | `n_total` | Stage-two tasks | Total estimated spend |
| ---: | ---: | ---: | ---: | ---: |
| 0.0800 | 0.0973 | 7 | 0 | $1.75 |
| 0.1000 | 0.1216 | 11 | 0 | $2.74 |
| 0.1412 (pilot value) | 0.1717 | 21 | 6 | $5.24 |
| 0.1800 | 0.2189 | 34 | 19 | $8.48 |
| 0.2439 | 0.2966 | 63 | 48 | $15.71 |
| 0.3200 | 0.3891 | 108 | 93 | $26.93 |

Spend figures extrapolate the pilot's observed `$0.041564` per cell and are planning estimates, not caps.

### Pooling, stopping, and the cap

Stage one's observations **pool** into the final effect estimate. The two stages are one study with one analysis over all `n_total` tasks, not a pilot followed by a replication. There is no interim analysis for efficacy or for futility, and no early stopping; the only decision taken between stages is the size of stage two.

The recomputed `n_total` must be checked against a pre-declared maximum before stage two is funded. If the rule demands more tasks than that maximum allows, the study **stops and reports that it stopped for infeasibility** — it does not silently run an underpowered stage two. That maximum is open decision five and is not the pilot's `$10.00`.

The final report states the total task count actually run, that it was re-estimated under this rule, and the observed `σ̂₁` that drove it.

## 3. Blocker two — eleven task fixtures do not exist

The repository contains four task fixtures, `t01` through `t04`, each a seed tree of roughly 60–100 lines of Python with a failing baseline, a prompt, a solution patch, and a completion oracle. A 15-task study needs **11 new fixtures**; a 43-task study needs **39**.

Each new fixture must satisfy the same contract the existing four do: a deterministic seed repository, a `diagnose.py` that fails before the fix and passes after, a prompt that does not prescribe a tool trajectory, a reference solution patch that applies cleanly, and a `unittest` oracle that fails before and passes after. Every one of those properties is verified by `manifest check --verify-oracles`.

The validity risk is larger than the authoring effort. The pilot's σ describes dispersion across four tasks that were chosen together and resemble each other. If the eleven new tasks are minor variations of the same shape, the confirmatory study inherits a σ that does not generalize and the result will not transfer to real work. If they are genuinely diverse, the true σ is likely **higher** than the pilot's estimate, which pushes the required n up again — the first blocker and this one are coupled, not independent.

This is the dominant cost of the study, and it is human authoring time, not provider spend.

## 4. Blocker three — the claims boundary reverses

Every controlled-spend artifact published so far deliberately withholds the paired effect. The pilot publishes dispersion, correlations, counters, and pooled spend precisely so that no reader can recover which arm was cheaper; a security review confirmed direction is unrecoverable from the published figures.

A confirmatory study exists to publish exactly that: **a paired effect estimate, its confidence interval, and a direction.** Running it is a deliberate decision to start making a comparative cost claim about Laconic, native OMP, and Headroom, including the possibility that the result is unfavourable or equivocal and must be published anyway.

The pre-registration must therefore fix, before any data exists:

- the exact estimand and its published form;
- the equivalence bounds, so a null is reported as equivalence rather than as absence of evidence;
- a commitment to publish the interval whatever its sign;
- what remains private even in a confirmatory report.

## 5. Blocker four — session-length external validity

The current short isolated tasks can answer a bounded short-task cost question. They cannot establish the long-session cache-reuse compounding that motivates Laconic's local modelled value. The mechanism's proposed economic benefit depends on when a result enters a session, how many later turns reread it, whether the agent expands it, and whether shorter visible context changes follow-up work. A short task population cannot identify those effects.

A replacement workload contract must define session-length strata, payload-heavy task representation, when observations enter the session, induced expansions and follow-up work, cache-write and cache-read accounting over subsequent turns, behavior and completion equivalence, and repository and task diversity.

Until that workload exists, fixture authoring for the confirmatory run, manifest creation or digest minting, credential access, provider requests, spend authorization, and effect, direction, equivalence, or superiority claims remain prohibited. The selected two-stage statistics stay intact; this blocker concerns whether the workload can answer the research question at all.

## 6. Structural work in the tooling

`TASK_COUNT` is a frozen module constant of `4` in `tools/controlled_spend/manifest.py`, enforced for every manifest, and `RUN_COUNT` derives from it. A confirmatory manifest with a different task count requires making the task count schema-scoped so the M20-v1 and M20-v2 manifests continue to validate byte-identically under their existing digests. That is a contained change with an obvious regression test, but it is deliberately **not** made yet. Parameterizing the task count today would add generality with a single call site, for a study that is not funded and whose final task count is unknown; the repository's own rule is to add an abstraction only once a second real call site exists. It lands together with the confirmatory manifest, where it has two call sites and a real test, and it must be reviewed before that manifest is committed.

The gateway cap, receipt binding, quiescence gate, and privacy allowlist all carry over unchanged. A confirmatory campaign will run longer than nine minutes in proportion to its cell count, so the quiescence window must be re-sized to the new expected duration.

## 7. Open decisions

None of these can be resolved from the pilot data.

1. ~~Design.~~ **Resolved:** the owner selected the internal-pilot two-stage design with effect-withheld, variance-only re-estimation. Section 2a specifies it.
2. The replacement long-session workload contract and its session-length strata, payload timing, expansion, cache-accounting, equivalence, and diversity requirements.
3. Who authors the new fixtures once that workload contract exists.
4. Whether the project is prepared to publish a comparative cost claim in either direction, including an unfavourable or equivocal one.
5. The spend cap for the confirmatory campaign, which is not the pilot's `$10.00`.

## 7a. This pre-registration cannot be finalized yet

A pre-registration is only worth having if it commits, before data exists, to publishing whatever comes out. Open decision four is exactly that commitment, and it is unresolved. Until it is answered, section 2a is a specified design rather than a binding protocol, and no confirmatory manifest may be committed against it. Selecting the design does not resolve it: choosing how to run the study is not the same as agreeing to publish an unfavourable result from it.

## 8. Not authorized by this document

No confirmatory manifest, no manifest digest, no fixture authoring, no credential access, no provider request, no spend, no change to any published artifact, and no savings, equivalence, direction, or superiority claim. M20-v1 and M20-v2 remain closed and may not be rerun or pooled into this study.
