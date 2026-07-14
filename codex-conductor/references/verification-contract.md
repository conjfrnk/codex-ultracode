# Verification

Prefer evidence in this order:

1. reproduce the reported failure;
2. focused unit or integration tests;
3. broader relevant tests;
4. type checks, lint, and build;
5. diff and static review.

Run checks against the actual result being delivered. For staged work, verification runs against the stage and must not mutate it; source drift or verifier mutation invalidates evidence.

Do not convert a provider-completed message into acceptance. Check exit status, bounded output, expected artifacts, and relevant behavior. A model verdict must satisfy its strict structured contract and cite evidence.

In the final response state what changed, checks actually run and their results, checks not run, and remaining risk. Avoid `everything works` or comparison claims unsupported by matched evidence.
