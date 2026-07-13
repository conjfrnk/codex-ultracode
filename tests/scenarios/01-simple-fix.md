# Scenario 01: Simple Fix

## Prompt

```text
Use codex-conductor to fix this one failing unit test. Keep the change minimal.
```

## Expected Behavior

- Select Mode 2.
- Do not use subagents.
- Provide a brief plan before editing if the fix is non-trivial.
- Keep the diff minimal and limited to the failing behavior.
- Run the relevant test when possible and cite the result.
