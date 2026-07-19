---
name: debug-from-evidence
description: Diagnose bugs, failing tests or builds, crashes, flaky behavior, performance regressions, and unexpected output by establishing root-cause evidence before proposing or implementing a fix. Preserve diagnosis-only boundaries; do not use for straightforward feature work or generic review.
---

# Debug From Evidence

Find the first causal fault, not the nearest symptom. When Codex Conductor also applies, let it own execution and safety; this skill owns the causal investigation.

## Preserve the request boundary

For diagnosis, explanation, or review-only requests, remain read-only and stop after establishing the cause and evidence. Implement only when the user requested repair or the task clearly includes it.

## Establish the observation

1. Read the complete failure, inputs, environment, stack, logs, recent diff, and relevant configuration.
2. Attempt the narrowest safe reproduction. Record the command or action, actual result, repeatability, and any environmental differences.
3. If reproduction is unsafe or unavailable, state why and identify the smallest missing observation. Do not manufacture a successful reproduction.
4. Separate the primary failure from cleanup noise, downstream errors, and stale or unrelated warnings.

## Trace and discriminate

Follow the incorrect value, state, timing, or control flow backward through its consumers and producers. Compare expected and actual state at each meaningful boundary, including a similar working path when one exists.

Maintain at most three materially distinct hypotheses. For each, record:

- the proposed cause and owning boundary;
- evidence for and against it;
- confidence;
- the cheapest observation that would distinguish it from the alternatives.

Run one discriminating probe at a time. Prefer observation, an existing test, or a temporary probe over a production edit. A changed error message is not proof that the cause moved or disappeared.

Conclude at the earliest faulty owner. Distinguish what is observed, what is inferred, and what remains unknown.

## Repair when authorized

1. Add or identify a regression check that fails for the causal mechanism when practical.
2. Make the smallest coherent fix at the owning boundary.
3. Rerun the reproduction or regression check and inspect its full result.
4. Run broader checks justified by the affected dependency surface.
5. Review the diff for symptom-hiding fallbacks, unrelated cleanup, and accidental compatibility changes.

After two failed repair attempts, stop stacking patches. Rebuild the causal model, revisit earlier boundaries, and choose a new discriminating probe. Ask for direction only when a material product or risk decision remains.

## Report

Include the reproduction or attempted reproduction and actual result, causal chain, considered hypotheses, discriminating evidence, confidence, change made if any, focused proof, broader verification, and residual uncertainty. Never turn an unreproduced symptom or unrelated green check into a fixed claim.
