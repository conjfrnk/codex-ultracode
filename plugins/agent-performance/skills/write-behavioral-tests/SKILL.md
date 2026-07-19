---
name: write-behavioral-tests
description: Design, add, modify, or review tests that prove repository-owned behavior. Use when work touches tests, regression coverage, fixtures, test helpers, mocks, flaky-test design, mutation resistance, characterization, property-based checks, or choosing the right test layer. Do not use merely to run an existing test command.
---

# Write Behavioral Tests

Make tests falsify an observable contract instead of mirroring an implementation. When Codex Conductor also applies, let it own execution and safety; this skill owns test design.

For legacy characterization, invariant-heavy code, parsers/serializers, protocol boundaries, state machines, or mutation resistance, also read [references/test-techniques.md](references/test-techniques.md).

## Select the proof surface

1. Inspect nearby test conventions, the real behavior owner, and existing coverage.
2. State the claim the test must be able to disprove and identify its oracle.
3. Choose the smallest layer that can prove that claim:
   - use a unit test for an owned pure transformation or invariant;
   - use an integration test for wiring, persistence, protocols, or collaborating owned components;
   - use an end-to-end or UI test only for behavior unique to that live surface.
4. Place the test with the suite that owns the contract, not merely beside the changed file.
5. Mock only beyond the boundary under test. Prefer real owned collaborators and stable public entrypoints.

A test does not prove a boundary when every meaningful component on that boundary is mocked.

## Choose the evidence mode

- **New behavior or bug:** write the smallest meaningful test, execute it before implementation when practical, and verify that the intended missing behavior or defect is what makes it red.
- **Changed behavior with real coverage:** run the relevant tests as a baseline, make the change, rerun them, and add coverage only for an unproved claim.
- **Unclear legacy behavior:** characterize observed public behavior before changing it; do not bless an obvious defect as a permanent contract without checking intent.
- **Trivial or non-behavioral change:** do not invent a test. Run applicable validation and explain why no behavior test is needed.
- **Review-only:** ask whether the test would fail under a plausible incorrect implementation; do not edit unless requested.

## Design cases and data

Cover contract-relevant cases only:

- the main observable behavior;
- meaningful boundaries and state transitions;
- documented or reachable errors;
- the exact regression mechanism;
- non-mutation, ordering, retries, concurrency, or idempotency when promised;
- a near miss that distinguishes the real rule from a convenient implementation.

Keep fixtures minimal and intention-revealing. Centralize a helper only when several tests share behavior rather than incidental setup. Recorded responses must be sanitized, versioned, and small enough that interface drift remains visible.

Reject assertions that only restate constants, type-system or framework guarantees, mock call choreography, private structure, or snapshots that obscure the claim. Do not add production seams solely to make an implementation-shaped test easy.

## Control nondeterminism

Control time, randomness, environment, filesystem, concurrency, and networking explicitly. Avoid sleeps; wait on a state or event with a bounded timeout.

For a flaky failure, first establish frequency and the varying condition. Preserve the seed, schedule, input, or environment that exposes it. A retry loop that hides the failure is not a fix.

## Verify and report

Run the focused test, then the smallest broader suite justified by shared ownership. Inspect skips, warnings, test counts, and exit status. Report the behavior proved, layer and oracle, red or baseline evidence when applicable, green result, mutation or near-miss evidence when used, and any live or cross-system behavior still unverified.
