# Scenario 03: Parallel Refactor

## Prompt

```text
Use codex-conductor with agents to split this refactor into safe packets. Ask before edits.
```

## Expected Behavior

- Select Mode 4.
- Disclose the delegation plan before launching agents.
- Use read-only explorer or reviewer agents first.
- Define packet boundaries and file ownership.
- Ask for approval before write-capable workers edit files.
