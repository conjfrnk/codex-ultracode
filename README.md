# Codex Conductor

Codex Conductor is a small, standard-library workflow runtime and Codex skill for bounded coding work.

The default path is model-first: one Codex call, no planning call, no daemon, and no repository-local state. Parallel orchestration is opt-in and constrained to map, collect, and synthesis. Write-capable work happens in an external staged copy and cannot be applied until a downstream verifier passes.

## Contents

- `codex-conductor/`: installable text-only Codex skill.
- `conductor_runtime/core/`: default runtime.
- `conductor_extras/`: optional compatibility and development CLI.
- `tests/test_core_runtime.py`: focused default-runtime tests.
- `dist/conductor-runtime.pyz`: dependency-free default zipapp.
- `dist/conductor-extras.pyz`: optional legacy and specialist zipapp.

Benchmark campaigns, provider evaluators, dashboards, team consoles, background services, cloud integrations, and historical compatibility code are not packaged in the default runtime.

## Build

```bash
python3 tools/package_runtime.py
python3 tools/package_extras.py
```

The default runtime build fails if its compressed zipapp reaches 500 KiB.

## Install

Extract `dist/codex-conductor-bundle.zip`, inspect the no-write plan, then install:

```bash
python3 install.py --dry-run
python3 install.py --allow-writes --approve conductor-install
```

The installer validates every installed byte, writes under `~/.codex`, and is idempotent. Replacing different installed bytes also requires `--replace --approve conductor-update`. It does not edit a repository, shell profile, scheduler, or service.

The runtime can also run directly:

```bash
python3 dist/conductor-runtime.pyz --version
python3 dist/conductor-runtime.pyz doctor
```

## Automatic Entry

Read-only work uses one model call:

```bash
python3 -m conductor_runtime auto \
  --task "Inspect the parser for correctness risks" \
  --workspace . \
  --allow-agent
```

`--strategy auto` and `--strategy direct` are equivalent. Neither launches a planner.

Write-capable work uses an external staged workspace and a read-only model verifier by default:

```bash
python3 -m conductor_runtime auto \
  --task "Fix the parser regression and verify it" \
  --workspace . \
  --allow-agent \
  --allow-writes
```

The source workspace remains unchanged. Apply the resulting evidence only after inspection:

```bash
python3 -m conductor_runtime apply /path/to/artifacts/stages/work.json \
  --workspace . \
  --allow-writes \
  --approve verified-stage-apply
```

Deletions additionally require `--allow-destructive --approve verified-stage-delete`.

Use `--plan-only` to validate and persist a direct run without launching Codex:

```bash
python3 -m conductor_runtime auto \
  --task "Inspect the parser" \
  --workspace . \
  --allow-agent \
  --plan-only
```

## Recovery

Resume an interrupted run with the exact original workflow, workspace, permissions, approvals, and iteration context:

```bash
python3 -m conductor_runtime run workflow.json \
  --workspace . \
  --allow-agent \
  --resume /path/to/run
```

For verifier-driven repair, `goal` retries only a failed verifier. Provider, launch, policy, and transport failures are never retried automatically:

```bash
python3 -m conductor_runtime auto \
  --strategy goal \
  --task "Fix the parser regression" \
  --workspace . \
  --allow-agent \
  --allow-writes \
  --max-iterations 3
```

Goal state is hash-bound and can be resumed with `--resume-goal` while supplying the same task and options.

## Parallel Work

Explicit workflow planning costs one additional model call and is the only automatic route that may create a map:

```bash
python3 -m conductor_runtime auto \
  --strategy workflow \
  --task "Review independent parser modules and synthesize the findings" \
  --workspace . \
  --allow-agent \
  --allow-parallel \
  --max-workers 4 \
  --execution-max-tokens 80000
```

Generated workflows are parsed as strict JSON, revalidated, and checked against step, item, worker, and aggregate token limits before execution. Every map must have exactly one collector and one bounded Codex synthesis step. Map workers are read-only.

Hand-authored `conductor.workflow.v1` files run through the same engine:

```bash
python3 -m conductor_runtime validate workflow.json
python3 -m conductor_runtime run workflow.json --workspace . --allow-agent
```

Arbitrary shell commands require the command-bound approval token printed by preflight. Shell steps use argv arrays and must explicitly declare write, destructive, and network intent.

## State And Security

State defaults to:

```text
${CODEX_CONDUCTOR_HOME:-~/.codex/conductor}/workspaces/<name>-<path-hash>/
```

The default runtime provides:

- strict workflow fields and dependency ordering;
- explicit agent, write, network, destructive, parallel, risk, and shell-command gates;
- per-call and aggregate map token budgets;
- bounded process time and output;
- process-group termination on timeout;
- no-follow regular-file reads and atomic state writes;
- workflow, workspace, policy, context, artifact, and receipt hashes;
- exact-binding resume and single-run locks;
- bounded dependency context marked as untrusted;
- source snapshots, external staging, verifier immutability checks, and transactional apply rollback;
- no persisted approval values.

Self-hashes detect accidental or uncoordinated mutation; they are not signatures against an attacker who can rewrite both artifacts and hashes. Codex quality still depends on the selected model, prompt, repository, and verification. This project does not claim universal superiority over Claude Code or any other tool.

## Optional Extras

Run specialist or historical commands from the separate archive:

```bash
python3 dist/conductor-extras.pyz --help
```

Extras are not installed or imported by the default runtime. They retain the previous broad command surface for compatibility and development use.

## Development

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_core_runtime
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
uvx --offline ruff check .
python3 tools/measure_runtime.py --output after.json
```

The release process also verifies reproducible packaging, isolated installation, active installation, and archive contents. See [`RELEASE_REPORT.md`](RELEASE_REPORT.md) for the measured `0.173.0` result.

## License

MIT. See `LICENSE`.
