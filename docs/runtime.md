# Core runtime

Run the core as `python3 -m conductor_runtime` from a source checkout, with the
path printed by the installer, or as `python3 conductor-runtime.pyz` from an
extracted release. The default installed path is
`~/.codex/conductor/bin/conductor-runtime.pyz`.

State defaults to
`${CODEX_CONDUCTOR_HOME:-~/.codex/conductor}/workspaces/<name>-<path-hash>/`
and must remain outside the source workspace. The examples below use the source
invocation.

For routine repository work, ask Codex to use `codex-conductor`. Use this CLI
when you need reproducible workflow JSON or direct control over state, budgets,
and permissions.

## Commands

| Command | Purpose |
| --- | --- |
| `auto` | Run a direct, goal, or planned workflow task. |
| `run` | Run or resume validated core workflow JSON. |
| `validate` | Validate core workflow JSON without execution. |
| `schema` | Print or write the core workflow JSON Schema. |
| `init` | Create a minimal read-only or staged-write workflow. |
| `migrate` | Convert a core-shaped legacy workflow to the core schema. |
| `status` | Verify and inspect an external run. |
| `results` | Verify and inspect captured output. |
| `list` | List valid core workflow files. |
| `apply` | Apply independently verified staged changes. |
| `doctor` | Check the local Codex CLI. |

Run `COMMAND --help` for the complete argument reference.

## Direct and staged work

Read-only work uses one model call and no planner:

```sh
python3 -m conductor_runtime auto --task "TASK" --workspace . --allow-agent
```

Adding `--allow-writes` runs the worker in an external staged copy and adds a
read-only verifier. The source is not changed automatically.

`--plan-only` validates and persists a direct run without launching Codex.
`--output PATH` explicitly exports a result, including inside the workspace,
and is no-clobber unless `--replace-output` is also supplied.

## Goal and workflow strategies

Use goal mode only for bounded verifier-driven repair:

```sh
python3 -m conductor_runtime auto --strategy goal --task "TASK" --workspace . \
  --allow-agent --allow-writes --max-iterations 3
```

Use workflow mode only when independent read-only work needs map-to-synthesis:

```sh
python3 -m conductor_runtime auto --strategy workflow --task "TASK" \
  --workspace . --allow-agent --allow-parallel --max-workers 4 \
  --execution-max-tokens 80000
```

Workflow mode adds one planner call, including with `--plan-only`. Every map is
bounded, read-only, collected, and synthesized once.

## Workflow files

The dependency-free core dialect is `conductor.core.workflow.v1`. The optional
extras runtime retains the distinct extended `conductor.workflow.v1` dialect.

```sh
python3 -m conductor_runtime schema --output core-workflow.schema.json
python3 -m conductor_runtime init review.json --template read-only
python3 -m conductor_runtime init change.json --template staged-write
python3 -m conductor_runtime validate review.json change.json
python3 -m conductor_runtime run review.json --workspace . --allow-agent
```

Generated examples are under `conductor-workflows/core/`. Convert a core-shaped
legacy workflow without overwriting it:

```sh
python3 -m conductor_runtime migrate legacy.json --output migrated.json
```

Resume requires the original workflow, workspace, permissions, approvals, and
iteration context:

```sh
python3 -m conductor_runtime run review.json --workspace . --allow-agent \
  --resume RUN_DIR
```

## Captured output

When a shell or Codex stream exceeds its inline limit, the runtime can retain a
redacted result outside the workspace: up to 32 MiB per stream and 256 MiB per
run. Use opaque ids and cited line ranges instead of opening store files:

```sh
python3 -m conductor_runtime results list RUN_DIR
python3 -m conductor_runtime results outline RUN_DIR RESULT_ID
python3 -m conductor_runtime results search RUN_DIR --query "literal text"
python3 -m conductor_runtime results get RUN_DIR RESULT_ID --start-line 120 --max-lines 40
```

`pipe_complete` reports whether the captured stream was fully drained;
`producer_status` separately reports completion, failure, or timeout.
`text_fidelity` marks replacement decoding. Optional capture failures do not
overwrite a successful producer status; control-stream truncation, integrity
violations, and descendants that prevent pipe closure still fail the step.

## Apply

Apply inspected, independently verified staged evidence explicitly:

```sh
python3 -m conductor_runtime apply STAGE_EVIDENCE.json --workspace . \
  --allow-writes --approve verified-stage-apply
```

Deletions additionally require
`--allow-destructive --approve verified-stage-delete`. Pending, failed,
drifted, or uninspected evidence is rejected. Apply makes no provider call and
is serialized per workspace.

## Permissions and recovery

Agent calls, writes, destructive actions, network access, parallel work, high
risk, and shell commands require explicit capabilities or exact approvals.
Shell approvals bind the executable's canonical identity, content hash, and
exact arguments. Approval values are not persisted. Provider and shell
processes receive a bounded environment without arbitrary ambient variables,
and retained secrets are redacted from output.

A blocked run needs permission or approval, not retry. Provider and transport
failures are never retried automatically; goal mode retries only a completed
worker rejected by its verifier. Resume and apply recheck state, stage quotas,
and source bindings before proceeding.
