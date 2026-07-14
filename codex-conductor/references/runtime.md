# Runtime

Prefer an installed `conductor-runtime` command. From a source checkout use `python3 -m conductor_runtime`; from a release use `python3 conductor-runtime.pyz`.

State defaults to `${CODEX_CONDUCTOR_HOME:-~/.codex/conductor}/workspaces/<name>-<path-hash>/`.

## Direct

Read-only, one model call:

```bash
python3 -m conductor_runtime auto --task "TASK" --workspace . --allow-agent
```

Write-capable, staged plus read-only verification:

```bash
python3 -m conductor_runtime auto --task "TASK" --workspace . --allow-agent --allow-writes
```

Zero-call validation:

```bash
python3 -m conductor_runtime auto --task "TASK" --workspace . --allow-agent --plan-only
```

## Goal

Use only with bounded verification-driven repair:

```bash
python3 -m conductor_runtime auto --strategy goal --task "TASK" --workspace . \
  --allow-agent --allow-writes --max-iterations 3
```

Resume with the same task, workspace, policy, options, and `--resume-goal GOAL.json`.

## Parallel Workflow

Use only for independent items that need synthesis:

```bash
python3 -m conductor_runtime auto --strategy workflow --task "TASK" --workspace . \
  --allow-agent --allow-parallel --max-workers 4 --execution-max-tokens 80000
```

This route pays for one planner call. Generated maps are read-only and must flow through one collector and one synthesis step.

## Explicit Workflow

```bash
python3 -m conductor_runtime validate workflow.json
python3 -m conductor_runtime run workflow.json --workspace . --allow-agent
python3 -m conductor_runtime run workflow.json --workspace . --allow-agent --resume RUN_DIR
python3 -m conductor_runtime status RUN_DIR
```

Resume requires exact workflow, workspace, policy, approval, and iteration-context bindings.

## Apply

Source workspaces are unchanged until explicit apply:

```bash
python3 -m conductor_runtime apply STAGE_EVIDENCE.json --workspace . \
  --allow-writes --approve verified-stage-apply
```

Deletions also require `--allow-destructive --approve verified-stage-delete`.

Do not apply pending, failed, drifted, or uninspected evidence. Application performs no provider or verifier call.

## Shell Checks

Shell steps use argv arrays and explicitly declare write, destructive, and network intent. Commands other than inert `true`, `false`, and `test` probes require the exact command-bound approval token reported by preflight.

## Failure

- A blocked run needs permission or approval, not retry.
- A failed provider or transport call is not automatically retried.
- A goal may retry only a completed worker whose verifier failed.
- Do not edit run state to bypass a lock, hash, budget, or permission failure.
