---
name: review-code-changes
description: Review diffs, patches, pull requests, or uncommitted changes for consequential correctness, regression, security, compatibility, integration, and maintainability defects. Use for ordinary code review, spec-compliance review, plan review against source, or evaluating review feedback. Remain read-only unless the user separately requests fixes; use debug-from-evidence instead when root-cause diagnosis is the main task.
---

# Review Code Changes

Find defects that materially affect users, operators, maintainers, or the promised behavior. Lead with findings, and allow a clean review to pass without invented criticism.

## Establish the review contract

Inspect the request or specification, current diff, relevant source, tests, public contracts, and nearby conventions. Reconstruct:

- intended behavior and explicit non-goals;
- changed public and internal surfaces;
- compatibility evidence and affected consumers;
- trust, persistence, concurrency, and failure boundaries;
- the proof the author relies on.

If intent remains ambiguous, identify the exact ambiguity. Do not assume the patch is correct merely because tests pass or incorrect merely because it differs from an expected implementation shape.

## Trace the affected surface

Read beyond changed lines only as needed to follow entrypoints, producers, owners, consumers, and tests. Review generated/config/schema/documentation surfaces when the change makes them part of the contract.

Check distinct lenses:

1. **Behavior:** acceptance cases, edge cases, errors, state transitions, and ordering.
2. **Integration:** callers, persistence, configuration, events, deployment/runtime assumptions, and compatibility.
3. **Trust and safety:** validation boundaries, authorization, secrets, destructive effects, retries, and idempotency.
4. **Concurrency and lifecycle:** races, cancellation, cleanup, reentrancy, resource ownership, and partial failure.
5. **Proof:** whether tests and verification would fail for the plausible bad implementation.
6. **Design:** canonical ownership, duplicated state, needless concepts, and local maintainability.

Use `simplify-without-drift` for a deeper structural pass when simplification is explicitly in scope.

## Validate findings before reporting

A finding needs all of:

- a precise location and reachable path;
- the violated behavior, invariant, or contract;
- a concrete impact or failure mode;
- evidence from source, tests, docs, or reproducible behavior;
- a bounded correction direction.

Try to disprove each candidate finding. Inspect guards, callers, types, and tests that may already make the bad state unreachable. Do not report style preferences, speculative consumers, pre-existing issues unrelated to the change, or vague requests for more tests.

Rank only by consequence:

- **Blocker:** unsafe to merge or use; causes severe loss, exposure, or a fundamentally invalid result.
- **High:** likely correctness, security, or compatibility failure in realistic use.
- **Medium:** meaningful defect with a narrower trigger or recoverable impact.
- **Low:** concrete, worthwhile problem that does not threaten the main outcome.

## Report

List findings first, highest consequence first. For each, give location, trigger, impact, evidence, and correction direction. Then state verification performed, missing evidence, and the overall verdict: `PASS`, `CHANGES NEEDED`, or `INCONCLUSIVE`.

For review-only requests, do not modify files, send messages, or create external state. If fixes are requested, preserve the review contract, implement the smallest coherent corrections, rerun the relevant proof, and re-review the resulting diff.
