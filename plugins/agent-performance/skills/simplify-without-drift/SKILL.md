---
name: simplify-without-drift
description: Review or refactor code for simpler ownership, fewer concepts, cleaner structure, and removal of over-engineering or AI-generated defensive clutter while preserving required behavior. Use for explicit simplification, cleanup, deslop, maintainability, invariant or guard audits, compatibility-shim review, or deep structural review; not for ordinary correctness review.
---

# Simplify Without Drift

Remove unjustified concepts while holding the behavior contract fixed. Remain read-only for review requests; edit only when simplification or fixes were requested. When Codex Conductor also applies, let it own execution and safety; this skill owns the structural analysis.

## Lock behavior and scope

Place the changed behavior in context by walking outward to its entrypoint, consumers, dependencies, tests, public promises, and documentation. State the canonical owner, observable behavior, effects, errors, ordering, concurrency, and compatibility evidence that matter. Evidence may be a current consumer, a documented public contract, a supported version transition, or a released or deployed artifact.

Do not relitigate approved product scope or assume that consumers absent from the checkout do not exist.

## Trace invariants and boundaries

Follow real producers and reachable states before judging validation, guards, fallbacks, or shims. Classify each candidate:

- **Required boundary:** protects untrusted input, an external response, protocol decoding, persistence, concurrency, retry/idempotency, configuration, secrets, or a supported transition.
- **Producer-owned invariant:** construction, typing, or one canonical validator makes the guarded state unreachable downstream.
- **Unresolved ownership:** reachability or the consumer contract still needs evidence.

Validate each fact once at its owner. Preserve boundary checks. Remove or avoid duplicated coercion, normalization, silent repair, fallback values, broad exception wrapping, and compatibility branches only when evidence makes them unnecessary.

## Run the simpler-design pass

Review in this order:

1. Delete behavior already owned by an established path.
2. Replace duplicate representations with the canonical helper, type, framework path, or source of truth.
3. Move behavior to its natural owner when callers currently coordinate internal details.
4. Collapse modes, flags, wrappers, adapters, or branches that encode no required distinction.
5. Make invalid combinations unrepresentable when the data model can do so locally.
6. Keep an abstraction only when it reduces what callers must understand.

Prefer removing concepts over cosmetic extraction. Do not propose speculative extensibility, broad rewrites, or compatibility unsupported by evidence.

## Validate each finding

Report a finding only when all of these are supported:

- the path is reachable in scope;
- the current cost or risk is concrete;
- the smaller direction preserves required behavior and compatibility;
- the change fits the local architecture;
- the value exceeds migration and review cost.

For each finding, include location, current cost, evidence, smaller direction, behavior that must remain, and the check needed after editing. Use `PASS` when there is no worthwhile simplification and `INCONCLUSIVE` when a missing contract prevents a safe recommendation.

If edits were requested, capture baseline behavior, apply one coherent simplification group, run focused and broader checks, inspect the diff, and repeat the review against the same contract.
