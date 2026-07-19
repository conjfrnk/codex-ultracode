---
name: benchmark-agent-configs
description: Design, curate, run, judge, validate, or report matched benchmarks for coding-agent prompts, configurations, skills, plugins, models, and orchestration strategies. Use only when the user explicitly requests a benchmark or baseline-versus-candidate comparison, compares agent setups, or asks for evidence of improvement, parity, cost, or quality per resource.
---

# Benchmark Agent Configs

Turn a comparative claim into a frozen intervention, matched run, blinded judgment, and bounded conclusion. Prompt inspection and feature inventories can identify capability gaps; they cannot establish a performance winner.

## Route the work

- **Design, curate, or execute a comparison:** read [references/protocol.md](references/protocol.md).
- **Prepare judge packets, review outputs, or adjudicate disagreement:** also read [references/judging.md](references/judging.md).
- **Aggregate replicates, quantify uncertainty, or decide claim eligibility:** also read [references/reporting.md](references/reporting.md).

Use the starter assets as separate audience-bound records:

- [controller-manifest.json](assets/controller-manifest.json) — identities, intervention, frozen conditions, blinding, failure policy, and analysis plan;
- [public-task.json](assets/public-task.json) — the only task packet a candidate receives;
- [evaluator-brief.json](assets/evaluator-brief.json) — private outcome rules, checks, vetoes, and judge guidance;
- [label-assignments.json](assets/label-assignments.json) — controller-only per-task X/Y mappings, stable across that task's replicates;
- [curation-record.json](assets/curation-record.json) — accept, revise, or reject before exposure;
- [run-record.json](assets/run-record.json) — one candidate/task/replicate execution and its artifacts;
- [judgment-record.json](assets/judgment-record.json) — one independent blinded review;
- [adjudication-record.json](assets/adjudication-record.json) — disagreement resolution and final task outcome;
- [benchmark-report.json](assets/benchmark-report.json) — paired outcomes, uncertainty, incidents, resources, and claim gate.

Keep controller identities and mappings away from candidates and judges. Keep evaluator material away from candidates. Give reducers judgment records by default, not raw identities or unrestricted trajectories.

## Validate packet mechanics

Before candidate exposure, enter this skill directory and validate the shipped templates:

```sh
python3 scripts/validate_packet.py --templates assets
```

For an instantiated comparison, pass the audience-separated files explicitly:

```sh
python3 scripts/validate_packet.py \
  --controller /private/control.json \
  --public-task /candidate/task.json \
  --evaluator /private/evaluator.json \
  --assignment /private/label-assignments.json \
  --curation /private/curation.json
```

Add `--run`, `--judgment`, `--adjudication`, or `--report` as those stages exist. Each optional flag accepts one stage record, so validate every record in its matching task context. The validator checks structure, cross-record identity and hashes, decision consistency, report arithmetic, claim gates, placeholders, and private-field leakage. Report validation establishes internal coherence and frozen-plan alignment; it cannot prove that omitted source records are complete. Mechanical validation does not establish task quality or comparison validity.

After every task has a final adjudication, unblind and aggregate with the controller-only [reducer](scripts/reduce_outcomes.py):

```sh
python3 scripts/reduce_outcomes.py \
  --controller /private/control.json \
  --assignments /private/label-assignments.json \
  --adjudication /private/task-1-adjudication.json \
  --adjudication /private/task-2-adjudication.json
```

Copy the reducer's candidate-ID counts into the report, then calculate the frozen `paired_win_rate_difference` estimator. Never aggregate global X/Y wins: their identities can reverse across tasks.

## Required workflow

1. State the target task classes, intervention, claim type, estimand, effect or equivalence margin, regressions, and safety vetoes.
2. Curate tasks before exposure. Reject leaked, unstable, trivial, impossible, underspecified, or treatment-specific packets.
3. Freeze public instructions, private checks, rubric, source state, budgets, permissions, stop conditions, randomization, replicate handling, failure policy, and analysis method.
4. Match every non-treatment condition that can be matched. Record unavoidable differences as confounds.
5. Run in fresh isolated workspaces. Preserve failures, timeouts, malformed outputs, incidents, and over-budget outcomes.
6. Judge final behavior before trajectories. Prefer deterministic held-out evidence where it proves the contract; blind subjective review and allow tie, neither acceptable, and inconclusive.
7. Collapse replicates at the preregistered task unit before estimating an effect. Never treat repeated runs of one task as independent tasks.
8. Report quality, process/safety, harness health, and resource use separately. Make a claim only when the frozen gate passes.

## Improvement ratchet

For every skill or prompt change:

- tie the change to an observed failure mode;
- predict the task class that should improve and the behavior that must not regress;
- include a direct trigger, a near miss, and a restraint case;
- compare baseline and treatment under frozen conditions;
- keep the change only when the target improves without a veto or unacceptable category regression.

Passing validation proves only that records are mechanically coherent. A comparison becomes decision-grade only through matched execution and independent outcome evidence.
