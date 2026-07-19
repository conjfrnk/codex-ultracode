---
name: change-contract
description: Create and maintain a compact behavior, ownership, decision, and proof contract for constraint-dense coding changes. Use before cross-file features, refactors, migrations, API/schema/config changes, ambiguous placement decisions, long multi-turn implementations, or after compaction or correction when decisions may have drifted. Do not use for tiny mechanical edits.
---

# Change Contract

Keep the intended behavior stable while implementation details evolve. When Codex Conductor also applies, let it own execution and safety; this skill owns the semantic model of the change.

## Capture the contract

Inspect source, tests, project guidance, and relevant documentation before asking questions. Record only material items:

- **Outcome:** the observable behavior that must change.
- **Entrypoint:** the normal command, route, UI action, job, or function that exercises it.
- **Acceptance:** success, failure, and boundary cases stated as falsifiable behavior.
- **Public surfaces:** affected APIs, schemas, configuration, environment, storage, events, or CLI behavior.
- **Invariants and compatibility:** behavior that must remain true, supported by evidence such as current callers, a documented public contract, a supported transition, or a released or deployed artifact.
- **Non-goals:** adjacent work and alternate artifacts that must remain untouched.
- **Canonical owner:** the existing module, type, or boundary that should own the behavior.
- **Dependents:** callers, consumers, tests, documentation, and operational surfaces that may need updates.
- **Proof surface:** the smallest fresh check that could disprove completion.
- **Open decisions:** only choices whose alternatives materially change the result.

Keep this contract in the active plan or a compact progress update. Create a repository file only when the task already calls for a durable specification.

## Map ownership

Trace entrypoint → boundary or producer → canonical owner → consumers → proof. For every acceptance item, identify the owner that implements it and the evidence that will test it.

Prefer extending an established owner over adding a parallel representation. Validate untrusted data at the actual boundary. Rely on internal invariants only after finding the constructor, type, or canonical validator that establishes them.

Ask the user only when inspection cannot settle a material product, compatibility, or scope choice. Otherwise state the narrow assumption and continue.

## Hold decisions steady

Recheck the contract after compaction, interruption, review feedback, a failed approach, or evidence that changes the plan:

1. Reconstruct decisions from the current request and source; treat summaries and old artifacts as leads to verify.
2. Compare the plan and diff with the outcome, non-goals, owners, compatibility evidence, and proof surface.
3. Surface any changed assumption, new public behavior, or ownership split.
4. Update the contract when evidence resolves a question; stop for direction when resolution requires a new material choice.

Do not create a parallel advisor workflow solely because this skill is active. Delegation remains with the host or orchestration layer.

## Close the contract

Map each acceptance item to fresh evidence and its actual result. Explain any expected owner, consumer, test, or documentation surface deliberately left unchanged. If an item is unproved, narrow the completion claim or report the exact gap.
