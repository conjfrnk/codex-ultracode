# Agent Performance skill routing evaluations

This repository-only suite evaluates the seven skills shipped by the
`agent-performance` plugin. It deliberately lives outside
`plugins/agent-performance/` so neither the private oracle nor its validator is
copied into an installed plugin.

`skill-routing-cases.json` is the private oracle. A runner gives a candidate
only a case's `prompt`, then compares observed routing and behavior with
`expected`.

Each case declares:

- `category`: `direct`, `near_miss`, `composition`, or `adversarial`;
- `covers`: skills whose trigger or restraint the case exercises;
- `expected.selected_skills`: the exact plugin skill set expected;
- `expected.boundary`: `analysis_only`, `source_read_only`, or `scoped_writes`;
- required and forbidden behavior labels;
- claim names with `must_prove`, `must_not_claim`, or
  `may_remain_unresolved` status.

The checked-in suite requires one direct case per skill and complete skill
coverage in near-miss, composition, and adversarial cases. Arrays are sorted
and duplicate-free so changes remain reviewable.

From the repository root, validate mechanics with:

```sh
python3 benchmark-suites/agent-performance-skill-routing/validate_skill_evals.py
```

Run the focused regression tests with:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest tests.test_agent_performance_evals
```

Validation proves schema integrity, coverage, and safety constraints. It does
not prove that a model routes or behaves correctly; that requires isolated
candidate runs using prompts without the private expectations.
