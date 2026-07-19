---
name: verify-before-claiming
description: Verify completion, correctness, readiness, equivalence, improvement, passing, or fixed claims against fresh evidence. Use before finalizing non-trivial coding work, after delegated work, or whenever Codex is about to say behavior works, a regression is fixed, checks pass, or one configuration is as good as or better than another. Do not use merely to design tests or diagnose a failure.
---

# Verify Before Claiming

Make the strength of the conclusion match the strength of the evidence. When Codex Conductor also applies, use its verification envelope; this skill audits the final claim set.

## Inventory the claims

Extract every material statement the final answer would make, including:

- behavior added, preserved, or fixed;
- tests, builds, lint, types, packaging, installation, or live flows said to pass;
- compatibility, safety, performance, cost, parity, or superiority;
- files or systems said to be unchanged;
- delegated work said to be integrated.

Rewrite vague claims into falsifiable statements. “Looks good” is not a proof target.

## Choose the disproof surface

For each claim, select the smallest fresh check that could show it is false. Prefer, in order:

1. the exact reproduction or acceptance path;
2. focused behavioral or integration tests;
3. broader tests justified by shared ownership;
4. type, lint, schema, packaging, or build validation;
5. live/manual inspection when automation cannot observe the behavior;
6. static reasoning, explicitly labeled as such.

Use independent evidence for high-consequence or subjective claims. A subagent summary, old log, cached result, or test unrelated to the changed owner is not sufficient by itself.

## Gather fresh evidence

Run checks after the final edit. Read the full result, exit status, warnings, skips, counts, environment, and artifact paths. Confirm the check exercised the intended files and behavior.

For configuration, prompt, documentation, or skill changes, validate structure and links, test discovery or ingestion when available, and use a realistic forward test when the behavior depends on model routing.

For equivalence or improvement claims, require a matched comparison with a declared metric, conditions, uncertainty, regressions, and safety vetoes. Static feature inspection may justify a capability statement, never a performance winner.

## Reconcile claims and evidence

Assign one status to each claim:

- **PROVED:** fresh evidence directly supports the bounded statement.
- **DISPROVED:** evidence contradicts it or exposes a regression.
- **UNRESOLVED:** the check was unavailable, incomplete, confounded, or too weak.

Fix and rerun only when repair is authorized. Otherwise narrow the statement. Do not omit failures, timeouts, warnings, skips, or unsupported delegated claims from the final report.

## Report

Lead with what is proved. State the exact checks and results, then unresolved or disproved claims, what was not exercised, and remaining risk. Never say “all tests passed” when only a subset ran, “fixed” when the original symptom was not rechecked, or “better” when no decision-grade comparison exists.
