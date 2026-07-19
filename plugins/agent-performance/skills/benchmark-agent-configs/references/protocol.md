# Matched comparison protocol

Use this protocol to produce reproducible evidence rather than a retrospective success story.

## 1. Preregister the claim

Record the hypothesis, target task classes, candidates, intervention, matched conditions, primary outcome, unacceptable regressions, and safety vetoes before execution.

Choose one claim type:

- **superiority:** the lower uncertainty bound must exceed the preregistered minimum effect;
- **non-inferiority:** the lower bound must stay above the negative tolerance;
- **equivalence:** the full interval must remain inside the symmetric equivalence band;
- **descriptive:** report observed outcomes without a winner claim.

Name the target candidate, comparator candidate, estimand, estimator, and analysis unit. Express the effect direction as target minus comparator. The bundled mechanical path uses `paired_win_rate_difference`: `(target wins - comparator wins) / valid tasks`, with other categorical outcomes contributing zero. For agent comparisons, the default unit is the task after collapsing repeated runs. Freeze the replicate aggregation, invalidation rule, uncertainty method, run order, random seed, and retry policy.

## 2. Build audience-separated task records

Candidate-visible material contains only the task, observable acceptance criteria, allowed artifacts, permissions, forbidden actions, budget, terminal condition, and the fixture revision needed to work. Keep task origin, generation rationale, gold material, private checks, and identity mappings outside the candidate workspace.

The evaluator brief must contain the exact acceptance rule, required checks, non-compensatory vetoes, outcome states, evidence requirements, anti-solutions, and rubric version. A judge cannot apply a rule hidden only in the controller record.

The controller record binds public/evaluator hashes, identities, treatment variables, randomization, execution policy, analysis, and two condition scopes: comparison-wide conditions shared by every task, plus each task's fixture revision, permissions, and budget. Tasks may differ from one another; the two candidates may not differ within a task unless the difference is the declared intervention. Store each visibility tier under access controls appropriate to its audience.

## 3. Curate before exposure

Review each packet for:

- clarity without implementation leakage;
- solvability from candidate-visible material;
- stable, deterministic setup or declared sources of variance;
- usefulness for the target capability and difficulty;
- treatment neutrality;
- privacy, licensing, and safety;
- a judgeable acceptance rule and meaningful anti-solutions.

Return `accept`, `revise`, or `reject` with evidence. Only accepted versions may run. After any candidate sees a packet, corrections require invalidating the affected run or creating a new task version.

## 4. Match and execute runs

Use fresh isolated workspaces from the same task fixture. Bind model/provider/version/effort, tools, network, permissions, skills, agent limits, time, tokens, cost, cache state, platform, and human intervention between the two candidates unless a field is part of the intervention. Different tasks may freeze different fixtures, permission envelopes, and budgets.

Balance order with the frozen randomization method. Record one run per candidate/task/replicate, including:

- final answer, patch, and declared artifacts;
- verification output and exit status;
- completion status, timeout, malformed output, or failure reason;
- elapsed time, tokens, cost, tools, and agents when measured;
- safety and permission incidents;
- enough bounded process evidence to diagnose after outcome scoring.

Do not silently drop or rerun a bad candidate result. Apply the preregistered policy. Invalidate a pair only for controller-owned infrastructure failure that prevented a matched attempt.

## 5. Judge and adjudicate

Prepare opaque artifact labels through a seeded per-task assignment recorded controller-side. Keep that task's label mapping stable across its replicates and judgments. Score final outcomes before process. Use deterministic checks first when they fully establish a criterion; use independent blinded judgment for maintainability, integration fit, or other qualities that cannot be safely encoded.

Every judgment records absolute acceptability for each candidate before the relative outcome. Allowed outcomes are candidate X wins, candidate Y wins, tie, neither acceptable, and inconclusive. Safety vetoes make a candidate unacceptable; they cannot be offset by strengths elsewhere.

Use multiple lanes only when they have distinct evidence responsibilities or the decision is material. An adjudicator resolves cited disagreements from judgment records. Read [judging.md](judging.md) for packet and finding rules.

## 6. Analyze and report

Collapse repeated runs according to the frozen task-level rule. Only after the final task outcome is fixed may the controller use its assignment record to remap X/Y into candidate IDs. Aggregate candidate-ID outcomes, compute the preregistered uncertainty method, and report invalidations and exclusions with reasons and denominators. Never aggregate global X/Y wins when label assignments vary by task.

Keep separate:

- final outcome quality;
- process and safety behavior;
- harness or packet integrity;
- observed resource use.

Apply the claim gate only after checking sample scope, matching, blinding, artifact completeness, vetoes, category regressions, uncertainty, and confounds. Read [reporting.md](reporting.md) before making a comparative claim.
