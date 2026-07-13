# Implementation Canary

Provider-visible workspaces are:

- `source/`: single-file slug normalization.
- `retry-policy/source/`: two-file compatibility migration.
- `event-routing/source/`: three-file shared-helper refactoring.
- `backoff-tests/source/`: test authoring without production edits.
- `quota-ledger/source/`: multi-module validation and idempotency repair.

Each matching `held-out/` directory stays physically separate before a provider runs. The backoff task uses five hidden production mutants instead of a held-out test module.

After a staged adapter emits a patch, apply it to a fresh copy of the matching source fixture, copy the matching held-out test into that copy's `tests/` directory, and run:

```bash
python3 -B -m unittest discover -s tests -v
```

The repository evaluator performs that work in a fresh OS-sandboxed workspace, verifies the patch hash against staged evidence, and emits hash-bound deterministic score input:

```bash
python3 -B tools/evaluate_implementation_canary.py \
  <unscored-provider-report.json> \
  <retained-provider.patch> \
  <new-evaluation-directory>

python3 -B -m conductor_runtime score-benchmark-report \
  <unscored-provider-report.json> \
  benchmark-suites/implementation-canary-tasks.json \
  <new-evaluation-directory>/score-input.json \
  <scored-provider-report.json>
```

The evaluator infers the canary from the report task id. `evaluation.json` binds the provider report, patch, source fixture, held-out fixture, exact visible/held-out counts or hidden-mutant outcomes, addition/modification scope, public signatures, bounded structural checks, and provider or structured-runtime reporting evidence. A matched trial is exploratory until both reports, evaluations, patch hashes, budgets, manifests, and the generated comparison are retained together. Use `write-parity-campaign` before repeated trials to preregister seeded row order, balanced provider-first assignment, and artifact destinations.
