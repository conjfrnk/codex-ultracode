# Runtime

Use `python3 -m conductor_runtime` from a checkout or
`python3 conductor-runtime.pyz` from a release. State defaults to
`${CODEX_CONDUCTOR_HOME:-~/.codex/conductor}` and must remain outside the
workspace.

## Commands

| Command | Purpose |
| --- | --- |
| `auto` | Run a direct, goal, or planned workflow task. |
| `run` | Run or resume validated workflow JSON. |
| `validate` | Validate workflow JSON without execution. |
| `status` | Verify and inspect an external run. |
| `list` | List valid workflow files. |
| `apply` | Apply independently verified staged changes. |
| `doctor` | Check the local Codex CLI. |

## Direct

Read-only work uses one model call and no planner:

```sh
python3 -m conductor_runtime auto --task "TASK" --workspace . --allow-agent
```

`--allow-writes` runs the worker in an external staged copy and adds a read-only
verifier. `--plan-only` validates and persists the run without launching Codex.

## Goal And Parallel Work

Use a goal only for bounded verifier-driven repair:

```sh
python3 -m conductor_runtime auto --strategy goal --task "TASK" --workspace . \
  --allow-agent --allow-writes --max-iterations 3
```

Use a planned workflow only for genuinely independent read-only items that need
synthesis:

```sh
python3 -m conductor_runtime auto --strategy workflow --task "TASK" \
  --workspace . --allow-agent --allow-parallel --max-workers 4 \
  --execution-max-tokens 80000
```

This route adds one planner call. Every map is bounded, read-only, collected,
and synthesized once.

## Explicit Workflows

```sh
python3 -m conductor_runtime validate workflow.json
python3 -m conductor_runtime run workflow.json --workspace . --allow-agent
python3 -m conductor_runtime run workflow.json --workspace . \
  --allow-agent --resume RUN_DIR
```

Resume requires the original workflow, workspace, permissions, approvals, and
iteration context.

## Apply

```sh
python3 -m conductor_runtime apply STAGE_EVIDENCE.json --workspace . \
  --allow-writes --approve verified-stage-apply
```

Deletions also require
`--allow-destructive --approve verified-stage-delete`. Pending, failed, drifted,
or uninspected evidence must not be applied.

## Permissions And Failure

Agent, writes, destructive actions, network, parallel work, high risk, and
arbitrary shell commands require explicit capabilities or approval tokens.
Approval values are not persisted.

Provider and transport failures are never retried automatically. Goal mode
retries only a completed worker rejected by its verifier. Do not edit external
state to bypass a lock, hash, budget, or permission failure.
