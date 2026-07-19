# Replicate analysis and claim eligibility

Use this reference before aggregating repeated runs, reporting uncertainty, or stating that one configuration is better, equivalent, or no worse.

## Analysis unit and replicates

Treat the task as the independent unit unless the design justifies another unit. Repeated candidate runs on the same task measure within-task variability; they do not enlarge the task sample.

Freeze one task-level aggregation rule before execution. It may require a majority of valid replicate judgments, an all-replicates acceptance rule for high-risk behavior, or a deterministic tie/inconclusive result when replicates disagree. Report replicate disagreement rather than smoothing it away.

Apply the frozen failure policy:

- candidate failure, timeout, malformed output, or budget breach normally counts against that candidate;
- controller-owned infrastructure failure invalidates the matched pair when neither candidate received a comparable attempt;
- retries occur only under the preregistered rule and remain linked to the original record.

## Paired outcomes

At the task boundary, use the controller-only assignment record to remap the final X/Y outcome to the exact candidate ID. Then report wins by candidate ID alongside ties, neither acceptable, inconclusive, and invalid pairs. Global X/Y counts are invalid because task-level label permutations can reverse their meaning. Preserve the assignment record for audit without exposing it to judges. Always state both the task denominator and the underlying run count. Break results down by preregistered task class and safety/restraint categories.

For the bundled `paired_win_rate_difference` estimator, compute `(target wins - comparator wins) / valid tasks`; ties, neither acceptable, and inconclusive contribute zero. The report validator reconciles this calculation with the declared point estimate before applying an interval-based claim gate.

Do not convert categorical judgments into an averaged numeric score unless the rubric declared anchored numeric semantics before exposure. Outcome quality remains separate from latency, token, cost, tool, and agent counts unless resource efficiency is the primary outcome.

## Uncertainty

Choose a paired method appropriate to the sample and estimand. Examples include an exact interval over non-tied paired outcomes or a task-clustered bootstrap over task-level scores. Record the method, seed, interval level, treatment of ties/inconclusive outcomes, and sensitivity checks before running.

Small or narrow task sets support only narrow conclusions. A precise calculation cannot repair contaminated tasks, unmatched conditions, correlated leakage, or a poor task sample.

## Claim gates

A superiority claim requires:

- the lower uncertainty bound above the frozen minimum effect;
- no safety veto or unacceptable preregistered category regression;
- complete matched artifacts and bounded confounds.

A non-inferiority claim requires the lower bound above the negative tolerance. An equivalence claim requires the entire interval inside the equivalence band. Failure to show a difference is not evidence of equivalence.

Mark the result ineligible when tasks changed after exposure, identities leaked where bias matters, failure handling drifted, exclusions were decided after results, artifacts are incomplete, or the sample does not represent the claimed task classes.

## Report contents

Include:

- hypothesis, intervention, target and comparator candidate IDs, claim type, target-minus-comparator estimand, margins, and frozen conditions;
- task and run denominators, invalidations, and exclusions;
- per-task and task-class outcomes;
- uncertainty and sensitivity results;
- safety incidents and category regressions;
- resource metrics with units and provenance;
- process/harness findings separate from outcome quality;
- confounds, missing evidence, claim eligibility, and a conclusion no broader than the evidence.
