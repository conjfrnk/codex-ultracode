# Core runtime

Use `python3 -m conductor_runtime` from a source checkout, the runtime path
printed by the installer, or `python3 conductor-runtime.pyz` from an extracted
release. The default installed path is
`~/.codex/conductor/bin/conductor-runtime.pyz`.

State defaults to
`${CODEX_CONDUCTOR_HOME:-~/.codex/conductor}/workspaces/<name>-<path-hash>/`
and must remain outside the source workspace. The examples below use the source
invocation; replace it with the installed path after installation.

## Skill-first quickstart

For repository work, ask Codex to use `codex-conductor` and describe the bounded
outcome. The Skill chooses direct execution for one obvious task and uses
map-to-synthesis only when independent investigation materially helps. Use the
low-level CLI below when you need a reproducible workflow file or direct control
over state, budgets, and permissions.

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
| `results` | Verify, list, search, outline, or read captured stream overflow. |
| `list` | List valid core workflow files. |
| `apply` | Apply independently verified staged changes. |
| `doctor` | Check the local Codex CLI. |

## Direct

Read-only work uses one model call and no planner:

```sh
python3 -m conductor_runtime auto --task "TASK" --workspace . --allow-agent
```

`--allow-writes` runs the worker in an external staged copy and adds a read-only
verifier. `--plan-only` validates and persists the direct run without launching
Codex. `--output PATH` is an explicit result export and may name a workspace
path; it is no-clobber unless `--replace-output` is also supplied.

## Goal and parallel work

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

The workflow route adds one planner call, including with `--plan-only`. Every
map is bounded, read-only, collected, and synthesized once.

## Authoring core workflows

The dependency-free core dialect is `conductor.core.workflow.v1`. The optional
extras runtime retains the extended legacy `conductor.workflow.v1` dialect;
the formats are intentionally distinct.

```sh
python3 -m conductor_runtime schema --output core-workflow.schema.json
python3 -m conductor_runtime init review.json --template read-only
python3 -m conductor_runtime init change.json --template staged-write
python3 -m conductor_runtime validate review.json change.json
python3 -m conductor_runtime run review.json --workspace . --allow-agent
```

The generated examples are also shipped under `conductor-workflows/core/`.
Legacy core-shaped files can be converted without overwriting the source:

```sh
python3 -m conductor_runtime migrate legacy.json --output migrated.json
```

Resume requires the original workflow, workspace, permissions, approvals, and
iteration context:

```sh
python3 -m conductor_runtime run review.json --workspace . --allow-agent \
  --resume RUN_DIR
```

## Recover truncated process output

Shell and Codex streams keep their existing bounded inline prefix. When a
stream crosses that limit, the runtime also retains up to 32 MiB as a redacted,
immutable result container under the external run directory. A 256 MiB
per-run quota fails capture explicitly without eviction. Raw bytes remain in
anonymous temporary files only until redaction; they never receive a durable
pathname. Capture storage activates only after overflow. Fan-out divides the
capture allowance by the active wave, and a 256 MiB process-wide reservation
cap bounds raw in-flight capture. Capture allocation, finalization, or budget
failure is recorded explicitly without changing an otherwise successful shell
producer result. Operational result-store I/O loss is reported the same way;
path, binding, or content-integrity violations still fail closed. Redaction and
commit are serialized.

Use opaque ids and cited line ranges instead of opening store files directly:

```sh
python3 -m conductor_runtime results list RUN_DIR
python3 -m conductor_runtime results outline RUN_DIR RESULT_ID
python3 -m conductor_runtime results search RUN_DIR --query "literal text"
python3 -m conductor_runtime results get RUN_DIR RESULT_ID --start-line 120 --max-lines 40
```

`pipe_complete` says whether the container holds every byte observed through a
cleanly drained pipe. It does not mean the producer succeeded; `producer_status`
reports completion, failure, or timeout separately. `text_fidelity` discloses
replacement decoding for non-UTF-8 streams. Codex control-stream truncation
still fails the step and is never accepted as terminal evidence. Shell and Codex
steps also fail if an escaped descendant keeps either process pipe from closing;
collection remains deadline-bounded. Run inspection verifies container,
descriptor, content, state-reference, packet, hardlink, and quota bindings
before retrieval. Inspection uses shared, non-mutating locks and reads one
container at a time; a following write safely removes a recognized
interrupted-write temporary.

## Apply

The source workspace remains unchanged until explicit application:

```sh
python3 -m conductor_runtime apply STAGE_EVIDENCE.json --workspace . \
  --allow-writes --approve verified-stage-apply
```

Deletions also require
`--allow-destructive --approve verified-stage-delete`. Pending, failed, drifted,
or uninspected evidence must not be applied. Application performs no provider
or verifier call and is serialized per workspace.

## Permissions and failure

Agent calls, writes, destructive actions, network access, parallel work, high
risk, and every shell command require explicit capabilities or exact approval
tokens. A shell approval binds the executable's canonical location, content
hash, and exact arguments; an executable inside the workspace is revalidated
and launched from the isolated copy. Approval values are not persisted.
Launched provider and shell processes get a bounded environment; retained
secret values are redacted from persisted and terminal output. Arbitrary
ambient variables are not forwarded; workflows that depended on build-specific
environment variables must express those inputs through files or approved
command arguments.

A blocked run needs permission or approval, not retry. Provider and transport
failures are never retried automatically. Goal mode retries only a completed
worker rejected by its verifier. Do not edit external state to bypass a lock,
hash, budget, or permission failure. Stage quotas are rechecked after provider
completion or timeout and again on resume/apply; an abrupt host kill can leave
external staged bytes until that run is resumed or cleaned up.
