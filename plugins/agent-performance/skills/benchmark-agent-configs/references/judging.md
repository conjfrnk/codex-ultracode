# Blinded judgment and adjudication

Use this reference when preparing evaluator packets, reviewing candidate artifacts, or resolving reviewer disagreement.

## Exposure boundaries

- Candidates receive only the public task and their workspace.
- Independent judges receive the public task, evaluator brief, opaque candidate artifacts, and bounded verification summaries.
- Judges do not receive controller identities, assignment maps, task-generation rationale, or unrestricted host paths.
- Adjudicators receive judgment records, rubric versions, and cited evidence. Escalate to raw artifacts only when a named disagreement cannot be resolved otherwise.

Sanitize candidate self-identification, model banners, metadata, path names, and transcript excerpts before review. Record the sanitization and keep the identity mapping controller-only.

Use one opaque X/Y mapping per task and keep it stable across that task's replicates, judges, and adjudication. Judges and adjudicators emit only X/Y outcomes. The controller unblinds a task after adjudication; reducers must not infer identities from artifacts.

## Review order

1. Confirm the packet hash, rubric version, allowed evidence, and absence of identity leakage.
2. Determine whether each candidate independently meets the acceptance rule.
3. Apply required checks and non-compensatory safety vetoes.
4. Compare candidates only after absolute acceptability is recorded.
5. Record one allowed outcome with evidence and confidence.
6. Inspect process evidence afterward for safety, scope, and diagnostic findings without rewriting the frozen outcome rule.

Use `inconclusive` when missing or contaminated evidence prevents a defensible decision. Use `neither_acceptable` when evidence is sufficient and both candidates fail.

## Finding standard

Every counted finding includes:

- the candidate and precise artifact location;
- the violated acceptance item, veto, or rubric dimension;
- the observable impact;
- supporting and counterevidence;
- confidence and whether it changes acceptability or the relative outcome.

Do not count implementation-shape preferences, duplicate findings, unsupported speculation, or expectations absent from the public task, evaluator rule, or discoverable repository contract.

## Independent lanes

Give each lane a distinct responsibility such as behavioral correctness, compatibility/integration, verification quality, or safety. Do not fan out identical generic prompts.

Reviewers work independently. Reducers cluster equivalent findings, preserve disagreement, reject unsupported findings, and request a targeted second pass only for a specific missing evidence surface.

## Adjudication

The adjudicator records:

- input judgment ids and their independence;
- supported, overturned, duplicate, and unresolved findings;
- the final categorical outcome and absolute acceptability;
- vetoes, missing evidence, and confidence;
- whether the result remains eligible for aggregation.

Adjudication resolves evidence conflicts; it does not change the task, acceptance rule, or analysis plan after seeing results. After adjudication is frozen, return the X/Y result to the controller for audited candidate-ID remapping.
