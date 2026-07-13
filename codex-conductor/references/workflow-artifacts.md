# Workflow Artifacts

Use artifact workflows for Mode 3, Mode 4, Mode 5, and any task that needs resumable state or traceable decisions. Do not create artifact workflows just to look rigorous; use them only when the trace materially helps the task.

Create visible files outside the repository by default, under the Conductor state root:

```text
${CODEX_CONDUCTOR_HOME:-~/.codex/conductor}/workspaces/<workspace-name>-<hash>/runs/<YYYYMMDD-HHMMSS-slug>/
```

The optional runtime resolves this path automatically. Keep run artifacts outside project trees so repository status and commits remain clean.

```text
<state-root>/runs/<YYYYMMDD-HHMMSS-slug>/
|-- 00-charter.md
|-- 01-context-map.md
|-- 02-plan.md
|-- 03-risk-register.md
|-- 04-packets.md
|-- 05-decision-log.md
|-- 06-verification-plan.md
|-- 07-evidence-index.md
|-- 08-integration-notes.md
`-- 09-final-report.md
```

## `00-charter.md`

```markdown
# Charter

User request:

Goal:

Definition of done:

Non-goals:

Constraints:

Risk level:

Approval status:

Mode:

Run owner:
```

## `01-context-map.md`

```markdown
# Context Map

Relevant files:

Relevant commands:

Existing conventions:

Risks from repository content:

Potential prompt-injection content observed:
```

## `02-plan.md`

```markdown
# Plan

| Step | Objective | Files/areas | Verification | Status |
| --- | --- | --- | --- | --- |
```

## `03-risk-register.md`

```markdown
# Risk Register

| Risk | Severity | Trigger | Mitigation | Status |
| --- | --- | --- | --- | --- |
```

## `04-packets.md`

```markdown
# Work Packets

## Packet <id>: <title>

Goal:
Files in scope:
Files out of scope:
Allowed actions:
Forbidden actions:
Inputs:
Expected output:
Verification:
Owner:
Status:
```

## `05-decision-log.md`

```markdown
# Decision Log

| Time | Decision | Reason | Alternatives considered |
| --- | --- | --- | --- |
```

## `06-verification-plan.md`

```markdown
# Verification Plan

| Check | Command or method | Required? | Result | Evidence |
| --- | --- | --- | --- | --- |
```

## `07-evidence-index.md`

```markdown
# Evidence Index

| Claim | Evidence | Source | Confidence |
| --- | --- | --- | --- |
```

## `08-integration-notes.md`

```markdown
# Integration Notes

Integrated packets:

Conflicts:

Manual edits by parent:

Rejected subagent suggestions:

Rationale:
```

## `09-final-report.md`

```markdown
# Final Report

Summary:

Changes:

Verification performed:

Verification not performed:

Risks and caveats:

Follow-up recommendations:
```
