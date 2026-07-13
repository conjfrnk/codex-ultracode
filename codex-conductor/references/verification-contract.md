# Verification Contract

## Verification Hierarchy

Prefer project-native evidence:

1. exact reproduction of bug or issue,
2. relevant unit tests,
3. relevant integration tests,
4. type checks,
5. linters,
6. build commands,
7. static review,
8. manual reasoning.

## Evidence Rules

Do not say a check passed unless it was run and passed.

If checks could not be run, say why and provide the strongest available alternative.

## Verification Matrix

For implementation tasks, fill this mentally or in an artifact:

| Claim | Check | Result | Evidence | Confidence |
| --- | --- | --- | --- | --- |

## Suggested Final Wording

Good:

`Ran npm test -- auth.test.ts; all 12 relevant tests passed.`

Good:

`I could not run the suite in this environment, so I reviewed the control flow and identified the exact call path affected.`

Bad:

`Everything works.`
