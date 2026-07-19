# Advanced behavioral test techniques

Use these techniques only when ordinary example-based tests leave a meaningful claim unproved.

## Characterization

Use characterization tests when legacy behavior is poorly documented and a change must preserve the observable surface. Exercise a public entrypoint, record only behavior that consumers can observe, and separate surprising-but-required behavior from likely defects that need a product decision.

## Properties and small exhaustive spaces

Use property checks for stable invariants across many inputs: round trips, monotonicity, idempotency, conservation, ordering, normalization, or boundedness. For small state spaces, enumerate them instead of sampling. Preserve random seeds and shrink failures to a readable counterexample.

Do not restate the implementation as the oracle. Derive the property from the public contract.

## Metamorphic and differential checks

Use a metamorphic check when the exact output is costly to specify but a controlled input transformation has a known relation to the output. Use a differential check when two independently owned implementations, versions, or encodings should agree over the same domain.

These are useful for parsers, serializers, migrations, scoring functions, query rewrites, and compatibility layers. Define the valid comparison domain first so shared undefined behavior does not create false failures.

## Contract and recorded-fixture checks

Use a contract test when an owned client and boundary must agree on request, response, error, or version behavior. Prefer a small sanitized recorded fixture when a handwritten mock would conceal field drift or protocol details. Record how the fixture was captured, remove secrets and unstable fields, and keep a refresh path.

## State, concurrency, and failure schedules

For state machines, test meaningful transitions and rejected transitions rather than every method independently. For concurrency, coordinate with barriers, events, or a controllable scheduler; assert ownership and terminal state after cancellation or partial failure. Never use an arbitrary delay as the correctness oracle.

## Mutation resistance

Name the plausible bad implementation the test should reject. When risk justifies it, temporarily invert a condition, perturb a boundary, remove an effect, or use a mutation tool to confirm the test turns red. Restore the source before continuing and keep mutation artifacts out of the final diff.

Mutation survival means the oracle or case selection is weak; it does not automatically mean more assertions are needed.
