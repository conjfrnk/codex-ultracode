# Conductor Runtime

`conductor_runtime` is the optional companion runtime for Codex Conductor. The Skill folder remains text-only; release bundles ship the dependency-free runtime beside it as `conductor-runtime.pyz`.

Each bundle includes a strict `conductor.release_bundle.v1` manifest and dependency-free `install.py`. The installer checks every runtime, installer, and Skill file against that manifest, rejects symlinked or unmanifested Skill content, supports a no-write dry run, and installs versioned plus active copies under external Codex/Conductor homes. A first install requires `--allow-writes --approve conductor-install`; replacing differing bytes also requires `--replace --approve conductor-update`. Identical reruns perform no writes. Installation does not modify repositories, PATH, shell startup, services, or schedulers, and a caught multi-target commit failure restores prior targets.

The runtime gives the project mechanical workflow behavior:

- declarative JSON workflow specs,
- visible run directories under the external Conductor state root,
- standard conductor artifacts for charter, plan, risk, decisions, evidence, and final report,
- dependency-free JSON Schema export for public workflow, reusable agent profile with immutable progressive Skill-tree bindings, agent memory, content-free Codex progress and single-step terminal evidence, agent-team state and turn envelopes, desktop-notification receipts, benchmark, Claude/Codex provider evidence, benchmark score/quality evidence, manifest, parity, evidence, routine manifest/control/history, and model-workflow receipt artifact types,
- resumable `state.json`,
- prepared run launch manifests with `prepare-run` plus explicit detached per-run launch with `start-background-run`, visible through status/wait commands, `list-runs`, diagnostics, and dashboard details,
- explicit content-free desktop completion notifications for direct, saved, model-authored, reviewed, automatic, goal, detached, and routine/service execution, with atomic no-replay receipts and fail-open local backends,
- bounded adaptive goal loops with command, fail-closed read-only model, or ordered command-then-model verification, resumable external goal state, optional explicit detached model-goal execution, and `list-goals`/`status-goal` inspection,
- per-run `runner.json` heartbeat and owner metadata with liveness classification for active/final workflow visibility,
- execute-wide advisory ownership plus bounded active child PID/process-group/session provenance for concurrent-resume prevention and interrupted-run recovery,
- strict streamed `codex_exec` thread checkpoints, content-free ordinary-call and map-packet progress journals, pre-return final-turn evidence for zero-replay local recovery, and explicit hash-bound same-thread continuation when that evidence is absent after proven local process loss,
- deterministic run, phase, and step timing metadata for new runs,
- low-risk shell execution with policy gates,
- Codex `exec` integration behind explicit `--allow-agent`, with automatic structured event and measured token capture,
- deterministic `auto` routing between one-shot model workflows and verifier-driven adaptive goals, with a safe plan/preflight fallback when execution is not approved,
- bounded parallel `agent_map` execution behind `--allow-agent --allow-parallel`, with workflow and step worker limits up to 128 and an additional `high-scale-agent-map` approval above 16 actual workers,
- bounded `agent_team` coordination behind `--allow-agent --allow-parallel`, with read-only default operation plus opt-in `--allow-writes` isolated member workspaces and parent-owned deterministic merges, persistent member sessions, a dependency-ordered task board, deterministic claims, next-turn mailboxes, resumable state, reset generations, lead synthesis, optional parent-validated lead task proposals, and an opt-in private redacted operator transcript,
- reusable policy-bounded agent profiles with specialty instructions, model/effort locking, sandbox/risk/resource ceilings, hash-bound workspace context, and bounded external cross-run memory with compatible recent or deterministic lexical-relevance selection,
- per-packet `agent_map` result caching and atomic completion receipts for compatible retry/resume runs, with optional bounded multi-item packets, whole-step invalidation, and canonical packet-hash-bound targeted retry,
- dynamic packet expansion from explicit lists, workspace item files, or generated artifacts,
- deterministic workflow draft generation from task text, failing commands, verification commands, and packet sources,
- instruction-only live parity runbook generation from parity task fixtures,
- an approval-gated read-only Claude Sonnet Ultracode parity runner with strict stream, model, budget, and partial-output evidence,
- an approval-gated source-isolated Codex read-only parity runner with explicit model/effort, strict sandbox/configuration, measured token, and source/stage integrity evidence,
- hash-bound benchmark rubric scoring with execution/quality separation and independent-review claim gates,
- safe saved workflow commands under `.claude/workflows/` using either non-executed JSON-compatible Conductor exports or a statically compiled Claude-style `agent()`/`pipeline()`/`phase()`/static-`parallel()` subset,
- durable routine manifests, controls, bounded history, catch-up, and explicitly approved immediate, foreground-scheduled, or detached local execution,
- gated Codex Cloud submission, status, diff review receipts, and unchanged-diff apply,
- redacted parity evidence bundles that aggregate local run, goal, routine, benchmark, and comparison evidence behind a fail-closed claim gate,
- static HTML dashboard generation for runs, goal-loop artifacts and detached model-goal workers, prepared launch handoffs, phases, artifacts, agent-map packet traces/evidence, and benchmark summaries,
- loopback-default live dashboard serving with a cross-run manual-gate approval queue, token-gated named-teammate follow-up forms, goal-loop and detached-worker summaries, prepared launch visibility, selected-run workflow JSON export, agent-packet trace rows, plus distinct fresh and same-thread interrupted-run recovery, packet retry, step retry/reset/skip, and run-level pause/resume/stop/restart/terminate controls,
- terminal run snapshots with `list-runs`, including prepared launch status when present,
- a dependency-free `run-console` one-shot or bounded follow view over validated run state and content-free Codex progress, with no additional provider call,
- local session trace summary import with `import-session-trace`,
- optional policy-gated local workflow hooks around run, step, provider-process, and Codex-supported provider-native pre-tool events,
- recovery controls for reconciling interrupted owners, retrying individual packets or steps, resetting, skipping, restarting, and exporting runs before `--resume` or reuse,
- bounded no-follow reads for workflow metadata, run state, prompt files, templates, agent-map cache metadata, rendered excerpts, and cache fingerprint hashing,
- redaction before writing logs and artifacts,
- manual gates using `--approve <token>`.

Default state location:

Generated runtime state is stored outside the repository, under `${CODEX_CONDUCTOR_HOME:-$HOME/.codex/conductor}/workspaces/<workspace-name>-<hash>/`. This includes runs, goals, routine manifests, benchmark/evidence directories used by default commands, and the default dashboard output. Set `CODEX_CONDUCTOR_HOME` to move the whole state tree. Runtime artifacts should not be written into project trees.

## Quick Start

Validate templates:

```bash
python3 -m conductor_runtime validate conductor-workflows
```

List or write public JSON Schemas:

```bash
python3 -m conductor_runtime schema --list
python3 -m conductor_runtime schema workflow
python3 -m conductor_runtime write-schema workflow "$HOME/.codex/conductor/generated/workflow.schema.json"
python3 -m conductor_runtime write-schema routine-manifest "$HOME/.codex/conductor/generated/routine-manifest.schema.json"
python3 -m conductor_runtime write-schema routine-controls "$HOME/.codex/conductor/generated/routine-controls.schema.json"
python3 -m conductor_runtime write-schema routine-state "$HOME/.codex/conductor/generated/routine-state.schema.json"
python3 -m conductor_runtime write-schema desktop-notification "$HOME/.codex/conductor/generated/desktop-notification.schema.json"
```

Inspect a workflow:

```bash
python3 -m conductor_runtime inspect conductor-workflows/repo-audit.json
```

Dry-run a workflow:

```bash
python3 -m conductor_runtime run conductor-workflows/repo-audit.json --dry-run
```

Run a local shell-only verification workflow:

```bash
python3 -m conductor_runtime run conductor-workflows/verification-sweep.json --allow-writes
python3 -m conductor_runtime run conductor-workflows/verification-sweep.json --allow-writes --desktop-notify
```

The verification sweep runs the repository's test suite. It is local, but it executes repository code and therefore requires explicit capability flags.

Run a bounded foreground goal loop:

```bash
python3 -m conductor_runtime run-goal conductor-workflows/verification-sweep.json \
  --allow-writes \
  --check-command-json '["python3","-B","-m","unittest","discover","-s","tests"]' \
  --max-iterations 3
python3 -m conductor_runtime list-goals
python3 -m conductor_runtime status-goal <goal-json>
```

`run-goal` repeats the workflow until verification succeeds or the iteration cap is exhausted. It accepts a command verifier, model criteria from `--check-prompt` or workspace-relative `--check-prompt-file`, or both. With both, the command runs first; failure or timeout records bounded feedback and zero model-verifier calls, while success permits exactly one read-only model review. A workflow that itself fails on exactly one valid unsatisfied `completion_verdict` may also continue: the failed run remains failed, its private bounded feedback is framed as untrusted input for the next authorized iteration, and the external verifier is not called for the failed iteration. Goal state and public detail retain only a strict step/contract/confidence/count/size/hash receipt. Invalid, satisfied, missing, multiple, changed, or over-limit completion evidence does not retry. Model verification requires `--allow-agent` and runs in a fixed high-quality envelope: explicit `high` effort, an 8,000 weighted-token runtime-hard cap, exact 4,000-token completion checkpoint, strict isolated read-only/null-root profile, disabled optional/alternate execution surfaces, and an ephemeral session. It requires a strict evidence-bearing JSON verdict, fails closed on cutoff/timeout/provider/malformed-output errors, stores only the criteria hash and length, and records policy plus usage beside the verdict. Unsatisfied model verdicts remain retryable feedback. To continue after an interruption or raise the iteration cap, rerun the same workflow and complete verifier chain with `--resume-goal <goal-json>`; resume validates the workflow, composite verifier fingerprints, and any private completion-feedback receipt before another call. One exact pre-0.48 model/composite fingerprint may migrate to the tighter policy, records that migration, and rewrites the current fingerprint before another call.

Use model verification when completion cannot be represented by one deterministic command:

```bash
python3 -m conductor_runtime run-goal conductor-workflows/implementation-packets.json \
  --allow-agent \
  --allow-writes \
  --approve write-packets \
  --check-prompt "The requested implementation is complete and relevant tests provide concrete passing evidence." \
  --max-iterations 3
```

Combine deterministic tests and semantic review by supplying both. The model call is skipped until the command passes:

```bash
python3 -m conductor_runtime run-goal conductor-workflows/implementation-packets.json \
  --allow-agent \
  --allow-writes \
  --approve write-packets \
  --check-command-json '["python3","-B","-m","unittest","discover","-s","tests"]' \
  --check-prompt "The implementation satisfies the complete public contract with concrete evidence." \
  --max-iterations 3
```

Run a Codex-backed read-only workflow:

```bash
python3 -m conductor_runtime run conductor-workflows/parallel-review.json --allow-agent --allow-parallel
python3 -m conductor_runtime run conductor-workflows/coordinated-team-review.json --allow-agent --allow-parallel
python3 -m conductor_runtime run conductor-workflows/adaptive-team-review.json --allow-agent --allow-parallel
python3 -m conductor_runtime run conductor-workflows/isolated-implementation-team.json --allow-agent --allow-parallel --allow-writes --approve isolated-team-write
python3 -m conductor_runtime run conductor-workflows/plan-approved-implementation-team.json --allow-agent --allow-writes --approve plan-gated-team-write
python3 -m conductor_runtime run conductor-workflows/operator-approved-implementation-team.json --allow-agent --allow-writes --approve operator-plan-gated-team-write
```

Create and bind a reusable agent profile without launching Codex:

```bash
python3 -m conductor_runtime write-agent-profile reviewer.json \
  --name correctness-reviewer \
  --instructions-file reviewer-instructions.md \
  --model gpt-5.6-sol \
  --effort high \
  --max-tokens 4000 \
  --sandbox read-only \
  --tool-policy isolated \
  --tool-output-token-limit 2000 \
  --skill .agents/skills/correctness-review \
  --memory-mode read-only \
  --memory-selection relevant \
  --memory-max-entries 16 \
  --memory-max-bytes 32768
python3 -m conductor_runtime validate-agent-profile reviewer.json
python3 -m conductor_runtime inspect-agent-profile reviewer.json
python3 -m conductor_runtime bind-agent-profile \
  conductor-workflows/parallel-review.json reviewer.json profiled-review.json \
  --step fanout-review
python3 -m conductor_runtime run profiled-review.json --allow-agent --allow-parallel
```

Create a command-restricted variant with structured rules:

```bash
python3 -m conductor_runtime write-agent-profile restricted-reviewer.json \
  --name restricted-reviewer \
  --instructions-file reviewer-instructions.md \
  --model gpt-5.6-sol \
  --effort high \
  --max-tokens 4000 \
  --sandbox read-only \
  --tool-policy restricted \
  --tool-output-token-limit 2000 \
  --skill .agents/skills/correctness-review \
  --allow-skill-script-json '{"skill":".agents/skills/correctness-review","script":"scripts/verify.sh","args":[]}' \
  --allow-skill-mcp-json '{"skill":".agents/skills/correctness-review","name":"openaiDeveloperDocs","url":"https://developers.openai.com/mcp","tools":["search_openai_docs","fetch_openai_doc"]}' \
  --allow-command-json '["git","status","--short"]' \
  --allow-command-prefix-json '["git","diff"]' \
  --allow-command-prefix-json '["rg"]'
python3 -m conductor_runtime validate-agent-profile restricted-reviewer.json
```

An authenticated route keeps values out of the profile and names only their environment sources:

```json
{
  "skill": ".agents/skills/private-docs",
  "name": "privateDocs",
  "url": "https://docs.example.com/mcp",
  "tools": ["search"],
  "auth": {
    "bearer_token_env_var": "PRIVATE_DOCS_TOKEN",
    "env_http_headers": {
      "X-API-Key": "PRIVATE_DOCS_API_KEY"
    }
  }
}
```

`conductor.agent_profile.v1` is strict and reusable across selected `codex_exec`, `agent_map`, or `agent_team` member executions in the bound workflow. A profile may lock model and effort, cap weighted tokens, workers, timeout, and isolated tool output, set a sandbox ceiling and risk floor, reference up to 16 workspace-relative context files, explicitly select up to eight progressive Skill trees, and opt into bounded Conductor memory with `recent` or `relevant` selection. Steps may narrow numeric or sandbox limits, but model/effort replacement, resource increases, and read-only-to-write widening are rejected during validation. Optional `tool_policy: isolated` adds named minimal workspace filesystem permissions, denied network and web search, ignored user config/project rules, a non-login sanitized environment with user startup/config roots redirected to the null device, and disabled native hook/plugin/app/browser/computer/memory/dependency/nested-agent surfaces. Omitted policy remains `standard` and preserves the legacy command shape. Context files are regular symlink-free bounded UTF-8; isolated and restricted profiles additionally reject `.env`, PEM, and key context paths. Ordered path, size, mode, and SHA-256 bindings enter the persisted workflow fingerprint, while raw static context stays out of lifecycle metadata. Profile or static-resource mutation invalidates preflight-to-execution handoff, resume, packet cache reuse, later goal iterations, and scheduled routine authorization before a provider starts.

Fresh sessions receive the complete profile section. Same-session direct recovery and resumed team plan, review, turn, and quality-retry calls re-read and hash-verify that same complete section, then transmit a compact receipt containing its SHA-256 and byte count only when the receipt is smaller. The exact session remains checkpoint/team-state-bound, and the receipt grants no new authority. Tiny sections keep the full text. `inspect-agent-profile --workspace PATH` reports `full_static_sha256`, full/resumed bytes, selected mode, and exact saved bytes without a provider call or raw-content output.

### Agent Profile Skills

Each repeatable `write-agent-profile --skill DIR` value, or each item in a profile's `skills` array, names one canonical workspace-relative Skill directory. Conductor follows the [Codex Skill progressive-disclosure shape](https://developers.openai.com/codex/skills): it injects the explicitly selected `<DIR>/SKILL.md`, then leaves optional local files to on-demand reads through the profile's existing tools. The entrypoint is a regular, non-empty UTF-8 file with no NUL and no more than 128 KiB. The complete selected directory is symlink-free and may contain at most 256 additional regular files, eight path levels, 8 MiB per resource, and 32 MiB of resources. One profile may select eight Skills; one workflow may bind 64 entrypoints, 256 KiB of entrypoint text, 2,048 resources, and 32 MiB of resource bytes. Special files, set-id/sticky modes, `.git`, `.env`, PEM/key paths, absolute paths, dot or parent components, ambiguous separators, duplicates, and control characters fail validation.

Before run creation, Conductor writes `conductor.agent_profile_skill_bindings.v2` into the copied workflow. Each selected Skill record stores its ordered directory path, entrypoint size/mode/hash, canonical tree hash, and an ordered content-free resource manifest containing relative path, size, SHA-256, and executable mode. It never copies entrypoint or resource content into the binding, state summaries, traces, or dashboards. Direct calls, map packets, team planning/review/turn/quality-retry calls, packet cache identity, resume, and routine authorization bind or revalidate the exact tree. Provider launches verify it immediately before and after every call, so add/delete/content/mode drift during a call fails closed. `inspect-agent-profile --workspace PATH` reports the count, bytes, and set hash without a provider.

Prompts label the selected entrypoint and tree as operator-selected, bounded, untrusted, and subordinate to current system, developer, user, Conductor safety, profile tool policy, and step scope. Optional resources are not preloaded and consume no prompt tokens; the model reads only a resource named by the selected Skill when needed. Tree binding alone does not execute scripts, enable MCP servers/apps/integrations, grant network access, or widen tools or permissions. Conductor does not use ambient native Skill discovery, and model-authored workflows cannot choose profile Skills. Omitting `skills` returns before binding and performs no Skill lookup, tree scan, file read, prompt branch, cache field, state metric, or command work.

`tool_policy: restricted` retains the isolated filesystem/network/tool envelope while enabling one Conductor-owned session `PreToolUse` hook. `command_allowlist` contains structured `argv` exact rules or `argv_prefix` rules; `allow_apply_patch` separately controls the patch tool. `skill_scripts` may add up to 32 operator-authored routes, each naming one already-selected Skill and one non-empty executable resource under its `scripts/` directory with either exact `args` or explicit `args_prefix`. The derived relative script argv is merged into the same command policy, whose combined limit remains 64 rules. No script runs automatically, and no other resource becomes executable.

`skill_mcp_dependencies` may add up to eight operator-authored routes, each naming one already-selected Skill, one safe MCP server name, and 1-32 exact tool names; the profile-wide tool limit is 64. A legacy HTTPS route pins one URL without embedded/query/fragment credentials. A `transport: "stdio"` route instead pins one executable non-empty resource under that Skill's `scripts/` directory and an exact argument array; it accepts no URL, auth, environment, interpreter, package runner, or arbitrary executable. On bind and resume, Conductor launches the installed Codex app-server only for opted-in profiles, disables native automatic Skill dependency installation, calls `skills/list` with `forceReload`, locates the exact selected `SKILL.md`, and requires one enabled repository Skill whose parsed dependency exactly matches the pinned name and HTTPS URL or expansion-free stdio command. It does not parse YAML itself. Every route requires `--approve skill-mcp:<name>`; HTTPS also requires `--allow-network`, while stdio separately requires `--approve skill-mcp-stdio:<name>` and grants no runtime network capability. Model-authored workflows cannot select the Skill.

For each actual stdio provider call, Conductor revalidates the complete bound Skill tree, copies it into a fresh mode-`0700` external route directory, makes the snapshot read-only, and leaves only the pinned script executable. Codex's process-local MCP config contains a Conductor self-proxy command plus a mode-`0600` receipt path and SHA-256, never the actual script command. The proxy consumes that strict receipt, rehashes every snapshot file and mode, sanitizes the child environment, creates a private writable home/temp, and supervises one process group. On macOS, Seatbelt denies network, workspace/home reads and writes, process forking, and writes outside the private runtime; on Linux, bubblewrap uses separate network/PID namespaces, read-only system runtime roots and Skill snapshot, hidden home/root, and only the private runtime bind as writable. Unsupported hosts or missing sandbox tools fail before the provider starts. Normal and forced cleanup terminate a recorded child process group and remove the receipt, snapshot, and temp tree. Direct, map, team, and guarded same-thread recovery create a fresh snapshot/proxy per process invocation. No daemon, persistent server/config, repository artifact, ambient credential, or provider transcript is added. The selected program and all MCP output remain untrusted; this v1 route intentionally does not support package installation, ambient language environments, arbitrary stdio commands, or OSes without the required sandbox.

Optional `auth` may contain only `bearer_token_env_var` and/or `env_http_headers`. Header names are RFC-token-shaped, case-insensitively unique, capped at 16, and cannot include `Authorization`, `Host`, `Content-Length`, proxy authorization, or hop-by-hop routing headers. Environment names are bounded identifiers. At real launch, every referenced value must be present, trimmed, control-free, 8-16,384 UTF-8 bytes, and a bearer value may contain no whitespace. Credential access requires the additional `--approve skill-mcp-auth:<name>`. Conductor never places a value in the profile, command, workflow, state, trace, hash, cache identity, or dashboard. It holds approved values transiently for exact raw/JSON-escaped redaction of provider output and deletes them from its registry after the step.

Each authenticated provider call starts one dependency-free in-process broker on a random opaque `http://127.0.0.1` route. The provider command replaces only authenticated server URLs with those loopback capabilities and omits every bearer/header environment name. The provider environment removes selected variables and any inherited alias containing an approved value. The broker opens only the exact operator-pinned HTTPS targets, strips client authorization and hop-by-hop headers, injects approved headers outside the provider process, rejects redirects plus upstream `401`/`403`, allows only MCP GET/POST/DELETE, and bounds request bodies, response bodies, headers, connect time, and the enclosing provider timeout. Allowed response headers and buffered or SSE bodies are exact-redacted for raw and JSON-escaped values before the provider receives them. Broker shutdown closes stalled local and upstream sockets and clears all routes; it writes no config, transcript, OAuth store, daemon, or repository state.

Because ambient client authorization is stripped and upstream authentication failures are never forwarded as authentication challenges, stored MCP OAuth cannot reach the approved endpoint. Ordinary Codex session persistence remains enabled, so authenticated direct `codex_exec`, `agent_map`, `agent_team`, and guarded `recover-run --resume-codex` continuations use the same thread semantics as unauthenticated routes, with a fresh broker capability on every process call. Literal/static headers, OAuth login or stored OAuth use, query credentials, arbitrary/ambient stdio commands, ambient user servers, and automatic dependency activation remain unsupported. The approved endpoint remains trusted for semantic behavior. Exact redaction does not claim to detect arbitrary transformed, encoded, or split credential material returned by a malicious server.

Command-policy v1 remains unchanged when no MCP route exists. Opted-in profiles emit v2 with an exact `tool_allowlist` of `mcp__<server>__<tool>` names. The Python evaluator, embedded restricted hook, and composed workflow-native bridge accept both versions and deny every unlisted tool. Every Bash call must still parse as one expansion-free simple command. Operators, substitutions, globs, assignments, redirects, comments, and malformed quoting are denied. A prefix rule intentionally grants all trailing arguments, including whatever behavior the selected executable exposes, so prefer exact rules for dangerous argument languages. The runtime pins restricted profiles to Codex CLI `>=0.143.0,<0.145.0`, disables `unified_exec` and alternate shell/code surfaces, discovers the generated session-hook hash, binds trust to that hash, disables every external unmanaged hook, preserves managed administrator hooks, and verifies the effective hook list before launching an agent. The runtime passes all hook and MCP settings through session CLI configuration and writes no repository-local runtime/config directory. State, traces, packet cache keys, teams, resumes, routines, and dashboards bind route counts and hashes. Current [Codex hook coverage](https://developers.openai.com/codex/hooks) remains incomplete, so this is exact Bash/patch/supported-MCP routing, not a universal native-tool allowlist. See `benchmark-suites/restricted-agent-profile-template.json` and `conductor-workflows/restricted-review.json`.

### Agent Profile Memory

`memory.mode: read-only|read-write` enables Conductor-managed memory for a profile without re-enabling Codex's native memory feature. Stores use strict `conductor.agent_memory.v1` JSON under `${CODEX_CONDUCTOR_HOME:-~/.codex/conductor}/workspaces/<workspace>-<hash>/agent-memory/<profile>.json`. They are workspace-scoped, revisioned, atomically replaced under a per-profile advisory lock, and bounded to 128 stored entries, 512 KiB total content, and 16 KiB per entry. Profile snapshots default to the newest 32 entries within 64 KiB and may narrow to at most 64 entries or 128 KiB; aggregate run snapshots stop at 256 entries and 512 KiB. Omitted `memory.selection` is exactly compatible `recent-v1`. `relevant` retains `lexical-relevance-v1`: it derives a query capped at 128 KiB from immutable workflow/profile text and ranks capped case-folded token presence by document rarity with a fixed tag boost and recency ties. Opt-in `hybrid` uses `hybrid-lexical-v1`, adding bounded content/tag frequency, adjacent query bigrams, exact structured identifiers, and integer length normalization. Both restore creation order for prompting.

```bash
python3 -m conductor_runtime remember-agent-memory correctness-reviewer \
  --workspace . --content-file verified-note.md --entry-id retry-boundaries \
  --tag retry --expected-revision 0
python3 -m conductor_runtime list-agent-memory --workspace .
python3 -m conductor_runtime inspect-agent-memory correctness-reviewer --workspace .
python3 -m conductor_runtime forget-agent-memory correctness-reviewer retry-boundaries \
  --workspace . --expected-revision 1
python3 -m conductor_runtime validate-agent-memory benchmark-suites/agent-memory-template.json
```

Operator writes redact detected secret-like values before persistence. `inspect-agent-memory` returns profile, revision, entry IDs, hashes, tags, provenance, and byte counts without content. At run creation the runtime copies selected entries into the unchanged v1 `agent_profile_memory_bindings` record in the external run workflow, labels them untrusted and potentially stale in each effective prompt, and includes the snapshot in workflow, resume, checkpoint, and packet-cache identity. Relevant and hybrid prompts, state, traces, and dashboards expose their versioned selector policy plus the bounded query SHA-256, never raw query text. A resumed run restores its original snapshot even if the store has advanced; a new run sees the new revision. Routine target authorization binds only static profile/context policy, so an approved routine may consume a fresh memory snapshot on each execution.

The `agent_memory` workflow step appends one bounded run artifact for future runs. It requires `memory.mode: read-write`, medium/high risk, `--allow-writes`, and `--approve agent-memory-write`; one update per profile per workflow is allowed. Entry IDs are deterministic by default, so retry after a crash between store commit and run-state commit is exactly-once. Model-authored workflows cannot opt themselves into external memory. `run-goal` can read a fixed snapshot but rejects update steps because memory must not be promoted before the external goal verifier succeeds. Memory remains untrusted reference data, not instructions, a capability grant, an embedding/synonym semantic index, or a team mailbox.

### Coordinated Agent Teams

An operator- or model-authored `agent_team` step defines 2 to 16 profile-backed members, exactly one lead, 1 to 128 initially ordered tasks, explicit dependencies, at most 16 rounds, a worker ceiling, per-turn weighted token cap, aggregate weighted token cap, an internal capture directory, and a final report. Read-only remains the default. The parent runtime is the coordinator: it commits deterministic claims before launching a round, runs ready members in parallel, validates one strict turn envelope per member, and delivers bounded messages only on later turns. A member keeps the same Codex thread across `continue` turns and later assigned tasks.

New runs use `conductor.agent_team_turn.v2` and may set `max_dynamic_tasks` from 0 through 32. Only the configured lead may propose tasks, only on a completed turn, and never while also requesting claims. Proposal ids are new and ordered; dependencies may reference only existing or earlier proposed tasks. Before committing them, the parent proves that the unchanged team has enough dynamic-task allowance, remaining rounds, effective workers, per-member turns, dependency depth, total turn slots, and minimum token funding. Proposals cannot add members, alter profiles, widen tools or permissions, raise budgets, or create nested teams. The lead should propose no work when the current result is already sufficient.

State is atomically checkpointed as `conductor.agent_team_state.v6` under the external run artifacts after claims and after each reconciled round. Proposed task descriptions and lead/round provenance are bound back to retained turn outputs during resume. Successful siblings remain complete when another member fails; `retry-step` preserves that state and resumes only unfinished dependency work. `reset-step`, downstream cascade reset, skip cascade, and `restart-run` advance a generation and create a fresh team with no prior sessions. Existing v5/v4/v3/v2/v1 state and v1 turn artifacts remain strict, explicitly exported, and resumable under their original authority. Total authorized token ceilings are conservative and include failed or interrupted launches. Validation, inspection, and all current/legacy schema exports are read-only; human inspection, APIs, and dashboards omit message bodies, operator instructions/replies, raw team artifacts/provider logs, and raw session IDs while showing hashes, routing, task origin, interruption counts, rounds, generation, and budget progress. The final report contains redacted task summaries and the message ledger.

An `agent_team` may opt into `active_messaging` with `max_tasks` from 1 through 32, `max_depth` from 1 through 4, a response `max_tokens` no greater than the ordinary turn cap, and explicit `allow_broadcast`. Each accepted message then creates a deterministic `message`-origin task for its named recipient; an allowed `all` expands in member order to every teammate except the sender. Response tasks run in the next available round, take priority after operator work, reuse the recipient session, preserve the team sandbox and tools, and use the narrower response cap. The parent rejects the complete candidate before state or source commit if routing, self-targeting, duplicate recipients, broadcast policy, hop depth, task/round/worker/member/turn capacity, or minimum aggregate-token funding is invalid. Active messages cannot combine with continuation, claims, or lead proposals. Hash-bound output verification reconstructs the exact message/task ledger on resume, task-created gates receive only hash metadata, and public surfaces expose counts/routing rather than bodies. Omission retains passive later-turn mailbox behavior and creates no response task, hook, workspace copy, or provider call. See `conductor-workflows/adversarial-peer-review.json`.

An `agent_team` may separately opt into `operator_chat` with `max_questions` from 1 through 32, a reply `max_tokens` no greater than the ordinary turn cap, and `reply_timeout_seconds` from 1 through 86,400. A completed teammate turn may target reserved recipient `operator`; before that turn commits, Conductor reserves one deterministic `waiting` response task for the same member and atomically checks it together with any peer-response tasks against every remaining task, round, worker, member, turn, and minimum-token limit. Other ready work continues. When nothing else is ready, only an enabled team with an unanswered question polls its private chat file every 250 ms. Timeout fails visibly but preserves the waiting task and session for an explicit answer plus `retry-step`.

Questions and replies use strict private `conductor.agent_team_operator_chat.v1` state. `list-team-questions` exposes routing, lifecycle, and hashes; `read-team-question` is the explicit private-text read; `reply-team-question` or the token-protected selected-run form commits a secret-screened answer. The reply passes the ordinary hash-only task-created gate, activates the reserved task, and wakes the asking member in its existing session with the narrower cap. It then uses the normal profile, explicit effort including `ultra`, sandbox, isolated-write merge, telemetry, and quality-gate paths. State-first question and reply-delivery crash windows reconcile without replaying a provider call, and reset supersedes unanswered old generations. Omission enters no chat helper, path, lock, poll, prompt, task, hook, copy, or provider-call path. Public views and generic excerpts never contain question or reply text.

An `agent_team` may independently opt into `operator_console` with exact `max_events` from 8 through 1,024 and `max_bytes` from 4 KiB through 2 MiB. This adds no provider call or token reservation. Before each ordinary teammate execution turn, the runner creates one private mode-`0600` `conductor.agent_team_transcript.v1` artifact bound to workflow fingerprint, step, generation, round, member, task, retry attempt, configured limits, and a deterministic opaque transcript id. Each accepted update is validated and atomically replaced, so a concurrent reader sees a complete active or terminal document. The provider-stream parser retains only a hash of `thread.started`, turn lifecycle markers, allowlisted tool kinds with started/completed status, and assistant-message text after secret redaction and character bounding. It discards prompts, reasoning, command strings and output, tool arguments/results, file paths, unknown payload fields, and raw session ids. Event or byte overflow drops whole trailing events and records truncation; malformed identities, sequence/hash drift, symlinks, unknown directory entries, or policy rebinding fail closed. A hard local loss may leave an honestly `active` transcript, but it does not authorize provider replay or claim a terminal outcome.

`list-team-transcripts` loads only the current team generation and returns identity, lifecycle, counts, session/output/file hashes, and truncation metadata without assistant text. `read-team-transcript` is the explicit private read; `--follow` polls the atomic file every 250 ms until terminal state or its bounded timeout. Existing `queue-team-task --interrupt-current` remains the sole write/redirect path and can target a teammate while another terminal follows the transcript. Completed, interrupted, timed-out, and handled-failure turns terminalize the artifact; interruption binds the accepted hash-only interruption output. Generic artifact excerpts, team logs, static details, public live/API details, hooks, reports, and hash-only listing never expose assistant text.

`team-console RUN_DIR STEP_ID` takes one generation-coherent private snapshot across all or repeated `--member` filters. It reuses the same strict no-follow scanner and reloads every selected transcript against workflow, step, generation, member, identity, message hash, and configured event/byte bounds before rendering. `auto`, `columns`, and `stack` layouts fit bounded ASCII panels to a 40-300 column terminal; history, event tails, and per-message display are separately capped. `--json` emits the same strict private `conductor.team_console_snapshot.v1` view. `--follow` polls from 100 through 5,000 ms, redraws only on validated state changes, has a one-second through 24-hour timeout, and exits when team and transcripts are terminal. Redirected output contains no ANSI. Provider-authored terminal controls, carriage returns, bidirectional formatting, and surrogate/control code points are rendered as inert ASCII escapes. The command is read-only and starts no writer, provider, network call, background process, or steering path; use `queue-team-task` separately for mutations.

The selected-run live page renders the same validated hash-only summaries and an `Open` form, never transcript content. A same-origin POST must present the per-server control token, then resolve the opaque run and step handles and revalidate the exact current-generation transcript before minting a 15-minute HMAC-signed `HttpOnly`, `SameSite=Strict` cookie whose path and signature bind that one private transcript URL. A fresh 256-bit per-server key signs grants independently of the control token, preventing a captured cookie from becoming an offline token verifier and invalidating grants on restart. A valid private GET refreshes that grant, escapes retained assistant text, and refreshes the page every two seconds only while the transcript remains active; the operator can pause refresh before editing. Eligible active pages fix the target member and expose next-turn or current-turn redirect through the ordinary token-protected `/control` handler. The read cookie never authorizes a write, cannot select another transcript, and is absent from public APIs. Omission returns before writer construction and adds no transcript directory, console-specific callback, transcript write, poll, prompt branch, token work, or provider call; the baseline terminal-recovery observer remains. `conductor-workflows/coordinated-team-review.json` demonstrates the console together with bounded named-member steering.

An `agent_team` may set `max_operator_tasks` from 0 through 32. Zero is the default and creates no inbox, lock, scan, interrupt poll, or provider call. While current v6 state, retained v5 state, or retained v4 state is running, `queue-team-task RUN_DIR STEP_ID --member MEMBER (--instruction TEXT|--instruction-file PATH)` or the selected-run live form appends one strict `conductor.agent_team_operator_inbox.v2` entry bound to the workflow fingerprint, team generation, named member, instruction hash, deterministic task id, and delivery mode. The default is `next-turn`. `--interrupt-current` or the live delivery selector additionally binds the exact active round/task. Enabled workers poll at 100 ms only during a provider call, but refuse to stop it until `thread.started` proves the resumable session. The selected child process alone is stopped; partial provider output is replaced by a hash-only `conductor.agent_team_interruption.v1` artifact, partial isolated writes are deleted without a merge event, and the member's session is retained. The redirect receives scheduling priority before the interrupted original task, and both resume that session in fresh authoritative workspace state. The queue must prove remaining graph, round, per-member, turn, and minimum-token capacity for both pieces of unfinished work plus the interrupted turn; the interrupted call keeps its full authorized cap because exact partial usage may be unavailable. If the call finishes before the poll observes the request, delivery safely degrades to next-turn. State-first interruption and task-acceptance windows reconcile under the same no-follow inbox lock, and final completion serializes against queueing. Secret-like, oversized, wrong-member, idle-member interrupt, duplicate interrupt, legacy-interrupt, terminal-team, over-capacity, over-budget, symlinked, or tampered requests fail closed. The loopback form still requires the per-server control token, same origin, opaque run/step handles, current-member routing, a 4,000-character instruction bound, and a 64 KiB request ceiling. Public responses and views retain only bounded routing, delivery, lifecycle, and hash metadata. This is explicit operator interruption and same-session redirect, not provider-native message injection into a still-running turn or exact recovery of discarded output.

For selected high-risk implementation tasks, a `workspace-write` team may add strict `plan_approval` policy with `task_ids`, bounded criteria, `max_revisions` from 0 through 3, and a plan cap no larger than the task cap. Each selected task must be static and assigned to one non-lead member. Omitted `reviewer`, or explicit `reviewer: lead`, requires `review_max_tokens` and retains autonomous fixed-lead review. Explicit `reviewer: operator` instead requires `reply_timeout_seconds`, forbids `review_max_tokens`, and launches no review-model call. Before any team round or source write, Conductor proves that the remaining aggregate budget can fund every worst-case configured plan/review cycle plus minimum implementation calls. The assignee always plans in a fresh external source copy under a read-only Codex sandbox. Lead mode reviews from a separate disposable read-only copy; operator mode commits `plan-ready`, then waits at a fixed 250 ms ceiling for `review-team-plan` or the token-protected selected-run control. Model, effort, profile, timeout, tool restrictions, and explicit `ultra` remain unchanged.

`conductor.agent_team_plan.v1` and `conductor.agent_team_plan_review.v1` bind exact task and revision. Private current `conductor.agent_team_plan_approval.v3` state binds workflow/generation/policy, reviewer mode, output paths and hashes, planner continuation, mutually exclusive lead-session/provider-usage or operator-decision evidence, decisions, timestamps, rejection count, and plan/review session-mode plus lifecycle prompt/receipt evidence; retained v2 and v1 remain compatibility contracts. `list-team-plans` is hash-only, while `read-team-plan` is the explicit private plan/feedback read. Operator feedback is secret-screened, returns to the same planner session after rejection, and cannot widen authority. Timeout leaves `plan-ready` unchanged so a later decision plus `retry-step` does not replay planning. Public inspection and static/live/API surfaces expose only bounded lifecycle metadata and hashes; team provider logs, plan text, feedback, and raw session IDs remain excerpt-opaque. Any source or copied-workspace mutation fails before implementation and the disposable copy is deleted. A hard loss during a provider plan/lead-review call leaves an uncertain state that cannot auto-replay; explicit `reset-step` advances the team generation before fresh calls. Omission returns before any approval path lookup, copy, prompt, operator lock, poll, or provider call. See `plan-approved-implementation-team.json` and `operator-approved-implementation-team.json`.

Optional team quality hooks cover `team_task_created`, `team_task_completed`, and `team_member_idle`. The parent first validates the teammate turn into an in-memory candidate, then invokes matching gates before candidate state, merge metadata, or source changes commit. `steps`, `members`, and `tasks` provide safe-id filters; task filters may name future dynamic tasks. Each command receives strict `conductor.agent_team_hook_input.v1` JSON on stdin with event/team/task identity, generation/round, status, description/output/summary hashes, workspace fingerprints, and at most 512 sorted changed paths. It contains no task or summary text, messages, session IDs, prompts, approval values, or provider output. Read-only gates run against a fresh external snapshot of source; write-team gates run against that member's isolated copy. A tracked-workspace fingerprint check before and after every gate rejects mutation regardless of `on_failure`. Normal command failure follows `block|warn`, or team-only `retry`: a configured nonzero exit code, default 2, supplies bounded redacted feedback to the same Codex session in the same execution workspace, including the retained isolated member stage for write teams. One to four attempts are allowed, each optional `retry_max_tokens` cap can only narrow the prior turn allocation, and every reservation consumes the existing team aggregate budget. Timeout, policy denial, mutation, unlisted exit, exhausted attempts, or insufficient budget fails without another model call. Omitted hooks add no process or model call; dry-run performs policy preflight once per applicable hook.

Before each automatic quality call, strict `conductor.agent_team_quality_retry.v1` state separately identifies the current task being retried and the event task whose gate failed, then records hashes of the session, feedback, hook input, workspace, and rejected output together with retry and token bounds. It never stores raw session IDs or feedback. Rejected outputs are retained outside source and hash-verified during recovery. A locally terminal provider turn can be journaled before process return and promoted to the strict completion journal before candidate evaluation; accepted, rejected, failed, and abandoned outcomes move to immutable history. Hard process loss never auto-replays a provider call: a terminal receipt promotes and reevaluates through deterministic local gates without another provider call, and a later accepted-turn merge intent commits write work; a retry lost before exact local terminal evidence is abandoned during explicit `retry-step` reconciliation. `validate-agent-team-quality-retry` and `inspect-agent-team-quality-retry` are local read-only evidence commands. Static/live dashboards expose terminal counts, pending recovery identity, and hook retry indices without feedback text.

With `sandbox: workspace-write`, every member profile must permit workspace writes and ordinary `--allow-writes` plus workflow/manual approval gates still apply. Before a parallel round, Conductor snapshots the tracked source and creates a fresh bounded symlink-free external copy for each active member. Codex receives that copy as both `--cd` and process cwd, while the original source stays unchanged until all launched turns finish. The parent then computes content/mode deltas and processes results in stable proposal/member order. `isolated-ordered-file-v1` applies disjoint edits, added/deleted/binary files, and executable-bit changes; identical same-path results are deduplicated. Different results for one path do not use last-writer-wins: the later task remains pending and the next round is forced to one worker, giving that task a fresh copy of the merged source for semantic resolution.

Every successful turn and merge conflict is appended to strict `conductor.agent_team_merge_ledger.v1`, including turn-output hash, base/result/source fingerprints, delta/plan hashes, changed/applied/deduplicated/conflicting path sets, and a continuous source chain. The ledger is cross-checked against accepted state turns; completed-step resume additionally re-snapshots the source and rejects outside drift. Provider usage accumulates across every successful team invocation, including conflict retries. VCS/dependency/cache directories from the staged-workspace exclusion list are not copied or merged.

Every teammate provider stream now composes one small baseline terminal observer, regardless of private-console or interruption policy. It keeps only bounded normalized metadata in memory, requires one stable UUID-bearing `thread.started`, and recognizes exactly one final `turn.completed`. If the canonical output already exists, its bounded deterministic redacted form parses, and an isolated write workspace has a stable snapshot, the observer atomically writes private current `conductor.agent_team_turn_terminal.v2` before the provider process returns. The receipt binds exact launch state and authorization, raw and redacted output checkpoints, resumable session, terminal telemetry, write-workspace base/result fingerprints, session mode, base/effective prompt hashes, and any lifecycle-context receipt. Retained v1 remains readable. If any exact artifact is not yet available, no receipt is written and ordinary post-return validation remains the fallback.

Terminal recovery runs before completed-turn recovery, quality-retry abandonment, reset, planning, or fresh provider work. It accepts only the prebound raw or redacted output hash and exact workspace result, normalizes raw output through the two declared checkpoints, reparses it, and promotes the receipt into the existing completion journal. A crash with both artifacts verifies their exact identity before removing the terminal marker. `validate-agent-team-turn-terminal` and `inspect-agent-team-turn-terminal` are local read-only commands; static/live views expose only identity, counts, totals, and hashes. Raw session, provider events, paths, and contents remain private. The observer adds no model call, prompt branch, token reservation, dependency, permission, network access, or background process.

Immediately after any ordinary or quality-retry teammate process succeeds and its output, resumable session, bounded telemetry, and optional isolated-write workspace validate, or after a terminal receipt is recovered, the worker atomically creates private current `conductor.agent_team_turn_completion.v2` before parent acceptance logic. The self-hashed write-new journal binds exact turn/retry identity, launch-state hash and counts, token authorization, retained output, raw session plus hash, strict provider events and measured totals, write-workspace base/result evidence, session mode, base/effective prompt hashes, and any lifecycle-context receipt. Retained v1 remains readable. One current file is allowed per member. Recovery runs before quality-retry abandonment, generation reset, planning, or fresh provider work; it validates retained bytes, lifecycle receipt, and state ancestry, restores completed siblings in stable order, reruns deterministic local gates, and persists a read candidate or promotes accepted write work into the merge-intent path. Telemetry import uses the completion hash as an atomic idempotency receipt, so a crash after accounting cannot duplicate events or usage. Stale journals are removed only after exact committed-turn or conflict evidence is proven. Tamper, rebinding, source drift, workspace drift, or lifecycle-evidence drift fails closed with zero provider calls.

`validate-agent-team-turn-completion` validates one or more private artifacts. `inspect-agent-team-turn-completion` returns only identity, counts, measured totals, and hashes; dashboards and live APIs use the same hash-only summary and label unaccepted journaled work `recovery-pending`. Raw session, event payloads, output/workspace paths, and contents remain private and generic excerpts are suppressed.

After a journaled write-team turn passes all applicable quality gates, the parent atomically writes private `conductor.agent_team_merge_intent.v1` before merge planning. It binds the retained turn output, isolated result workspace, launch-base manifest, expected source, exact prior state/ledger, and accepted candidate state. Resume validates every path/hash and recomputes the deterministic plan. Nonconflicting intent recovery promotes into the ordinary merge transaction with no provider replay. Conflicting intent recovery records the no-source-change event, tolerates only exact prior/reconciled state and ledger checkpoints, and then leaves only that task for its normal semantic rebase call. Reset resolves the old intent before advancing generation. `validate-agent-team-merge-intent` and `inspect-agent-team-merge-intent` are local read-only commands; public views expose only identity and hashes.

Before source mutation, the parent atomically writes `conductor.agent_team_merge_transaction.v1`, binding the exact plan, source-before manifest, retained member workspace, and before/candidate state and ledger hashes. Resume accepts only exact before, partial, or candidate source checkpoints and exact before/candidate metadata. It applies only remaining file operations, writes candidate state and ledger, emits `conductor.agent_team_merge_recovery.v1` with `provider_replayed: false`, removes the completion, intent, and transaction markers, and cleans the retained workspace. A reset performs this recovery before advancing generation. Terminal, completion, intent, transaction, workspace, output, source, state, or ledger drift fails closed. Validation/inspection commands, public schemas, static/live dashboards, metrics, and reports expose hashes and checkpoint classes without file contents.

This is not full Claude agent-team parity. Active messages automatically wake peers in a later parent round, teammates can ask bounded human questions and resume after the answer, users can privately follow redacted teammate progress and queue or interrupt named-member work, and selected plans may use either autonomous fixed-lead review or explicit human approval/rejection with same-session revision. Conductor still cannot inject text into a still-running provider turn or take over that provider's native interactive teammate session; observation is a separate strict local transcript. Exact in-flight provider-turn recovery is also absent. Automatic hook feedback is opt-in and bounded rather than implicit. The no-replay team guarantee starts at the durable validated local terminal receipt before provider-process return; a provider turn still in flight, or remotely completed but lost before exact terminal/output/workspace evidence is local, can still leave outcome and spend uncertain. Live matched team-quality evidence is still absent.

Run the explicit-ultra active peer-review workflow:

```bash
python3 -m conductor_runtime run conductor-workflows/adversarial-peer-review.json --allow-agent --allow-parallel
```

Run a dynamically expanded read-only workflow:

```bash
python3 -m conductor_runtime run conductor-workflows/dynamic-runtime-review.json --allow-agent --allow-parallel
```

Run a gated bulk-row workflow:

```bash
python3 -m conductor_runtime run conductor-workflows/bulk-row-review.json --approve bulk-row-workflow --allow-agent --allow-parallel
```

Generate a workflow draft from a task and failing command:

```bash
python3 -m conductor_runtime plan-workflow \
  --task "Fix the failing checkout regression" \
  --failing-command "python3 -B -m unittest discover -s tests" \
  --check-command "python3 -B -m unittest discover -s tests" \
  --output "$HOME/.codex/conductor/generated/checkout-regression.json"
```

Generated workflows are drafts. They are validated before writing, but they do not execute automatically. Review the JSON, then run it with the normal `run` command and any required capability flags.

Use the deterministic automatic front door for substantial work:

```bash
# No execution approval: deterministic direct compile plus preflight, with zero provider calls.
python3 -m conductor_runtime auto \
  --task "Audit the runtime and record evidence" \
  --workspace . \
  --allow-agent

# Approved one-shot execution.
python3 -m conductor_runtime auto \
  --task "Audit the runtime and record evidence" \
  --workspace . \
  --allow-agent \
  --approve model-workflow-execute

# An explicit verifier selects bounded direct repair, one worker call per iteration.
python3 -m conductor_runtime auto \
  --task "Fix the regression and prove it" \
  --check-command-json '["python3","-B","-m","unittest","discover","-s","tests"]' \
  --workspace . \
  --allow-agent \
  --approve model-workflow-execute

# A multi-worker envelope tries direct once, then plans only after verifier failure.
python3 -m conductor_runtime auto \
  --task "Fix broadly, escalate only if tests fail" \
  --check-command-json '["python3","-B","-m","unittest","discover","-s","tests"]' \
  --max-workers 4 \
  --workspace . \
  --allow-agent \
  --allow-parallel \
  --approve model-workflow-execute

# Explicit decomposition spends one planning call, then runs the generated graph.
python3 -m conductor_runtime auto \
  --strategy workflow \
  --max-workers 4 \
  --task "Decompose a repository-wide audit" \
  --workspace . \
  --allow-agent \
  --allow-parallel \
  --approve model-workflow-execute

# Explicit bounded native delegation: no planner, read-only parent plus depth-1 children.
python3 -m conductor_runtime auto \
  --strategy native \
  --native-agent-threads 2 \
  --task "Review independent correctness and security concerns" \
  --workspace . \
  --allow-agent \
  --allow-parallel \
  --approve native-agent-delegation \
  --approve model-workflow-execute
```

`auto` does not ask another model how to route. Its `deterministic-v5` decision uses only explicit operator inputs. The default serial envelope selects `direct-workflow`, compiling one bounded `codex_exec` step without a planning call; a command verifier, model verifier, or explicit command-then-model chain selects `adaptive-direct-goal`. In a chain, command failure short-circuits semantic review and records zero model-verifier calls. Omitted-cap direct workers scale with resolved `low` through `ultra`: read-only work receives 4,000/8,000/12,000/16,000/20,000 weighted tokens and writes receive 8,000/16,000/24,000/32,000/40,000. Explicit caps are unchanged. A diagnosis/review synthesis clause consolidates observations by independently actionable repair boundary before return, including candidate findings that share one first faulty executable line or one validation site. Semantic review uses the same isolated 8,000-token/high-effort/ephemeral envelope in every route. A verifier plus explicit multi-worker envelope selects `adaptive-progressive-goal`: iteration one uses one direct worker, and only failed verification permits a model-authored graph using the full envelope. Later model iterations resume the same planner session. `--strategy progressive` pins this policy, `direct` forbids escalation, `goal` plans immediately, `workflow` requests one-shot decomposition, and explicit `native` compiles one read-only direct parent with one funded depth-1 child by default, zero planning calls, and one shared aggregate token ceiling. Task text is untrusted and cannot select the route, verifier topology, execution mode, writes, network, parallelism, or approvals. A one-shot route safely becomes `plan-only` when `model-workflow-execute` is absent, and direct plan-only performs zero provider calls. Adaptive and `--background` routes reject missing approval before provider launch. `--allow-writes` makes the requested graph write-capable, while execution separately requires `--approve model-workflow-write`.

Native mode defaults to one concurrently open child slot; `--native-agent-threads 1..6` changes that child-only ceiling, while the parent remains separate. The runtime configures both supported Codex concurrency contracts so the enforced total is parent plus the requested children, fixes depth at one, disables recursive fan-out, and permits only the standard read-only shell path. Irrelevant plugin, app, browser, computer, MCP, web, hook, goal, memory, and dependency surfaces are mechanically disabled while repository guidance and the selected model remain intact. Product-native funding covers a 24,000-token integration parent plus 18,000 per child, or `24,000 + 18,000 * child_slots`: 42,000 through 132,000 weighted tokens. A caller-controlled direct benchmark may explicitly lower this no further than the legacy minimum `12,000 + 6,000 * child_slots`; this keeps the matched read-only campaign at 18,000 for one child without underfunding ordinary defaults. `parent-integration-and-synthesis-v4` is the sole native checkpoint contract, replacing rather than repeating the generic completion clause and adding one final overlap/root-cause and output-schema pass. Same-component methods or call sites that bypass one required helper become one component-level root cause, while independently repaired components remain separate. The current auto receipt records both limits and the dedicated approval; deterministic-v4 receipts retain their original formula. `--plan-only` still performs zero provider calls. Native mode rejects writes and adaptive verifiers, requires `--allow-agent --allow-parallel --approve native-agent-delegation`, and requires `model-workflow-execute` only when execution is requested. Child usage is included in aggregate reconciliation. The provider's shared cutoff can overshoot while concurrent turns are in flight, so complete post-run reconciliation is authoritative: a measured overage fails the step and retains any answer only as failed-turn partial evidence. Unavailable reconciliation remains advisory. Top-level provider telemetry remains incomplete, and provider-emitted lifecycle counts remain informational and may be incomplete.

For `auto` only, omitted `--planner-effort` selects a conservative verified fast path. Under `verified-task-text-v6`, direct/progressive work with an explicit verifier may resolve to `low` only when the task is at most 400 characters and explicitly identifies either one smallest-change public-function repair or one deterministic test addition to a named file while production code remains unchanged. Migration, refactor, idempotency, every broad/sensitive marker, and all other task shapes remain low-effort blockers. Other direct/progressive work with an explicit verifier, at most 1,000 characters, and no broad, sensitive, explicit multi-file/multi-module, broad test-authoring, concurrency, distributed, schema, transaction, or repository-wide marker resolves to `medium`; every other task resolves to `high`. Frozen v1/v2/v3/v4/v5 identifiers preserve their prior decisions. Explicit `low|medium|high|xhigh|ultra` overrides this policy. The receipt stores the resolved provider effort, never the `auto` label. Direct workers treat the runtime call as the complete workflow and do not inspect skills, plugins, goals, or orchestration state. External verification replaces internal test, runtime, diff, and bespoke-check loops. Auto-selected low/medium writes use one combined pre-edit read; high verified work uses at most two reads in a compact workspace and may use more in larger repositories. Low/medium/high verified workers prepare the full edit set and prefer one patch call when two to eight files can be changed together, while failed or incomplete combined writes remain correctable; `xhigh` and `ultra` are unrestricted. A terminal failed write run may still satisfy the goal only if its real workspace passes the deterministic command verifier; `run_status: failed` remains in the iteration. Missing-run, blocked, interrupted, read-only, and model-verifier failures remain fail-closed. Goal artifacts and dashboards record each iteration's planning mode, planner calls, component verifier statuses, semantic-verifier calls, and verifier policy. Legacy `deterministic-v1` and `deterministic-v2` receipts remain validatable but cannot claim composite verification.

Every accepted launch first writes a pending receipt and atomically finalizes strict `conductor.auto_orchestration_receipt.v2` evidence with the redacted task hash/length, route and reason, execution mode, policy booleans, limits, status, and hash-only artifact records. Raw task text and approval values are absent. Native receipts additionally bind the dedicated approval, child-thread ceiling, and exact derived aggregate weighted-token ceiling; strict v1 remains readable but cannot carry native authority. Foreground and plan-only artifact hashes are terminal. A background auto receipt is an immutable handoff record and marks the existing child run/goal receipt `handoff-mutable`. `list-auto` scans a bounded no-follow receipt directory; `status-auto` inspects one receipt. Both resolve a child only when its filename and resolved path hash match under the expected generated/goals roots. Use `--child-artifact` for a custom output path. Missing, malformed, symlinked, stale, or path-drifted child evidence becomes `unresolved`/`invalid` attention rather than success. Static/live dashboards expose the same handoff/effective-status split. Validate routing bytes with `validate-auto-receipt`. Existing child run/goal artifacts remain authoritative completion evidence, so evidence bundles do not duplicate auto routing receipts.

Matched topology experiments use a separate strict campaign rather than changing normal `auto` routing:

```bash
python3 -m conductor_runtime write-auto-topology-campaign \
  benchmark-suites/auto-topology-tasks.json \
  "$HOME/.codex/conductor/benchmarks/auto-topology-campaign.json" \
  --seed 20260709 \
  --repetitions 3
python3 -m conductor_runtime run-auto-topology-arm \
  "$HOME/.codex/conductor/benchmarks/auto-topology-campaign.json" \
  benchmark-suites/auto-topology-tasks.json \
  --next \
  --dry-run
python3 -m conductor_runtime status-auto-topology-campaign \
  "$HOME/.codex/conductor/benchmarks/auto-topology-campaign.json"
```

`conductor.auto_topology_campaign.v3` hashes every task, binds the exact Conductor version/runtime build, and separately binds a deterministic score-free execution contract containing the prompt, public success criteria, and required evidence. It preregisters direct, progressive, and plan-first arms in independently copied workspaces with seeded order balanced independently of task text. Dry-run reports release/build compatibility with zero launches. A real arm revalidates the task and execution hashes before policy, launch receipt, workspace copy, or provider discovery, then requires agent/write capability, `auto-topology-campaign-arm`, `model-workflow-execute`, and `model-workflow-write`; progressive and plan-first additionally require parallel capability, while direct forbids it. All arms forbid network and destructive access. Current v3 results repeat the exact release identity. The runner writes a launch receipt before the existing `auto` pre-provider receipt, refuses to replay an unknown interrupted call, evaluates hidden tests only after execution in a temporary copy, assigns no-change failures zero, and captures planner receipts under the arm root. Frozen exact-release v2 and release-unbound v1 campaign/result contracts remain strict status evidence but cannot launch new work; v1 also cannot support inference. Status rehashes campaign, workspace, auto/goal/planner/run/evaluation bytes before reporting release uniformity, matched quality, calls, tokens, latency, cost, escalation, and direct-first repayment. The first exact `0.130.0` direct retry-policy arm scored 10/10 in one worker call at 82.849 seconds and 69,567 gross tokens. Its matched plan-first arm produced three valid two-step v8 plans but scored 2/10 in 220.481 seconds after three planner and three worker calls: model-authored 18/18/22 KiB stream limits dropped terminal telemetry, and the v2 runner had forwarded only the prompt rather than the public no-test-change criterion. Release 0.131 fixed those harness defects with v9's 256 KiB floor/24,000-token write ceiling and v3's full contract binding. Its first fresh progressive backoff test-authoring arm scored 10/10 in one worker call at 121.377 seconds and 73,174 measured tokens. The matched plan-first architect made two allowed inspections and exhausted 20,283 gross tokens against its 20,000 ceiling before emitting JSON, so it scored 0/10 with no worker. Release 0.132 makes enclosing-verifier architecture zero-inspection without raising any cap. Immutable scores remain unchanged, missing resource telemetry remains incomparable, and 43 pending arms preclude a topology or Claude comparison.

The exact `0.132.0` v10 campaign then proved zero-inspection architect recovery on its first plan-first arm: two valid two-step plans launched two workers. Both workers reached 24,000 weighted tokens; the second passed visible verification but missed a publicly undocumented held-out `completed_attempts` contract, producing an immutable 5/10 score in 141.023 seconds across four calls. It also exposed that planner-authored workers ran at `high` despite the campaign's `ultra` declaration. Release 0.133 pins external-verifier steps/profiles to the selected model/effort, advances to v11 acceptance-preserving implementation prompts, and makes the scored fixture contract public. Forty-four arms remain pending, resource telemetry is incomplete, and no recommendation is supported.

Use the separate read-only campaign to measure topology and provider quality on objective diagnosis tasks without changing ordinary routing:

```bash
python3 -m conductor_runtime write-readonly-parity-campaign \
  benchmark-suites/read-only-diagnostic-tasks.json \
  "$HOME/.codex/conductor/benchmarks/readonly-parity/campaign.json" \
  --seed 20260711 \
  --repetitions 2 \
  --artifacts-dir arms
python3 -m conductor_runtime run-readonly-parity-arm \
  "$HOME/.codex/conductor/benchmarks/readonly-parity/campaign.json" \
  benchmark-suites/read-only-diagnostic-tasks.json \
  --next \
  --dry-run
python3 -m conductor_runtime status-readonly-parity-campaign \
  "$HOME/.codex/conductor/benchmarks/readonly-parity/campaign.json"

# Product-profile evidence uses its separate 42k task fixture and explicit profile.
python3 -m conductor_runtime plan-readonly-parity-campaign \
  benchmark-suites/read-only-diagnostic-product-tasks.json \
  --seed 20260711 \
  --repetitions 2 \
  --budget-profile product-v1
```

`conductor.readonly_parity_campaign.v2` preregisters three diagnostic tasks over two repetitions, for 18 balanced arms, and records an explicit budget profile. `matched-v1` preserves the historical equal 18,000-weighted-token serial/native threshold. `product-v1` uses the dedicated product task fixture and the ordinary 20,000-token ultra serial ceiling plus the funded 42,000-token one-child native ceiling. Frozen `conductor.readonly_parity_campaign.v1` files remain accepted under their exact matched contract. Each validated campaign binds its per-system cap through generated workflow configuration, observed overshoot accounting, and completed-result audit; a result or workflow from another profile is invalid. Claude remains forced through the strict read-only adapter with `--model sonnet --effort ultracode`, no fallback, no Opus main response, and each task's provider-dollar threshold. Current arms use non-planning `dontAsk` and explicitly preapprove only `Read`, `Glob`, and `Grep`. Historical plan-mode evidence remains hash-valid, but status marks that semantic mismatch as a blocker and excludes Claude pairs from supported quality advantages. Controls fail closed after observed overshoot but cannot prevent an already in-flight provider turn from crossing a threshold. Every campaign binds the exact runtime build hash, and the hidden evaluator persists hashes, matched key identifiers, criteria, and totals rather than answer text.

Current hidden evaluation schema `conductor.readonly_diagnostic_evaluation.v2` distinguishes the event-routing subscription canonicalizer bypass from the independent documented list-container validation boundary. Candidate findings are assigned to hidden root causes with a bounded deterministic maximum-weight one-to-one match, avoiding order-sensitive greedy misclassification when evidence legitimately mentions overlapping concepts. V1 evaluation files retain their original schema, identity, answer-key hash, score, and campaign result; status validates them without rerunning a newer key. A v2 forensic replay is diagnostic context only and never rewrites immutable campaign evidence.

A live arm requires `--allow-agent --approve readonly-parity-campaign-arm --approve high-risk`. Codex additionally requires `model-workflow-execute`; native Codex requires `--allow-parallel --approve native-agent-delegation`; Claude requires `--allow-network --approve claude-paid-run`. The executor narrows the effective policy per arm, so Codex keeps network and writes disabled even when a `--next` operator supplied the capabilities needed for a later Claude arm. It rejects a same-version runtime whose exact build hash differs, writes `preparing`, `ready`, and `launching` checkpoints before provider execution, reuses only validated terminal provider artifacts, and refuses to replay an unknown interrupted launch. When a failed Codex turn emitted a complete agent message but no final-message file, the runner may recover only that bounded answer from a strict terminal JSONL stream; provider success stays false. Failed providers and invalid answers still finalize with deterministic scores instead of disappearing from coverage.

Status independently validates the campaign, exact runtime build, source/task prompt hashes, provider report or model-workflow receipt, strict Codex stream where present, answer/evaluation chain, unchanged workspace, run state, and content-free Codex progress. It reports final success, hidden quality, native-child compliance, raw latency, provider-native gross tokens, comparable parent-plus-child Codex weighted tokens, authoritative Claude cost, cap-observation coverage, compliant arms, and overshoot arms as separate dimensions. Pairwise output additionally reports final-success-first duration and gross tokens to success: one successful arm beats a failed arm, two successful arms compare lower resource use, and two failed or unobserved arms remain incomparable. A quality advantage requires complete repeated coverage and a paired Wilson interval; a native routing recommendation additionally requires every native arm to have actually used a child, complete aggregate Codex accounting, and no observed cap breach. Provider-dollar and weighted-token thresholds are never called equivalent, Codex authoritative dollar cost remains unavailable, and `universal_superiority_supported` is always false because this cohort does not cover implementation, recovery, safety, or usability.

Generate a model-authored declarative workflow draft:

```bash
python3 -m conductor_runtime plan-model-workflow \
  --task "Design a bounded repository-wide security review with synthesis and verification" \
  --workspace . \
  --allow-agent \
  --max-workers 4
```

The model architect runs through the strict isolated read-only Codex profile and writes to the external workspace `generated/model-workflows/` directory by default. User config/rules/project instructions, network/web, plugins/apps, nested agents, code-mode hosts, unified/deferred execution, and adjacent optional surfaces are disabled while the minimal shell route remains available for workspace inspection. Its complete prompt renders exact top-level, per-step, and agent-profile fields from the same sets used by the validator, requires `agent_profiles` to be an object array, forbids external Skill/source/docs/reference/example lookup, and permits at most two combined target-workspace inspection calls before strict JSON. In an adaptive goal, the existing trusted verifier is explicit: the architect omits standalone inspection/planning/verification/synthesis/review workers, prefers one implementation worker or one coordinated team, and uses at most 4,000 weighted tokens after the last inspection for remaining reasoning plus JSON. Ordinary explicit planning retains task-appropriate review. Conductor then revalidates the normal workflow schema plus a narrower model-draft policy. Unknown fields, hooks, mutating/network/external shell commands, unsafe parallel writes, over-limit graphs, and low-risk declarations for high-risk task scopes are rejected or escalated before writing. The architect may select `agent_team` when persistent role-specific coordination materially helps; generated teams stay caller worker/item bounded, exactly-one-lead, and dynamically capped. Planner reasoning defaults explicitly to `high`; `--planner-effort low|medium|high|xhigh|ultra` deterministically selects a runtime-hard cap of 4,000, 8,000, 12,000, 16,000, or 20,000 weighted tokens. The exact cap and 50%-remaining completion checkpoint are included in the planner prompt.

Explicit review planning also creates a bounded no-follow tracked-content snapshot before the provider starts and verifies it again after the read-only turn. Bound draft/receipt destinations must remain outside the workspace. Current `conductor.model_workflow_receipt.v4` records hashes/counts plus model/effort/cap/isolation provenance, never raw paths, task text, file contents, or approval values. Current drafts use `conductor.model_workflow_prompt.v12`. V12 retains zero-inspection compact graphs, complete public acceptance preservation, selected model/effort pinning, the 256 KiB provider floor, and the 24,000-token write ceiling. It also appends a trusted complete-workflow handoff after authored worker prompts: no external skills/plugins/goals/orchestration, no tests/runtimes/toolchain/diff/bespoke checks under the enclosing verifier, a half-budget first-write checkpoint, complete public-contract checklist, and no post-write tool churn. Ordinary explicit planning retains authored choices, repository guidance, its two-call inspection allowance, and narrower caps. Frozen v11 and earlier accepted contracts remain valid. Serial direct uses v4, native direct remains v3, and receipt schemas v1-v4 expose the exact historical prompt sets accepted by the runtime.

`--write-capable` permits gated `codex_exec` workspace-write steps and isolated `agent_team` workspace-write steps. Every such step must have an ancestor `manual_gate` with `approval_id: model-workflow-write`; `agent_map` remains read-only. Write-team member profiles must carry a workspace-write ceiling and the ordinary team validator enforces the isolated merge contract. This flag authorizes drafting that topology, not running it. Inspect the JSON and receipt, then hand that exact pair to the reviewed execution command:

```bash
python3 -m conductor_runtime run-reviewed-model-workflow \
  <draft.json> \
  <draft.json.receipt.json> \
  --workspace . \
  --allow-agent \
  --approve model-workflow-execute
```

`run-reviewed-model-workflow` performs no planning call and accepts no task, model, effort, worker, item, step, or planning-limit override. For a bound v4 receipt it validates the exact receipt snapshot, canonical workflow fingerprint/name/filename/mode/step count, resolved workspace path, and fresh bounded workspace fingerprint before policy checks create a run receipt. Limits and write-capable status come only from the reviewed receipt. `--allow-agent` is required only when the graph contains an agent step; write/parallel/high-scale/manual gates, full dry-run preflight, foreground execution, optional `--background` worker, and the v3 run receipt remain unchanged. V3 and unbound-v4 preflight requires `--allow-unbound-workspace`; non-dry execution also requires `--approve unbound-reviewed-workspace`. This compatibility authority does not disable graph, receipt, or ordinary policy validation.

Plan, preflight, and execute in one explicit foreground command:

```bash
python3 -m conductor_runtime run-model-workflow \
  --task "Audit the runtime and record verified evidence" \
  --workspace . \
  --allow-agent \
  --approve model-workflow-execute
```

Use the same handoff with detached execution after preflight:

```bash
python3 -m conductor_runtime run-model-workflow \
  --task "Audit the runtime and record detached evidence" \
  --workspace . \
  --background \
  --allow-agent \
  --approve model-workflow-execute
```

`run-model-workflow` is automatic only after explicit invocation and approval. It validates requested capabilities before planning, writes the normal model draft and planning receipt, dry-runs the complete generated graph through the normal runner, and starts workflow processes only if every policy, risk, agent, shell, and manual-gate preflight succeeds. Foreground execution remains the default. `--background` records the successful preflight as a prepared launch and starts one session-isolated worker using the same anonymous policy/approval pipe as `start-background-run`; `--dry-run` is mutually exclusive and stops after preflight without requiring execution approval. Write-capable mode additionally requires `--allow-writes --approve model-workflow-write`; parallel caps above one require `--allow-parallel`, and caps above 16 retain `high-scale-agent-map`.

Current `conductor.model_workflow_run_receipt.v3` receipts represent both process models. A detached receipt is written as `running` before launch, its SHA-256 and absolute path travel only in the anonymous pipe, and the worker verifies that hash before atomically finalizing status. Receipt changes after launch fail closed: workflow state remains visible, but background exit is nonzero and reports `CompletionReceiptFinalizationError`. Planning or launch failure records a strict failed receipt; raw task text, approval values, and the completion-receipt path are not stored in background metadata. Strict `v1` and foreground-only `v2` receipts remain validatable and exported.

Run a bounded adaptive model-planned goal:

```bash
python3 -m conductor_runtime run-model-goal \
  --task "Fix the regression and prove every acceptance criterion" \
  --check-command-json '["python3","-B","-m","unittest","discover","-s","tests"]' \
  --workspace . \
  --allow-agent \
  --approve model-workflow-execute \
  --max-iterations 3
```

`run-model-goal` creates a fresh validated graph on each iteration rather than rerunning a fixed graph. Failed command output or an unsatisfied fail-closed model verdict becomes bounded redacted untrusted context for the next planning call. Supplying both verifier types creates an ordered chain: failed commands skip model review, while a passed command permits one semantic verdict; only model infrastructure or malformed-verdict errors stop fail-closed. By default, the first planner call captures the canonical UUID from Codex's `thread.started` event and later calls use `codex exec resume`; resumed calls reassert the read-only sandbox configuration and fail closed if telemetry omits the UUID or reports a different one. The continuation prompt relies on the retained planner transcript instead of replaying the full task, reducing repeated context while preserving the outer workflow-authoring contract. Use `--fresh-planner-session-per-iteration` to disable this behavior. `--desktop-notify` is valid in foreground or detached mode and sends only after the terminal goal state is durable.

Add `--dynamic-interval` with a model-only `--check-prompt` to let that same verifier choose the next retry delay from 1 through 60 minutes; `--min-delay-minutes` and `--max-delay-minutes` may narrow the range. The strict verdict adds exactly `next_delay_minutes` and `next_delay_reason`: a satisfied verdict requires `null` plus an empty reason, while an unsatisfied verdict requires an in-range integer plus a non-empty bounded reason. The selection consumes no extra provider call. Before waiting, the goal atomically records the selected delay, a reason hash/count, and one absolute UTC wake time. Foreground or detached interruption leaves that record intact; matching `--resume-goal` waits only the remaining time and never asks the model to select again. Delay-policy drift, timestamp/reason/verdict drift, malformed output, command/composite verifiers, and out-of-range choices stop fail-closed before another iteration.

Optional `--monitor-command-json` turns the selected wake into a deadline and listens for one earlier event. The value is one JSON argv array, never a shell string. Preflight rejects commands classified as write-capable or destructive, requires existing network or external-path authority when applicable, screens secret-like argv, and binds only the argv hash/count into the external goal state. A detached launch transfers the raw argv only through its anonymous payload. The child process has its own process group, a 64 KiB output cap, and a 2,000-character first-line event cap; the runtime kills it on event or deadline. The redacted line becomes explicitly untrusted feedback for the next planner iteration, while list/dashboard views expose only lifecycle, counts, and hashes. Nonzero or eventless early exit fails closed. If execution is interrupted after launch but before a terminal receipt, resume marks the prior monitor outcome uncertain, does not replay the command, and sleeps only the remaining persisted deadline. Status and dashboards distinguish `monitoring` from ordinary waiting. This is local event wake, not a cloud/API/GitHub trigger, reboot-persistent task, daemon, or general streaming UI.

```bash
python3 -m conductor_runtime run-model-goal \
  --task "Watch the deployment and investigate only when current evidence changes" \
  --check-prompt "The deployment is complete and healthy with concrete current evidence." \
  --dynamic-interval --min-delay-minutes 1 --max-delay-minutes 60 \
  --monitor-command-json '["tail","-n","0","-f","deployment-status.log"]' \
  --background --workspace . \
  --allow-agent --approve model-workflow-execute
```

Each iteration has its own draft, planning receipt, preflight/execution receipt, and run directory under external state. The session UUID is stored only in the external goal artifact so foreground, detached, and later resume processes can continue it; list/dashboard/API summaries expose only continuity mode, availability, SHA-256, and turn count. `--resume-goal` continues only when the task, planning configuration, verifier, and planner-session mode fingerprints match and a larger iteration budget remains. Goals created before same-session continuity can be resumed with `--fresh-planner-session-per-iteration`. Interrupted iteration metadata is retained and continuation starts with a fresh graph against current workspace state. Foreground execution is the default.

Add `--background` to run the bounded adaptive loop in one explicit session-isolated worker. The parent completes request, execution-approval, full verifier-chain, limit, and resume-fingerprint validation before launch; the worker revalidates both composite components from the anonymous payload and reports signal-handler readiness before the command returns. Raw task text, model verifier criteria, workspace paths, and approval values cross only through an anonymous stdin pipe. Strict `conductor.background_model_goal.v1` metadata under `<goals-dir>/background-workers/` stores verifier kind/fingerprint, hashes, counts, policy booleans, worker/session identity, heartbeat/readiness, log names, and final status, but not payload or approval values. The worker installs no daemon and never restarts.

```bash
python3 -m conductor_runtime run-model-goal \
  --task "Fix the regression and prove it" \
  --check-command-json '["python3","-B","-m","unittest","discover","-s","tests"]' \
  --workspace . \
  --background \
  --allow-agent \
  --approve model-workflow-execute \
  --max-iterations 3
python3 -m conductor_runtime status-background-model-goal <background-metadata.json>
python3 -m conductor_runtime wait-background-model-goal <background-metadata.json> --timeout-seconds 3600
python3 -m conductor_runtime terminate-background-model-goal <background-metadata.json>
python3 -m conductor_runtime validate-background-model-goal <background-metadata.json>
```

`terminate-background-model-goal` accepts only fresh, same-host, session-isolated active metadata and sends `SIGTERM` so the worker can kill and reap active planner/runner process groups before finalizing the goal. `--force` still starts with that cleanup signal and escalates after a bounded grace period. A hard external kill cannot guarantee final metadata; a stale or dead worker must be inspected and resumed explicitly.

Generate an Ultracode-style deterministic orchestration draft:

```bash
python3 -m conductor_runtime plan-workflow \
  --profile ultracode \
  --task "Fix the checkout regression and verify the runtime" \
  --write-capable \
  --check-command "python3 -B -m unittest discover -s tests" \
  --output "$HOME/.codex/conductor/generated/ultracode-checkout.json"
```

The `ultracode` profile still only writes a workflow draft. It adds charter/risk artifacts, repository context mapping, synthesis, optional gated implementation, verification, and final review; when agents are enabled and no packet source is supplied, it also adds automatic bounded `agent_map` fan-out from the collected file list. Running the generated workflow still requires normal flags such as `--allow-agent`, `--allow-parallel`, `--allow-writes`, and approval tokens when the workflow needs them.

Generate a reusable saved workflow command from a draft:

```bash
python3 -m conductor_runtime plan-workflow \
  --task "Review runtime files for policy drift" \
  --packet-glob "conductor_runtime/*.py" \
  --saved-command runtime-policy-review
```

`--saved-command NAME` writes `.claude/workflows/<NAME>.js` by default. Use `--saved-output PATH` to choose a `.js` or `.mjs` destination. The generated file still contains only JSON-compatible `meta` and `workflow` exports and is parsed by the same saved-workflow loader; it is not arbitrary JavaScript.

Generate an instruction-only live parity runbook. Its Conductor instructions include the isolated Codex adapter, an approval-required `plan-model-workflow` candidate with a receipt path, and a deterministic `plan-workflow --profile ultracode` baseline; generating the runbook starts neither provider:

```bash
python3 -m conductor_runtime plan-live-parity-runbook benchmark-suites/parity-tasks.json
```

The runbook records candidate operator instructions for comparable Conductor and Claude/Ultracode runs. It has `evidence_status: planned-not-evidence`; it does not execute Claude, Codex, workflows, network calls, or paid tools.

Preregister repeated matched trials with a deterministic seed before any provider launch:

```bash
python3 -m conductor_runtime write-parity-campaign \
  benchmark-suites/implementation-canary-completion-reserve-tasks.json \
  "$HOME/.codex/conductor/benchmarks/implementation-campaign/campaign.json" \
  --run-id implementation-campaign \
  --seed 20260709 \
  --repetitions 3 \
  --artifacts-dir "$HOME/.codex/conductor/benchmarks/implementation-campaign/trials" \
  --codex-model gpt-5.6-sol
```

The strict v4 campaign binds each task contract, budget, repetition, provider order, artifact destination, automatic-effort policy identifier, and concrete per-row Codex effort. Its current `verified-task-text-v6` profile uses the narrow low-effort fast path above, resolves other bounded verified work without an independent hard marker to `medium`, and leaves broader work at `high`. V4 also reads frozen v5, v4, v3, and v2 plans, and trial preflight recomputes against the stored identifier rather than the newest default. An explicit concrete effort fixes every row. The versioned completion-reserve fixture keeps every task contract field unchanged except a uniform 24,000-token direct-write ceiling, matching the ordinary product profile; the ceiling does not require the worker to consume it. V3 original-policy, v2 fixed-effort, and v1 ultra-only campaign schemas remain readable. Trial order is SHA-256 ranked from the seed; provider-first assignment uses an independent ranking and alternation so counts differ by at most one. The Claude lane must remain `sonnet` with `ultracode`, and alternative models are disallowed. `plan-parity-campaign`, `write-parity-campaign`, and `validate-parity-campaign` never launch providers and always label the artifact `planned-not-evidence`.

Use `status-parity-campaign CAMPAIGN [--json]` to account for execution without editing the immutable plan. A row is completed only after both raw reports, deterministic evaluations, retained patches and current stage fingerprints, score inputs, scored reports, and live manifests form valid hash-bound chains and a fresh strict pair comparison succeeds. Missing chains remain partial; malformed or drifted evidence is invalid and makes human status exit nonzero. Status v5 preserves quality while separately reporting execution, final success, verified delivery, latency, safety, row dominance, raw cost, provider-native gross tokens, native-cap cohorts, and the validated Conductor release cohort. Verified delivery is true only when the independently scored quality evidence passes, the retained stage is successful, its configured verifier passed without timeout, and the hash-bound completion receipt agrees. It does not change the provider status, raw execution, final success, incidents, or strict claim eligibility. Historical reports that predate `environment.conductor_runtime_version` remain readable; their system version is `null`, their pair provenance class is `unbound-conductor-runtime-version`, `unbound_release_pairs` accounts for them separately from mixed pairs, and every inference lane remains blocked. Raw resource sections include per-system observed totals, paired coverage, and lower-is-better outcomes; token pairs require the same named accounting contract. Safety compares highest non-informational severity and then non-informational incident count; info-level provenance remains in manifests but cannot penalize transparency. A pair can enter descriptive cap stratification only when all provenance and numeric budgets match except the expected hard-dollar versus hard-token enforcement methods and both reports name the same Conductor runtime version; strict claim eligibility is never rewritten. Completed rows from different or unbound releases remain visible longitudinal evidence but block every inference lane. Strict broad/universal gates and `final-success-first-v1` resources continue to require strict pair provenance and raw outcomes. Separate `product_profile_inference` and `product_profile_universal` results use the preregistered native product profiles without calling their cap mechanisms equivalent: broad product-profile quality requires complete valid coverage, at least ten pairs, two repetitions per task, a paired 95% interval strictly above zero, every pair stratification-eligible, at least two rows in every observed cap cohort, and one release cohort. Product-profile universal support uses `verified-delivery-first-v1` time, cost, and tokens: one verified delivery wins only when paired telemetry exists, two deliveries compare lower resource use, and two non-deliveries or missing telemetry remain incomparable. Raw outcomes are retained unchanged.

Use `run-parity-campaign-trial CAMPAIGN PARITY_TASKS (--trial-id ID | --next) --dry-run` to preflight one implementation-canary row with zero provider launches and no receipt write. Automatic-policy campaign preflight recomputes the stored effort from the hash-bound task and schema-bound policy and rejects any mismatch. The plan also reports the current runtime release, completed campaign releases, and whether another launch would preserve the release cohort. A non-matching runtime may inspect status and dry-run, but real continuation fails before approval checks, tool discovery, receipt creation, or provider calls; use the archived matching runtime or start a fresh campaign. Remove `--dry-run` only with agent/network/write capabilities and all five aggregate/provider/staged approvals. Execution follows the preregistered provider order and uses the row's exact concrete Codex model/effort plus internally pinned Claude Sonnet Ultracode. Atomic per-system launch states prevent duplicate spend: a captured report can be finalized with `--resume`, but `launching` without a report is treated as an unknown remote outcome and never retried automatically. Deterministic finalization accepts verified retained patches and the narrow `no-changes` case; the latter creates a bound empty patch marker and 0/10 score while preserving failed execution. Other patchless or unsafe outcomes remain incomplete.

Run one Codex read-only task through an external stage:

```bash
python3 -m conductor_runtime run-codex-readonly-task \
  benchmark-suites/parity-tasks.json repo-security-audit \
  --workspace . \
  --output "$HOME/.codex/conductor/benchmarks/codex-security-audit.json" \
  --model gpt-5.6-sol \
  --effort ultra \
  --max-tokens 200000 \
  --timeout-seconds 900 \
  --allow-agent --allow-network \
  --approve codex-live-run
```

The adapter requires explicit model, reasoning effort, fixture-bounded token cap, agent/network gates, and `codex-live-run`. It copies a bounded symlink-free source snapshot outside the repository and runs an ephemeral `codex exec` with approval policy `never`, strict configuration, ignored user config/rules/project instructions, a custom minimal-read permission profile, and a sanitized model-shell environment whose user home/config/startup roots point at the null device. Network, web search, apps, hooks, browser/computer tools, image generation, memories, goals, plugins, dependency installation, multi-agent features, deferred execution, and alternate code-mode/unified execution hosts are disabled. The discovered Codex executable is canonicalized before version probing and launch so a nested macOS sandbox helper does not re-execute a denied symlink. Top-level `allow_login_shell=false` rejects login-shell requests and keeps normal shell calls out of machine/user startup files; the similarly named nested shell-environment key is invalid and is never emitted. The developer and task wrappers define the complete workflow and direct the worker to skip external skills, plugins, goals, and orchestration state. The Codex CLI 0.144 envelope therefore does not require a separately linked `codex-code-mode-host`, access to a global skill path, a symlink-safe fallback, or GUI-backed developer-tool initialization. Prompt text crosses stdin. Source and stage fingerprints must remain unchanged. Service routing remains at the CLI default unless the operator explicitly selects `--service-tier priority`; that override may reduce latency but consumes increased account usage, so ordinary comparison runs remain lightweight by default.

Strict `conductor.codex_provider_evidence.v5` records CLI version, command-bound model, selected service tier and binding method, execution mode, terminal state/usage, runtime-budget exhaustion, retained output, source/stage integrity, parse/truncation/timeout state, permission profile, and policy invariants. It adds exact/lower-bound/unavailable usage precision, a source, and gross plus weighted lower/upper bounds. Completion usage is exact. A future failure event carrying `usage` is also exact. Codex CLI 0.144.1 omits usage from failed JSONL turns, although its runtime budget has already crossed the command-enforced limit; v5 therefore records both gross and weighted usage as at least the requested cap and leaves both upper bounds null. This is a proven one-sided bound, not an estimate. The adapter keeps `codex exec --ephemeral --ignore-user-config --ignore-rules`; it does not switch to app-server, inspect session state, or persist credentials to recover telemetry. Legacy v1-v4 evidence remains validatable under its original semantics.

Run one Codex implementation task through a disposable external stage:

```bash
python3 -m conductor_runtime run-codex-staged-task \
  benchmark-suites/parity-tasks.json runtime-bugfix \
  --workspace . \
  --output "$HOME/.codex/conductor/benchmarks/codex-runtime-bugfix.json" \
  --check-command-json '["python3","-B","-m","unittest","discover","-s","tests"]' \
  --model gpt-5.6-sol --effort ultra --max-tokens 200000 --timeout-seconds 1800 \
  --allow-agent --allow-network --allow-writes \
  --approve codex-live-run --approve codex-staged-write
```

The staged permission profile grants write access only to the external stage. The prompt states the configured rollout budget as a hard ceiling and treats 50%-remaining as a completion checkpoint. A larger or screened-out stage also uses that reminder as its write-progress checkpoint. If a complete small-workspace packet is present, both providers treat it as the initial repository read and skip plan/progress narration, listing, search, and discovery rereads before the first edit; direct Codex instead uses the existing 90%-remaining reminder as its no-write checkpoint. An edit-tool-required target read remains permitted. Complete-context Codex turns determine the full required edit set before writing and prefer one multi-file patch call when two to eight files can be changed together. The preference grants no new capability, does not force larger edits into one call, and permits normal correction after any failed or incomplete combined write. If the applicable checkpoint arrives with no successful required write, Codex stops deliberation or discovery and issues the best complete contract-mapped write; later budget is reserved for unwritten affected files, a specific known gap, or failed-write correction. This selection changes no effort, cap, permission, retry, or verifier. A direct turn never runs tests, language runtimes, toolchain probes, or diff commands inside the provider shell. Both staged providers statically audit intended code against the complete contract while preparing each required write. For ordinary implementation tasks, one shared checklist follows the complete task and workspace context and separately covers every changed or directly affected public function, wrapper, caller, and parameter, including accepted/rejected domains, boundaries, errors, non-mutation, and compatibility conversions. At object and container boundaries, the outer type is checked before attribute or key access so wrong types preserve documented exception classes instead of leaking incidental `AttributeError` or `KeyError`. The worker may not validate only the primary transformed value; the real `auto` direct-write prompt uses the same checklist without another call or effort increase. Once every required write succeeds, staged providers make no further tool call for rereads, listing, diffs, checks, or explanation, emit one short completion sentence, and end the turn. Failed writes and remaining required multi-file writes may still be corrected before that boundary. The external deterministic verifier and runtime-derived completion summary are authoritative. For a validated `test-authoring` task tag, both wrappers replace the generic exhaustive static audit with the same compact baseline-first policy: write a minimal discriminating suite with representative finite boundaries, cover one case per documented equivalence class, and use table-driven cases or subtests where clear. Imports are limited to the target module and test framework unless the task contract explicitly names another required facility; wrong-type cases use built-in values rather than auxiliary imports such as `Decimal` or `Fraction`. This trusted task-type policy follows untrusted task/workspace context immediately before final-write guidance in both prompts. Providers validate or statically trace the documented baseline before optional breadth and stop after distinct fault classes. Task prose cannot activate this policy. The verifier is policy-checked before launch and runs with a sanitized environment, denied network, and stage-only writes under macOS Seatbelt or Linux bubblewrap. Verifier evidence is redacted before a 4,096-character prefix/suffix bound so long failures retain their terminal summary without expanding artifacts. Source fingerprints are checked after Codex and after verification. Strict `conductor.codex_staged_evidence.v1` rejects invalid stage content, binary/non-UTF-8 or executable-mode changes, source drift, verifier mutation/failure/timeout, and patch races. A clean provider budget cutoff remains a failed provider/final outcome, but the verifier can still retain a valid authored patch when the stage is safe. Every staged report also carries a validated completion summary derived from the final change set and verifier record, so file/check reporting remains available even when the provider stops before final prose. Nothing mutates or automatically applies to the source by default.

Staged commands use `--exit-contract strict` by default. Explicit `--exit-contract verified-stage` changes only their process exit code: it accepts an already validated non-empty patch when source remained unchanged, the verifier did not mutate tracked files, the isolated verifier passed with return code zero and no timeout, and the completion summary agrees. The report remains failed after a provider cutoff, all provider incidents and token/cost evidence remain intact, and no patch is applied. Unsafe, malformed, patchless, unverified, or failed-verifier outcomes still exit nonzero. The option adds no call, retry, permission, cap, or prompt text.

The acceptance decision reopens the retained patch with no-follow semantics, enforces the evidence-bound exact byte count, and verifies its SHA-256. Missing, symlinked, replaced, enlarged, or changed patch bytes fail the operational exit contract.

All three staged commands expose explicit `--apply-verified-stage`. It requires the existing staged/provider gates plus `--approve verified-stage-apply`; deletion still independently requires `--allow-destructive --approve verified-stage-delete`. The apply policy and external receipt path are preflighted before provider launch. After the provider and authoritative verifier finish, the command writes and fsyncs strict evidence before calling the same hash-bound `apply-verified-stage` transaction. That phase adds zero provider/verifier calls and preserves every provider, repair, benchmark, incident, cost, and token outcome. If `--exit-contract` is omitted, this explicit path uses verified-stage delivery exit semantics; explicitly selecting `strict` still reports the strict provider/repair outcome. `--apply-receipt` selects an external receipt path. A pre-existing receipt is rejected before provider launch; use standalone `apply-verified-stage` to resume a prepared/partial transaction or recheck a merged receipt without replay.

Before either staged provider launches, a complete stage with no more than 24 files may be rendered once as canonical `conductor.small_workspace_context.v2` JSON. Both raw workspace bytes and serialized packet bytes must fit a 16 KiB ceiling further narrowed to half the applicable validated token ceiling. The runtime uses bounded no-follow reads, checks the full hash-bearing parent snapshot before and after packet construction, and disables the entire packet for `.env`/credential/key filenames, non-UTF-8 or NUL-bearing content, framing collisions, or detected secret-like values. Snapshot drift is a pre-provider validation failure. The provider projection contains only its schema plus sorted path/content/executable records; workspace and per-file hashes, counts, and raw-size metadata remain in parent validation and are not repeated to the model. Safe packet bodies are explicitly untrusted repository data, not instructions, and serve as the initial repository read. The adapters state the no-narration/list/search/discovery rule once while permitting a target-file read required by an edit tool. The existing provider-prompt SHA-256 binds the exact packet; reports retain only that hash, never file contents. Larger or screened-out workspaces use the existing tool-driven path with no extra process or model call.

The lean staged wrapper removes duplicate complete-context/no-discovery prose, the test-authoring pointer-only static-audit block, and model-irrelevant packet hashes while shortening checkpoint and finalization wording. The complete rendered task contract and every enforceable safety, coverage, verifier, write-progress, and zero-tool condition remain. On the exact frozen `backoff-test-authoring` task and source, the Codex provider prompt is 6,172 bytes instead of 8,219 (2,047 bytes and 24.9% smaller), while Claude is 5,008 instead of 6,295 (1,287 bytes and 20.4% smaller). These are exact deterministic prompt-byte measurements; they do not establish token, cache, latency, cost, quality, or cross-provider superiority.

Use bounded verifier-driven correction when a task fixture has enough aggregate token and time budget:

```bash
python3 -m conductor_runtime run-codex-staged-repair \
  benchmark-suites/implementation-canary-tasks.json slug-normalization \
  --workspace benchmark-suites/implementation-canary/source \
  --repair-dir "$HOME/.codex/conductor/benchmarks/slug-repair" \
  --check-command-json '["python3","-B","-m","unittest","discover","-s","tests","-v"]' \
  --model gpt-5.6-sol --effort ultra \
  --max-attempts 2 --max-tokens-per-attempt 10000 \
  --attempts-this-run 1 \
  --provider-timeout-seconds 120 --check-timeout-seconds 30 \
  --allow-agent --allow-network --allow-writes \
  --approve codex-live-run --approve codex-staged-write
```

`run-codex-staged-repair` rejects a launch unless worst-case tokens and provider-plus-verifier time fit the selected fixture. Attempt one copies the source; each retry copies the preceding external stage into a new stage and starts a fresh provider session. Only `verification-failed` and `no-changes` retry. Verifier output is redacted, bounded, delimiter-sanitized, and enclosed as untrusted diagnostics. Provider failure or cutoff, source drift, invalid stage content, unsupported changes, verifier mutation, and verifier timeout stop immediately.

Repair defaults to strict process exit. Explicit `--exit-contract verified-stage` changes only process exit when a completed repair already has a safe verifier-passing result. The runtime reloads strict repair evidence, its hash-linked final attempt report, the exact attempt patch, and the cumulative `final.patch`; it requires unchanged source, no verifier mutation, supported non-empty changes, a performed passing verifier with an agreeing completion summary, no automatic apply, and provider status limited to success or the recognized Codex token cutoff. A cutoff remains failed in repair and aggregate benchmark evidence. Repeating identical completed-run arguments with `--resume --exit-contract verified-stage` launches no provider or verifier process. The explicit inline apply option can consume that same terminal evidence; paused checkpoints and uncertain active attempts are never applied or accepted by the delivery exit contract.

The repair directory starts with atomic `repair-state.json`. `--attempts-this-run N` cleanly records `paused` after at most `N` new attempts when another retry remains; repeat the launch with `--resume` to continue. The per-invocation count may change, but the task, source, model, limits, and verifier must not. Strict `conductor.codex_staged_repair_state.v1` stores task/configuration/source/path hashes, bounded counters, safe artifact names, stage fingerprints, and fixed non-apply policy only. It omits approval values, raw workspace paths, verifier argv, prompts, feedback, and provider output. Resume revalidates the task contract, resolved workspace hash, all model/limit settings, verifier command hash, original source snapshot, every completed report, and every completed stage before another provider call. Active duration accumulates across invocations without counting downtime.

The checkpoint is written as `attempt-active` before provider launch and returns to `ready` only after the report and stage are durably linked. An `attempt-active` state cannot be resumed: the runtime cannot prove whether the interrupted call consumed budget, so replay would be unsafe. Completed attempts are never rerun. Terminal recovery accepts only an exact preexisting cumulative patch, can regenerate a missing aggregate report from a valid repair receipt, rejects an aggregate report without its receipt, and treats a completed resume idempotently.

Completed repair directories contain sequential stages, incremental reports/patches, strict `repair.json`, one cumulative `final.patch` against the original source when safe, and aggregate `benchmark-report.json`. `conductor.codex_staged_repair.v1` records aggregate usage, a continuous stage fingerprint chain, attempt-report hashes, cumulative changes, and non-apply policy. `validate-codex-staged-repair-state` checks the checkpoint and linked completed-attempt/final artifacts; `validate-codex-staged-repair` revalidates every linked report plus final patch bytes and SHA-256; `validate-benchmark-report` validates the self-contained aggregate report.

Score `benchmark-report.json` normally. Repair-backed scoring emits `conductor.benchmark_quality_evidence.v2`, which hashes the aggregate receipt and embedded attempt sequence in addition to the task, output, rubric, and reviewer. A live manifest must use the total repair token cap, runtime-hard token enforcement, exact Codex model/effort/version, and staged-write permissions. Successful and exhausted deterministic trials can remain live evidence; provider, drift, invalid-stage, or unsupported-change outcomes cannot. Comparison claim eligibility still requires equivalent manifests and independent scoring across systems.

Run one paid read-only Claude task through the strict adapter:

```bash
python3 -m conductor_runtime run-claude-readonly-task \
  benchmark-suites/parity-tasks.json repo-security-audit \
  --workspace . \
  --output "$HOME/.codex/conductor/benchmarks/claude-security-audit.json" \
  --max-budget-usd 0.75 \
  --max-turns 8 \
  --timeout-seconds 900 \
  --allow-agent \
  --allow-network \
  --approve claude-paid-run
```

Write-capable parity tasks use a disposable external stage and a separate deterministic verifier:

```bash
python3 -m conductor_runtime run-claude-staged-task \
  benchmark-suites/parity-tasks.json runtime-bugfix \
  --workspace . \
  --output "$HOME/.codex/conductor/benchmarks/claude-runtime-bugfix.json" \
  --check-command-json '["python3","-B","-m","unittest","discover","-s","tests"]' \
  --max-budget-usd 1.00 --max-turns 12 --timeout-seconds 1800 \
  --allow-agent --allow-network --allow-writes \
  --approve claude-paid-run --approve claude-staged-write
```

The command refuses to start unless the selected fixture authorizes paid cost and the operator supplies all three capability gates. It probes Claude Code 2.1.203 or later before the paid call, fixes `--model sonnet --effort ultracode`, supplies no fallback, disables session persistence and prompt suggestions, and gives Claude only `Read`, `Glob`, and `Grep`. It uses non-planning `dontAsk`, passes the same exact list through `--allowedTools`, and therefore denies every other action without turning the requested audit or diagnosis into Claude's plan workflow. Prompt text is piped through stdin, not stored in argv. `--max-budget-usd` cannot prevent Claude Code from finishing an in-flight request above the cap; overshoot is therefore a failed `budget-cap-overshoot` incident rather than success. No retry is automatic.

Claude output uses verbose `stream-json`. The terminal result is authoritative; when it is empty after a budget/turn cutoff, only the last non-empty completed assistant message becomes the bounded redacted answer. Earlier assistant turns remain counted but are not concatenated into the scored output. The benchmark report embeds strict `conductor.claude_provider_evidence.v3` with requested permission contract and model, observed models, CLI version, terminal state, cost, overshoot, turn count, capture status, non-persisted approval policy, and measured `modelUsage` tokens. All main and helper records contribute uncached input, cache creation, cache read, output, and gross totals under `provider-native-gross-v1`; incomplete or inconsistent counters fail closed and absent counters remain null. Strict plan-mode v2 and v1 evidence remains readable under its original schema and cannot be rewritten as v3. Any Opus usage, non-Sonnet main response, malformed or duplicate JSON, missing terminal evidence, timeout, or truncation fails closed. Internal helper models such as Haiku are recorded separately and never treated as the main model. Provider success proves this invocation contract only; task quality still needs scorecard evidence.

The staged command requires both paid and staged-write approvals. It copies at most 20,000 regular files and 256 MiB into a symlink-free external stage, excludes VCS/dependency/cache directories, applies the same optional secret-screened small-workspace context contract described above, and invokes Claude with `acceptEdits` plus exactly `Read`, `Glob`, `Grep`, `Edit`, and `Write`. Claude receives no Bash tool and never edits the source directly. The verifier argv passes normal shell policy before the provider launch; on macOS it runs under Seatbelt, and on Linux it requires bubblewrap. The verifier environment is sanitized, network is denied, and filesystem writes are limited to the stage. Unsupported hosts fail before the paid invocation.

After Claude and again after verification, source fingerprints must match. Tracked verifier mutations, source drift, symlinks, special files, binary/non-UTF-8 changes, executable-mode changes, patch-output races, or malformed evidence fail closed. A failed or timed-out verifier can retain a valid authored Git patch for review, but no patch is automatically applied. The same explicit inline apply transaction is available after passing verification without changing the Sonnet/Ultracode pin. Validate standalone evidence with `validate-claude-staged-evidence`.

Claude exposes the same strict-by-default staged exit contract. Selecting `verified-stage` never changes the pinned Sonnet/Ultracode provider evidence or turns a Claude budget overshoot into provider success.

Attach rubric quality evidence before turning a read-only provider report into a live manifest:

```bash
python3 -m conductor_runtime validate-benchmark-score-input <score-input.json>
python3 -m conductor_runtime score-benchmark-report \
  <provider-report.json> benchmark-suites/parity-tasks.json \
  <score-input.json> <scored-report.json>
```

The score input and generated `conductor.benchmark_quality_evidence.v1` bind the task contract, retained output, provider/staged evidence, criterion scores, threshold, and reviewer provenance by SHA-256. Final task pass requires both `execution_passed` and quality pass. Re-scoring and bound-content tampering fail closed. Comparison requires the same contract and scale plus independent reviewers before quality provenance is claim-equivalent.

For a live manifest backed by provider evidence, `write-run-manifest` requires exact model/version/effort, requested cap, cap-enforcement method, external repository identity where applicable, permissions matching the selected execution mode, quality evidence where required, and carried incidents. Claude evidence requires observed Sonnet and `hard-provider` dollar enforcement. Current Codex v4 and legacy v2/v3 evidence require the command-bound model and `runtime-hard` token enforcement; legacy v1 retains `post-run-fail-closed`. Write-capable success requires successful staged evidence, but a source-preserving, non-applied, verifier-unmutated `verification-failed` or `verification-timed-out` outcome with a retained validated patch may also be bound as a live failed result. The campaign runner's exact validated `no-changes` contract may likewise bind a deterministic zero-quality outcome. Drift, verifier mutation, unsupported status, or any other patchless failure remains ineligible. A clean budget cutoff may retain a separately verified patch when safe. Rejected, malformed, provider-timed-out, truncated, drifted, or otherwise invalid provider evidence cannot be marked live. Comparison rechecks every report-manifest binding and requires complete independent quality evidence for claim eligibility. Use the provider, staged, and quality validators for standalone fixtures.

Generate a packetized workflow draft from repository files:

```bash
python3 -m conductor_runtime plan-workflow \
  --task "Review runtime files for policy drift" \
  --packet-glob "conductor_runtime/*.py" \
  --check-command "python3 -B -m unittest discover -s tests" \
  --output "$HOME/.codex/conductor/generated/runtime-packet-review.json"
```

Packetized drafts add a read-only `agent_map` step. Generated sources are capped at 1,000 items and use the `balanced-v2` ceiling of 25 model packets, with at most 64 source items encoded into one JSON-array packet. Generated workflows can request up to 128 workers. Running them requires the normal agent capability flags; more than 16 actual workers also requires `--approve high-scale-agent-map`. The runtime bases that gate on the smaller of configured workers and resolved packet count.

List and run a saved workflow command:

```bash
python3 -m conductor_runtime validate-saved-workflows
python3 -m conductor_runtime list-saved-workflows
python3 -m conductor_runtime inspect-saved-workflow conductor-verification-sweep
python3 -m conductor_runtime export-saved-workflow conductor-verification-sweep "$HOME/.codex/conductor/generated/verification-sweep.json"
python3 -m conductor_runtime prepare-run conductor-workflows/verification-sweep.json --allow-writes
python3 -m conductor_runtime start-background-run conductor-workflows/background-smoke.json --desktop-notify --allow-writes --approve background-smoke-execute
python3 -m conductor_runtime status-background-run <run-dir>
python3 -m conductor_runtime wait-background-run <run-dir> --timeout-seconds 60
python3 -m conductor_runtime run-saved-workflow conductor-verification-sweep --dry-run --allow-writes
python3 -m conductor_runtime run-saved-workflow audit-routes --allow-agent --allow-parallel --print-result
python3 -m conductor_runtime validate-desktop-notification <run-dir>/desktop-notification-completed.json
python3 -m conductor_runtime list-saved-workflows --project-root . --include-personal
```

Saved workflow commands are `.js` or `.mjs` files under `.claude/workflows/`. By default Conductor scans the current workspace's `.claude/workflows/` directory. With `--project-root`, it also scans ancestor project workflow directories from the workspace up to that root. With `--include-personal`, it also scans `~/.claude/workflows/`, or an explicit `--personal-workflows-dir`. Personal saved workflow discovery is classified as `external_path` by the command policy, even for otherwise read-only saved-workflow subcommands. Resolution is deterministic: the nearest project command wins over parent project commands, project commands win over personal commands, and duplicate command names at the same precedence rank are rejected.

Conductor first supports its native safe data format:

```js
export const meta = {
  "name": "conductor-verification-sweep",
  "description": "Run local verification."
};

export const workflow = {
  "schema": "conductor.workflow.v1",
  "name": "verification-sweep",
  "description": "Run local validation commands.",
  "mode": "read_only",
  "steps": [
    {
      "id": "runtime-help",
      "kind": "shell",
      "risk": "low",
      "writes": false,
      "command": ["python3", "-B", "-m", "conductor_runtime", "--help"]
    }
  ]
};
```

Both native exported objects must be JSON-compatible object literals: quoted keys, valid JSON values, and no computed JavaScript expressions.

Release 0.166 introduced the common Claude saved-workflow shape through a static compiler:

```js
export const meta = {
  name: 'audit-routes',
  description: 'Audit every route handler for missing auth checks',
}

const found = await agent('List every .ts file under src/routes/.', {
  schema: { type: 'object', required: ['files'], properties: { files: { type: 'array', items: { type: 'string' } } } },
})

const audits = await pipeline(found.files, file =>
  agent(`Audit ${file} for missing authentication checks.`, { label: file }),
)

return audits.filter(Boolean)
```

This compatibility path accepts static literals, top-level awaited `agent()` and `pipeline()` bindings, literal or `args.NAME` item arrays, prior-agent array-of-strings schema properties, callback template interpolation, static `schema`/`label` options, and a direct return or `.filter(Boolean)` pipeline return. It preserves statement order and duplicate list elements, emits one `agent_map` packet per array element, caps concurrency at 16 and the script's total possible agents at 1,000, and compiles every provider step read-only. A consumed agent schema is normalized for strict output, materialized under the external run, passed to `codex exec --output-schema`, and read back through a strict bounded JSON pointer. The terminal return compiles to a deterministic `collect_results` step and `claude-workflow/result.json`, with a 10 MiB aggregate ceiling, JavaScript-compatible falsey filtering, byte-level source/output receipts, and no additional model call. The CLI reports the result artifact path by default to avoid context expansion; `--print-result` explicitly renders the completed value. Imports, loops, branches, mutation, arbitrary calls, computed interpolation, unsupported options, and every other script construct fail closed.

Neither format evaluates JavaScript or requires Node. The loader rejects symlinked or workspace-escaping paths, uses component-wise no-follow reads and exports, rejects same-precedence duplicate names, validates the compiled workflow with the normal schema, and runs it through the same `WorkflowRunner` and `RuntimePolicy` gates as JSON workflows. `agent()`, `pipeline()`, `parallel()`, and `phase()` are compile-time syntax forms, not JavaScript calls performed by the loader. The static compiler accepts JSON-compatible constants, declared phase metadata, phase markers/options, and the common `parallel(SOURCE.map(item => () => agent(...)))` thunk form. A static source may contain scalar or object items; callback prompts may read static item properties and static schema constants. Those prompts are pre-rendered into one true-parallel `agent_map` with bounded inline `item_semantics: "opaque"`, so prompt text is hashed rather than resolved as a workspace path. Dynamic `args.NAME` parallel sources remain bounded and opaque after argument rendering; prior-agent sources retain the required array-of-workspace-paths schema handoff. Capability flags such as `--allow-agent`, `--allow-parallel`, `--allow-writes`, `--allow-network`, and `--approve` remain mandatory when the resulting workflow needs them. Arbitrary JavaScript, helper functions, branches/loops, nested fan-out, promise/result transforms other than terminal `.filter(Boolean)`, richer multi-callback `pipeline()` forms, write-capable Claude script compilation, and automatic background launch are not claimed by this subset.

Saved workflow placeholders can receive bounded operator input:

```js
export const meta = {
  "name": "audit-target",
  "description": "Audit one target path."
};

export const workflow = {
  "schema": "conductor.workflow.v1",
  "name": "audit-target",
  "mode": "review",
  "steps": [
    {
      "id": "capture",
      "kind": "write_artifact",
      "risk": "low",
      "output": "inputs/target.txt",
      "content": "Target: {{args.target}}"
    }
  ]
};
```

Run it with `--arg target=src/routes`. Values are parsed as strict JSON when possible, otherwise as strings; non-standard constants such as `NaN` are rejected. Argument keys must be safe identifiers; duplicate, oversized, missing, unused, or secret-like args are rejected. Inline placeholders render in a single pass so arg text is treated as literal text, not another template. Exact placeholders may render JSON values such as arrays for fields like `agent_map.items`. The rendered workflow is validated again before inspect, export, or run. This is a safer placeholder mechanism, not Claude's arbitrary JavaScript `args` global.

Generate a local run and benchmark dashboard:

```bash
python3 -m conductor_runtime list-runs
python3 -m conductor_runtime list-runs --status running --json
python3 -m conductor_runtime run-console <run-dir>
python3 -m conductor_runtime run-console <run-dir> --follow --refresh-ms 500
python3 -m conductor_runtime list-goals
python3 -m conductor_runtime diagnose-run <run-dir>
python3 -m conductor_runtime dashboard
```

`list-runs` prints a tab-separated snapshot of recent runs with status, workflow, progress counts, runner liveness, prepared launch status when present, and modification time. With `--json`, it emits a compact redacted envelope containing run handles, status, progress counts, runner liveness, prepared launch metadata, usage summary, and evidence-complete state while omitting internal paths and raw excerpts. The command reuses the dashboard's bounded no-follow readers; repeated `--status` filters return the newest matching runs up to `--max-runs`. It is a read-only snapshot, not a live watch mode, daemon, or process supervisor.

`run-console RUN_DIR` builds one strict `conductor.run_console_snapshot.v5` from bounded run readers and prints a responsive 60-240 column view. `--follow` accepts a 100-5,000 millisecond refresh and one-second through 24-hour timeout, redraws only when the canonical snapshot changes, and exits when the run leaves planning, running, or a pending control state. Redirected output contains no ANSI; interactive clearing is optional. `--json` emits one snapshot. The snapshot deliberately omits step detail and retains only run identity/status, runner liveness and active-process count, diagnostic severity/count, step identity/status/phase/kind/timing, fixed Codex progress counters, the runner-authorized native envelope, provider-emitted native-agent/collaboration counts, bounded aggregate reconciliation status/token/session integers, and hash-only failed-output validity/source/size evidence with explicit unavailable and truncation markers. It starts no provider, model, network call, writer, daemon, or background process.

`diagnose-run RUN_DIR` prints a redacted, read-only diagnostic report for one run, with `--json` for automation. It flags malformed state/workflow JSON, failed or blocked runs, prepared runs waiting for resume, stale active runners, failed or blocked steps, and completed runs whose standard evidence is incomplete. When a prepared launch manifest has a validated resume command argv, diagnostics report that command as guidance. Suggested commands are recovery guidance only; the command does not mutate run state or supervise processes.

`doctor` checks Python, the discovered `codex` launcher, and the sibling `codex-code-mode-host` used by code-mode-only models. It returns nonzero when the launcher is absent or invalid, the packaged companion source is unavailable, or the launcher's sibling is missing, broken, non-executable, or resolves to a different release. `doctor --json` emits the same bounded result. `doctor --repair-codex-host --approve codex-host-repair` creates only a missing sibling symlink whose parent resolves inside the current user home; it is idempotent when healthy and refuses to replace any existing entry.

`prepare-run WORKFLOW` creates a visible run handoff without executing the workflow. It runs the same dry-run preflight as `run --dry-run`, writes planned step state, then marks the run `needs_resume` and writes `launch.json` with a redacted `run --resume` argv, policy flags, preflight status, and external-supervisor contract. Approval token values are not persisted; if a later launch needs `--approve`, the operator must supply it again. This command does not launch or supervise a worker.

`start-background-run WORKFLOW` is the explicit detached counterpart. It refuses launch unless the full prepared-run dry-run preflight succeeds, then creates one new session-isolated worker for that run. The worker revalidates its PID/session identity, prepared policy, workflow, worker cap, and approval count before resuming. Approval values are transferred once through an anonymous stdin pipe and are not placed in argv, environment, `launch.json`, `background.json`, or worker logs by the handoff layer. `--desktop-notify` adds only that fixed boolean flag to worker argv. Strict `conductor.background_run.v1` metadata records lifecycle, process identity, policy booleans, approval count, log names, and final status. `status-background-run` combines background, runner, and workflow state; `wait-background-run` blocks for a bounded interval and returns nonzero unless the run completed successfully. Notification backend failure is advisory and cannot change the worker's workflow exit. The worker has no restart policy, installs no daemon, and is controlled through the existing validated `terminate-run` path.

`run`, `run-saved-workflow`, `start-background-run`, `run-goal`, `run-model-workflow`, `run-reviewed-model-workflow`, `run-model-goal`, and `auto` accept `--desktop-notify` only for real execution. `write-routine-manifest --desktop-notify` persists only that fixed flag so manual, supervised, and service-owned routine launches use the same contract. Omission adds no receipt directory, backend lookup, or process launch; dry-run and plan-only requests are rejected before planner or run creation. macOS uses fixed `/usr/bin/osascript`; Linux uses only fixed root-owned, non-group/world-writable `/usr/bin/notify-send`. Both receive fixed status text through argv without a shell, with null stdio and a five-second timeout. Title and body never contain task text, output, paths, errors, model text, secrets, workflow/routine names, or execution identity. A mode-`0600` `desktop-notification-<run-status>.json` claim is created before launch. Run-owned receipts stay in the unique run directory; goal and pre-run-wrapper receipts use a mode-`0700` sidecar beside the external terminal artifact. Final claims suppress replay; an interrupted `sending` claim becomes `uncertain` and is never automatically retried. Status-specific files allow one blocked notification and a later distinct completion notification after approved resume. `delivered` means the backend command returned zero, not that user attention was observed. `failed` and `unavailable` remain fail-open and cannot change workflow, goal, wrapper, or routine exit status. Validate receipts with `validate-desktop-notification` or schema type `desktop-notification` (`conductor.desktop_notification.v1`). Cloud tasks remain outside this local contract because remote completion is asynchronous and may happen off-host.

`run-goal WORKFLOW` is foreground-only fixed-graph adaptive automation. Command verifiers are policy-checked before any workflow iteration. Model verifiers require `--allow-agent`, use a read-only Codex sandbox, and must return exactly `satisfied`, `feedback`, `evidence`, and `confidence`; malformed, timed-out, or failed provider results stop the loop as `verifier_failed`. Goal artifacts store verifier fingerprints and redacted bounded verdict evidence rather than raw criteria. Unsatisfied verdict feedback and failed command excerpts are passed to the next iteration's Codex prompts as explicitly untrusted evidence, and the feedback hash/length is visible in runner metadata. `--resume-goal` validates the same workflow and verifier fingerprints and continues from the next iteration. `--desktop-notify` writes its replay-safe receipt beside that external goal artifact after a terminal state. Dashboard and live detail views expose verifier kind, status, confidence, feedback, evidence count, output metrics, recorded usage, and detached model-goal lifecycle when present while omitting raw provider output. This command is not a scheduler, hidden daemon, or background process supervisor.

Write and run a routine manifest:

```bash
python3 -m conductor_runtime write-routine-manifest \
  "$HOME/.codex/conductor/routines/nightly-verification.json" \
  --name nightly-verification \
  --workflow conductor-workflows/verification-sweep.json \
  --daily-at 02:30 \
  --timezone UTC \
  --desktop-notify \
  --allow-writes \
  --max-minutes 30
python3 -m conductor_runtime validate-routine-manifest "$HOME/.codex/conductor/routines/nightly-verification.json"
python3 -m conductor_runtime list-routines --json
python3 -m conductor_runtime status-routine "$HOME/.codex/conductor/routines/nightly-verification.json"
python3 -m conductor_runtime run-routine-now "$HOME/.codex/conductor/routines/nightly-verification.json" --approve routine:nightly-verification
python3 -m conductor_runtime pause-routine "$HOME/.codex/conductor/routines/nightly-verification.json"
python3 -m conductor_runtime resume-routine "$HOME/.codex/conductor/routines/nightly-verification.json" --approve routine:nightly-verification
python3 -m conductor_runtime update-routine-schedule "$HOME/.codex/conductor/routines/nightly-verification.json" --weekdays-at 08:30 --timezone America/Los_Angeles --approve routine:nightly-verification
python3 -m conductor_runtime update-routine-schedule "$HOME/.codex/conductor/routines/nightly-verification.json" --cron '*/15 8-18 * * 1-5' --timezone America/Los_Angeles --approve routine:nightly-verification
python3 -m conductor_runtime supervise-routines --approve routine:nightly-verification --approve foreground-supervisor --max-cycles 0
python3 -m conductor_runtime supervise-routines --background --approve routine:nightly-verification --approve background-supervisor
python3 -m conductor_runtime status-background-routine-supervisor <metadata.json>
python3 -m conductor_runtime wait-background-routine-supervisor <metadata.json> --timeout-seconds 3600
python3 -m conductor_runtime terminate-background-routine-supervisor <metadata.json>
python3 dist/conductor-runtime.pyz install-routine-service \
  --routines-dir "$HOME/.codex/conductor/routines" \
  --routine-manifest "$HOME/.codex/conductor/routines/nightly-verification.json" \
  --runtime "$(pwd)/dist/conductor-runtime.pyz" \
  --allow-service-install --allow-writes \
  --approve routine-service-install --approve routine:nightly-verification --approve WORKFLOW_APPROVAL
python3 dist/conductor-runtime.pyz status-routine-service --routines-dir "$HOME/.codex/conductor/routines" --json
python3 dist/conductor-runtime.pyz uninstall-routine-service --routines-dir "$HOME/.codex/conductor/routines" --allow-service-install --approve routine-service-uninstall
```

Routine manifests use schema `conductor.routine_manifest.v1`. They can target either `--workflow PATH` or `--saved-workflow NAME` with the same safe saved-workflow scope and `--arg KEY=VALUE` handling used by `run-saved-workflow`. Source execution retains the legacy Python module launcher. A manifest written by an installed or cached-plugin `.pyz` instead persists that absolute self-launch path plus `runtime_sha256`; loading or execution rehashes the regular no-follow runtime and rejects drift. The launch subcommand, target, workspace, runs directory, maximum workers, and every capability flag must match the manifest's target and policy fields, so handcrafted argv cannot widen authority. The schedule grammar supports `--manual`, `--interval-minutes N`, `--daily-at HH:MM`, `--weekdays-at HH:MM`, `--weekly-at DAY:HH:MM`, `--monthly-at DAY:HH:MM`, `--once-at ISO-UTC`, or `--cron 'MINUTE HOUR DAY-OF-MONTH MONTH DAY-OF-WEEK'`; wall-clock schedules accept a safe timezone identifier. Cron is numeric and at most 128 characters. Every field accepts `*`, one value, comma lists, ascending ranges, and positive `/` steps. Day-of-week uses `0` or `7` for Sunday through `6` for Saturday. Vixie day semantics use both expanded day masks when either field begins with `*`, including a stepped wildcard; otherwise either day match is sufficient. Names, seconds, macros, `L`, `W`, `?`, `#`, wraparound ranges, non-ASCII digits, and malformed or out-of-range values fail closed. Creation canonicalizes whitespace and decimal leading zeros; stored manifests must already contain that canonical expression. Monthly days 29-31 clamp to the final day of short months. The manifest stores the redacted launch argv, workflow fingerprint, policy flags, budget hints, `evidence_status: planned-not-evidence`, `no_process_started: true`, and an external-supervisor contract. Notification opt-in is represented only by one exact `--desktop-notify` argv item; equals-form or duplicate flags are rejected, and no fixed message or approval value is added.

Cron due-slot resolution is bounded to the existing latest-slot seven-day catch-up window and walks timezone-aware UTC minutes rather than constructing ambiguous local timestamps. A nonexistent spring-forward minute does not run. A repeated fall-back local minute represents two distinct UTC slots, each eligible once; persisted `last_scheduled_for_utc` prevents replay after restart. Only the newest currently eligible unmatched slot runs, so the scheduler never launches once per missed interval. The same evaluator is used by one-cycle or multi-cycle foreground supervision, detached supervision, and hash-bound launchd/systemd routine services.

Schedule evaluation chooses only the most recent eligible missed slot within seven days, never replays every missed interval, and consumes paused or blocked slots once. `run-routine-now` (and the compatible `run-routine` command) validates the current target fingerprint, requires `--approve routine:<name>`, and executes even while paused. `pause-routine` suppresses future scheduled slots; `resume-routine` and atomic `update-routine-schedule` require the routine approval. Strict `conductor.routine_controls.v1` and `conductor.routine_supervisor.v1` files store capped control events and execution history under the external `_supervisor` directory without approval values. Duplicate names fail before any execution, and automatic workflow runs use atomic suffix allocation when multiple launches share a timestamp.

`supervise-routines` evaluates due schedules in the foreground; multiple cycles require `--approve foreground-supervisor`. `--background` requires `--approve background-supervisor`, defaults to `--max-cycles 0`, and launches one visible POSIX session with no restart policy. Approval values cross once through anonymous stdin and are never written to argv, environment, metadata, state, logs, or run artifacts by the handoff layer. The worker revalidates the payload, reports readiness before launch returns, writes heartbeat and result counters to strict `conductor.background_routine_supervisor.v1` metadata, rejects a second live/stale/remote supervisor for the same routines directory, and reloads controls/manifests each cycle. Due workflows execute in-process through the normal `WorkflowRunner`, so capability/manual gates, target fingerprints, per-run evidence, timeouts, active-step cleanup, and manifest-requested desktop notification remain enforceable. Notification delivery occurs after the routine timer is disabled and cannot change the recorded result. `status-`, `wait-`, `validate-`, and `terminate-background-routine-supervisor` expose the lifecycle; `list-routines`, `status-routine`, static dashboard, and live API/UI show state and history. The loopback live UI offers control-token-protected pause, resume, and run-now actions; extra run approvals travel only in the POST body. This remains local while-awake scheduling, not an installed or reboot-persistent service, and it does not itself create parity evidence.

`install-routine-service` is the separate opt-in restart-persistent path. It requires one packaged `.pyz`, `--allow-service-install`, `routine-service-install`, every explicit `--routine-manifest` and `routine:<name>` approval, all matching capability flags, and any workflow approval values being durably authorized. The strict `conductor.routine_service_grant.v1` artifact expires within 365 days and stores only SHA-256 approval fingerprints; `all` is rejected. It binds runtime bytes/path, manifest bytes, workflow fingerprints, routines directory, platform, and limits, and permits no unlisted routine. The runtime writes one deterministic launchd LaunchAgent on macOS or systemd user unit on Linux and rechecks the on-disk grant plus all bindings every cycle. `--no-start` stages the reviewed grant and descriptor under external Conductor state without writing to an OS service directory. `update-routine-service` requires `routine-service-update`, stops the old unit before atomic rotation, and forbids in-place platform changes. `uninstall-routine-service` requires `routine-service-uninstall`, removes the user unit and grant, and retains execution history.

Strict `conductor.routine_service_state.v1` records heartbeat, lifecycle, grant hash, PID, and bounded counters without raw approvals. `status-routine-service`, `list-routines --json`, and static/live dashboards distinguish installed, running, stale, blocked, expired, stopped, failed, incomplete, and invalid states. Runtime, grant, manifest, target, descriptor, state, heartbeat, or PID drift requires attention rather than inferred health. Skill installation, runtime packaging, routine-manifest creation, and transient supervision never install this service.

Use the Codex Cloud review lifecycle:

```bash
python3 -m conductor_runtime cloud-submit --task "Fix the checkout regression" --env <environment-id> --allow-network --approve cloud-submit
python3 -m conductor_runtime cloud-status <task-id> --allow-network --approve cloud-read
python3 -m conductor_runtime cloud-review <task-id> --allow-network --approve cloud-read
python3 -m conductor_runtime cloud-apply <task-id> --review-receipt <receipt-json> --allow-network --allow-writes --approve cloud-apply
```

`cloud-submit` stores only a task hash and bounded redacted tool output, not the task prompt. `cloud-review` stores a redacted diff and a receipt containing the raw diff SHA-256. `cloud-apply` re-fetches the diff and refuses to mutate the workspace if the task, attempt, workspace, or hash differs from the reviewed receipt.

Build a redacted local evidence bundle:

```bash
python3 -m conductor_runtime evidence-bundle
python3 -m conductor_runtime write-evidence-bundle "$HOME/.codex/conductor/benchmarks/evidence-bundle.json"
# After live comparison evidence exists:
python3 -m conductor_runtime validate-evidence-bundle "$HOME/.codex/conductor/benchmarks/evidence-bundle.json" --benchmarks-dir "$HOME/.codex/conductor/benchmarks" --require-claim-eligible
```

Evidence bundles use schema `conductor.evidence_bundle.v1`. They summarize local run completion evidence, goal-loop attention status, routine manifests, benchmark report readiness, and comparison report provenance. `--require-claim-eligible` exits nonzero unless the bundle includes at least one comparison report labeled claim-eligible from complete live provenance and backed by a matching source-report fingerprint in `--benchmarks-dir`. This command is an evidence index and automation gate; it does not execute benchmarks, call Claude/Ultracode, turn planned runbooks or routines into evidence, or prove superiority.

The dashboard is a static HTML file. It escapes rendered artifact text, redacts secret-like values, limits excerpts, skips symlinks, and does not execute artifact content. Goal tables summarize status, workflow, iteration counts, latest run/check status, and operator resume guidance from bounded goal JSON reads; malformed goal JSON is shown as invalid and symlinked goal files are skipped. Each listed goal links to a bounded detail section keyed by an opaque goal handle, with the latest 100 iteration rows, verifier status/return-code/byte-count/truncation summaries, omitted raw verifier excerpts, and invalid-artifact states. Each listed run links to a bounded detail section with diagnostics, advisory runtime action summaries, runner heartbeat liveness, prepared launch handoff metadata, step metadata, imported session trace summaries, standard artifact excerpts, artifact listings, log listings, and `agent_map` packet rows with trace/cache/output/log evidence when available. Files under an `agent_team` capture directory and that team's raw provider/hook logs retain file metadata but never receive dashboard excerpts; validated team summaries provide the public view. Runtime action summaries are derived from redacted step state and workflow dependencies: ready steps, dependency-blocked steps, manual-gate operator actions, active steps, and failed-step recovery guidance. They are display-only; they do not execute steps, grant approvals, skip dependencies, or bypass existing CLI/live recovery controls. Benchmark and comparison report tables include normalized evidence-readiness labels such as local benchmark evidence, benchmark failed, report-declared claim eligible, claim-blocked comparison, inconsistent claim provenance, invalid report, and report attention counts. Claude provider reports additionally show provider status, main/helper models, cost, cap overshoot, and whether partial assistant output survived a cutoff, so claim-blocking provenance is visible without reading raw JSON.

Dashboard summary counts are evidence-oriented. A run is `Evidence-complete` only when the runtime state is completed, every recorded step is completed or skipped, all standard conductor artifacts are present, and the final report is no longer the pending template. Stale, malformed, missing, planned, blocked, failed, or incomplete runs are counted as `Needs attention`. Valid live parity runbooks, run manifests, and evidence bundles in the benchmark directory are classified as planned/provenance/index support artifacts rather than unknown-report warnings; they still do not satisfy the live comparison claim gate.

Serve a live local dashboard and control surface:

```bash
python3 -m conductor_runtime serve --host 127.0.0.1 --port 8765
```

The server prints the local URL and a per-server control token in the terminal. The page auto-refreshes run, automatic-orchestration, goal, and benchmark summaries. Auto rows separate immutable handoff status from resolved child status/liveness and flag invalid or unresolved child evidence. The page links each goal to `/goal/<opaque-goal-handle>` for selected-goal detail, links each run to `/run/<opaque-run-handle>` for selected-run detail, shows diagnostics, manual-gate approval guidance, advisory runtime action summaries, runner heartbeat liveness and active child count, imported session trace summaries, run/phase/step durations, step kinds, process output metrics, bounded agent/item counts, and bounded `agent_map` packet trace rows when available, and includes run-level pause/resume/stop/restart/terminate/recover forms plus retry, reset, and skip forms for recorded non-gate workflow steps. An eligible running current-schema team additionally shows a compact member selector and follow-up field; the form delegates to the same queue contract as the CLI and vanishes when the team completes or spends its allowance. The recovery form requests guarded `--retry-running` semantics but still fails unless cross-process ownership and recorded process identities prove the old execution is inactive. Packet rows expose audited `retry-packet` controls: failed, timed-out, output-missing, or stale-cache packets show `Retry packet`, while completed or cached packets show `Rerun packet`. The control increments only that packet's generation, binds the reset to its current item hash, and leaves compatible sibling cache entries reusable when the run is later resumed. Runtime action summaries classify ready, dependency-waiting, operator-action, failed-recovery, and active steps without executing anything. The selected-goal detail page and `/api/goal/<opaque-goal-handle>` expose bounded redacted goal metadata without raw verifier stdout/stderr excerpts or internal paths. The selected-run detail page links to `/api/run/<opaque-run-handle>`, which returns an allowlisted `conductor.run_detail.v1` JSON payload for local tools with artifact/log excerpt text omitted by default, and to `/run/<opaque-run-handle>/workflow.json`, which returns the run's validated workflow as redacted JSON with private metadata removed and does not write a destination file. Manual-gate approval guidance is display-only: resume from the CLI with `--approve` after chat-visible/operator approval. The control token is not embedded in the HTML and does not grant manual-gate approval; paste it into a control form only when taking a supported run/step control action. Live links and browser-rendered forms use non-reversible run and step handles instead of raw run directory names or raw step ids; packet forms additionally submit the canonical bounded packet index, while team forms submit a bounded member id and instruction. The internal control helper also accepts a direct child `run_id` and `step_id` for local scripted callers and tests after token, path, and symlink validation. Control POSTs require the token, reject cross-origin POSTs when an `Origin` header is present, resolve selected runs under `--runs-dir`, reject symlinked run paths, cap reason or instruction input under dedicated limits, serialize live mutations per run inside the server process, and delegate state changes to the same audited recovery/control implementation used by the CLI.

`serve` binds to `127.0.0.1` by default. A non-loopback bind such as `0.0.0.0` requires `--allow-remote`; use that only behind an appropriate local network security and authentication boundary. The control token is an operator/CSRF guard for this local tool, not a production auth system. The runtime does not provide production TLS, user accounts, browser auto-open, or a background daemon.

Private transcript cookies intentionally omit `Secure` because the default server is loopback HTTP. An explicitly remote deployment must terminate TLS at its external boundary and prevent direct unencrypted access to the runtime port.

Each `run`/`run-saved-workflow` execution holds `.runner-execution.lock` for the complete `WorkflowRunner.execute()` call and writes `runner.json` while active. The no-follow lock must be a single-link regular file; supported POSIX systems use a nonblocking advisory lock, while an in-process lock also prevents same-process overlap. A second runner fails before heartbeat or state ownership changes. The raw runner file includes schema, runner status, current event, process id, process group id, session id, hostname, start timestamp, latest heartbeat timestamp, mode, workspace, maximum worker count, current `state.json` run status when readable, and bounded active child identities. Child rows contain only PID, process-group id, session id, and start timestamp, never argv, prompt, or command text. Process start and finish update metadata synchronously around every runtime-launched child, and a final timestamp is written when the runner exits.

Static and live dashboards render compact runner status plus computed liveness: active runners with recent heartbeats are `alive`, active runners whose heartbeat is older than the runtime threshold are `stale`, finished runners are historical, and older runs without metadata remain readable as unavailable. This lets an operator distinguish older runs with no runner metadata, recently alive active runs, stale/interrupted active runs, final runner ownership, and the workflow's own terminal state. This is observability metadata plus the explicit `terminate-run` signal control described below; it is not a scheduler, hidden daemon, arbitrary process supervisor, or automatic restart mechanism.

The `status` command keeps its default raw `state.json` output for compatibility. Use `status RUN_DIR --with-runner` to print an envelope containing both redacted run state and summarized runner heartbeat/liveness metadata.

Run a write-capable workflow only after an explicit gate:

```bash
python3 -m conductor_runtime run conductor-workflows/bugfix-repro.json --allow-agent --allow-writes --approve implement-bugfix
```

Retry a failed or blocked step, then resume the run:

```bash
python3 -m conductor_runtime retry-step <run-dir> <step-id> --reason "fixed local input"
python3 -m conductor_runtime run conductor-workflows/<workflow>.json --resume <run-dir> <needed capability flags>
```

Queue and inspect a bounded follow-up for one running teammate:

```bash
python3 -m conductor_runtime queue-team-task <run-dir> <agent-team-step-id> --member <member-id> --instruction "Recheck the bounded failure mode"
python3 -m conductor_runtime queue-team-task <run-dir> <agent-team-step-id> --member <member-id> --instruction "Stop the broad search and inspect this boundary" --interrupt-current
python3 -m conductor_runtime list-team-inbox <run-dir> <agent-team-step-id> --json
python3 -m conductor_runtime list-team-questions <run-dir> <agent-team-step-id> --json
python3 -m conductor_runtime read-team-question <run-dir> <agent-team-step-id> <question-id>
python3 -m conductor_runtime reply-team-question <run-dir> <agent-team-step-id> <question-id> --reply "Preserve compatibility and add the exact regression check"
python3 -m conductor_runtime list-team-plans <run-dir> <agent-team-step-id> --json
python3 -m conductor_runtime read-team-plan <run-dir> <agent-team-step-id> <task-id>
python3 -m conductor_runtime review-team-plan <run-dir> <agent-team-step-id> <task-id> --decision approve --feedback "Minimal scope and deterministic verification are present"
```

Rerun one observed `agent_map` packet while preserving compatible sibling results:

```bash
python3 -m conductor_runtime retry-packet <run-dir> <agent-map-step-id> 3 --reason "refresh packet 3"
python3 -m conductor_runtime run conductor-workflows/<workflow>.json --resume <run-dir> <needed capability flags>
```

Reconcile a run left active by hard process interruption, then resume it:

```bash
python3 -m conductor_runtime recover-run <run-dir> --retry-running --reason "confirmed runner and tracked children exited"
python3 -m conductor_runtime run conductor-workflows/<workflow>.json --resume <run-dir> <needed capability flags>
```

Continue one checkpointed interrupted `codex_exec` on its existing Codex thread, then resume the workflow:

```bash
python3 -m conductor_runtime recover-run <run-dir> --resume-codex --reason "continue checkpointed Codex thread"
python3 -m conductor_runtime run conductor-workflows/<workflow>.json --resume <run-dir> <needed capability flags>
```

Reset a step and all transitive downstream dependents:

```bash
python3 -m conductor_runtime reset-step <run-dir> <step-id> --cascade --reason "upstream input changed"
```

Skip a non-gate step with an audit reason:

```bash
python3 -m conductor_runtime skip-step <run-dir> <step-id> --reason "documented external evidence replaces this check"
```

`skip-step` cannot skip `manual_gate` steps. Resume with the required `--approve <token>` instead.

Restart every step in a run, then resume from the same run directory:

```bash
python3 -m conductor_runtime restart-run <run-dir> --reason "fresh pass"
python3 -m conductor_runtime run conductor-workflows/<workflow>.json --resume <run-dir> <needed capability flags>
```

Save a run's validated workflow JSON for reuse:

```bash
python3 -m conductor_runtime save-run-workflow <run-dir> conductor-workflows/saved-copy.json
```

## Safety Model

The runtime is a local automation layer, not a safety bypass. It blocks or requires explicit flags for:

- write-capable shell commands,
- destructive shell commands,
- network-capable shell commands,
- shell commands that reference absolute paths or parent-directory paths, which require `--approve external-path` or a step-specific approval,
- high-risk steps,
- Codex agent calls,
- parallel agent calls,
- manual gates.

These flags are intentionally verbose because they become part of the command history and run evidence. They are not a substitute for careful review.

Run artifact IO uses fd-relative, no-follow helpers for run creation, resume path checks, `state.json`, standard artifacts, workflow artifacts, logs, packet item files, and agent-map cache/output reads and writes. The runtime still treats every run directory as local untrusted data; concurrent local filesystem mutation can make a run fail closed rather than bypass these checks.

## Recovery Controls

State-changing recovery commands edit `state.json` and append an audit row to `05-decision-log.md`; they do not delete logs or captured artifacts. `recover-run` additionally finalizes the proven-inactive `runner.json` with its recovery resolution, while `save-run-workflow` leaves run state unchanged and writes a new validated workflow JSON file at the requested destination. Controls use the shared `.control.lock`, and execution-sensitive mutations also use `.runner-execution.lock`.

- `retry-step RUN_DIR STEP_ID`: only works when the step is currently `failed` or `blocked`.
- `queue-team-task RUN_DIR STEP_ID --member MEMBER [--interrupt-current]`: appends one bounded operator follow-up to a running current-schema `agent_team` that explicitly grants `max_operator_tasks`. The optional flag binds and interrupts only the named member's active turn before same-session redirect delivery. It is a local write serialized against interruption reconciliation and final team completion; instruction text is retained only in the private external inbox and task state needed for delivery.
- `list-team-inbox RUN_DIR STEP_ID`: reads the current team/inbox atomically and returns only IDs, hashes, generations, statuses, routing, and counts.
- `list-team-questions RUN_DIR STEP_ID`: lists bounded teammate question routing, status, and hashes without private text.
- `read-team-question RUN_DIR STEP_ID QUESTION_ID`: explicitly reads one private question and any retained reply.
- `reply-team-question RUN_DIR STEP_ID QUESTION_ID (--reply TEXT|--reply-file PATH)`: secret-screens and answers one pending question; the enabled runner activates its pre-reserved same-session response task.
- `list-team-transcripts RUN_DIR STEP_ID`: lists current-generation private transcript identity, lifecycle, bounds, counts, and hashes without assistant message text.
- `read-team-transcript RUN_DIR STEP_ID TRANSCRIPT_ID [--follow] [--timeout-seconds N]`: explicitly reads one private redacted transcript or follows its atomic event history until terminal state or timeout. This command is read-only; targeted guidance still uses `queue-team-task`.
- `team-console RUN_DIR STEP_ID [--member MEMBER] [--layout auto|columns|stack] [--follow]`: explicitly displays one coherent private multi-member snapshot or follows validated changes until terminal state. History/event/message, width, refresh, and timeout bounds are local display limits; `--json` is one-shot and private. The command has no write authority.
- `list-team-plans RUN_DIR STEP_ID`: lists plan reviewer, lifecycle, revision, and hashes without private plan or feedback text.
- `read-team-plan RUN_DIR STEP_ID TASK_ID`: explicitly reads one private current plan, criteria, and latest review.
- `review-team-plan RUN_DIR STEP_ID TASK_ID --decision approve|reject (--feedback TEXT|--feedback-file PATH)`: secret-screens and commits one operator decision under the same plan-state lock used by the waiting runner. It is a local write and launches no provider.
- `retry-packet RUN_DIR STEP_ID PACKET_INDEX`: resets one observed packet in an inactive `agent_map` run. It binds the reset to the canonical hash of the packet's complete source-item list, increments only that packet's generation, marks the parent step pending, and preserves compatible sibling cache entries. `--cascade` also resets transitive downstream steps without widening the selected upstream packet reset.
- `recover-run RUN_DIR --reason ...`: reconciles run state after a hard interruption. Recovery requires a free execute-wide POSIX lock, same-host runner metadata, an inactive owner PID, strict tracked-child metadata, and no live recorded child session. An interrupted running step is marked failed by default because its outcome is uncertain; `--retry-running` explicitly resets that one step for a fresh invocation. Mutually exclusive `--resume-codex` is available only for exactly one interrupted `codex_exec` with a strict active checkpoint matching the run workflow, step model, sandbox, output, and resume limit. It binds the exact checkpoint bytes so later mutation fails before provider launch. If interruption occurred between steps, recovery preserves completed work and returns the run to its derived completed or resumable state. Recovered runner metadata is finalized and the resolution is audited.
- `reset-step RUN_DIR STEP_ID`: marks a step `pending` so a later `run --resume` re-runs it.
- `skip-step RUN_DIR STEP_ID --reason ...`: marks a non-gate step `skipped`; skipped dependencies are accepted by downstream steps on resume.
- `pause-run RUN_DIR --reason ...`: requests a pause. If a workflow is active, the runner honors it before starting the next step and records `paused`; if it is inactive, the run is marked `paused` immediately.
- `resume-run RUN_DIR --reason ...`: clears a paused run so a later `run --resume` can continue.
- `stop-run RUN_DIR --reason ...`: requests a stop. If a workflow is active, the runner honors it before starting the next step and records `stopped`; if it is inactive, the run is marked `stopped` immediately.
- `restart-run RUN_DIR --reason ...`: resets every workflow step to `pending`. If a workflow is active, the runner honors it at the next step boundary and records `needs_resume`; if it is inactive, the run is reset immediately. Existing artifacts/logs are preserved, and every `agent_map` cache generation is incremented so packet maps rerun fresh.
- `terminate-run RUN_DIR --reason ...`: sends `SIGTERM` to the process group of a validated active session-isolated runner. A detached worker handles TERM by killing and reaping every active step process group before unwinding. `--force` sends `SIGKILL` when the platform exposes it and therefore cannot perform that graceful child cleanup. The command rejects non-active or terminal run state, missing or invalid runner metadata, inactive runners, mismatched runner run status, stale or invalid heartbeats, other-host metadata, the current process/group/session, dead PIDs, and runner metadata whose process group or session id does not match the runner pid. A pending audit record is written before signaling; successful sends are recorded under `termination_events`, `control_events`, and `05-decision-log.md`, and the run is marked `stop_requested`. Use `stop-run` for normal foreground or step-boundary cancellation.
- `save-run-workflow RUN_DIR DESTINATION`: writes the run's validated, redacted `workflow.json` to a new destination for reuse. It refuses existing destinations and symlink paths, creates missing destination parents component-by-component without following symlinks, then creates the file relative to the verified parent directory.
- `record-usage RUN_DIR`: records operator-supplied measured token/cost usage at run level or for one `--step-id`. Step-level records roll up into `state.usage`; run-level records replace the current run usage summary.
- `import-usage RUN_DIR USAGE_JSON`: reads a bounded local JSON artifact, extracts supported measured usage fields, and records them through the same audited usage path. It supports common `input_tokens`/`output_tokens`, `prompt_tokens`/`completion_tokens`, Anthropic cache input token fields, `total_tokens`, `cost_usd`, and `total_cost_usd` shapes. It rejects malformed, oversized, symlinked, ambiguous, negative, oversized-token, non-finite, duplicate-key, or internally inconsistent artifacts.
- `import-session-trace RUN_DIR TRACE_JSON`: reads a bounded local JSON or JSONL artifact containing event arrays such as `events`, `trace_events`, `session_trace`, `session_trace_events`, `sessions[].events`, one JSON object per line, OpenAI Responses-style local `output[]` rows, or Anthropic/Claude-style local `message` rows. It normalizes redacted provider/session/agent/step/model/tool/status/timing/token summaries, writes them under `state.session_trace_events`, and appends an audit row. Text detail is taken only from scalar explicit `detail` or `summary` fields; transcript-like raw `message`, `content`, `description`, `input`, `output`, `text`, and tool `arguments` fields are not persisted. With `--record-usage`, it also aggregates supported numeric token/cost fields already present in the current local trace artifact and records them through the same audited usage path in the same state transaction. It rejects malformed, oversized, symlinked, duplicate-key, deeply nested, over-count, non-finite, unknown-step, unsupported-row, missing-usage, ambiguous-usage, or secret-like-source artifacts. It does not persist raw prompts, raw stdout/stderr, raw provider logs, or call provider trace or billing APIs.
- `diagnose-run RUN_DIR`: read-only diagnostics for state/workflow errors, stale runners, failed/blocked steps, incomplete evidence, and `agent_map` cache/packet trace recovery issues, with suggested audited recovery commands and JSON output.
- `run-console RUN_DIR [--follow]`: read-only content-free run and Codex progress display. It cannot approve, steer, retry, resume, terminate, or otherwise mutate a run.
- `--cascade`: for `retry-packet`, `retry-step`, and `reset-step`, also resets every transitive downstream dependent. For `skip-step`, leaves the requested step skipped and resets downstream dependents.

State-changing control events are recorded under `state.json` as `control_events` with redacted reasons and affected steps. Packet retry events also record the canonical packet index and resulting packet generation; interruption recovery records its resolution, provider-continuation choice, prior runner status/PID, and tracked-process count. `terminate-run` records capped `termination_events` with the signal name and validated process metadata. Reason text is capped at 512 characters for every caller. Source `state.json`, `workflow.json`, `runner.json`, Codex checkpoint and terminal evidence, packet trace, item source, and the shared control and execution lock paths are checked as applicable before control actions; run-control source reads use bounded no-follow regular-file opens. Direct retry/reset/skip/packet mutations acquire execution ownership and reject active or interrupted state, preventing races with the runner. Reset, skip, restart, and affected cascade operations refuse to discard a pending locally completed Codex turn; ordinary retry/resume consumes it first. Otherwise reset, retry, skip, and restart invalidate affected Codex continuation bindings. `retry-packet` additionally rejects non-`agent_map` steps, invalid or unobserved indices, malformed or conflicting trace rows, unsafe historical item sources, item-list drift, and tampered packet-generation state. Live browser forms are shortcuts over these same commands: general run recovery uses guarded fresh retry, the distinct `Resume Codex thread` action requests exact checkpoint binding, packet-row retry forms submit an opaque parent step handle plus the canonical packet index, and all forms preserve the selected-run return path. Manual-gate approval remains CLI-only.

Usage records are stored under `state.json` as `usage`, step token/cost fields, and capped `usage_events`, then audited in `05-decision-log.md`. `record-usage` requires at least one non-negative token or cost value, derives `total_tokens` from input plus output when possible, rejects inconsistent totals, rejects secret-like source labels, and redacts reasons. `import-usage` extracts only numeric usage fields from local JSON and never persists the raw provider/tool artifact. `import-session-trace --record-usage` extracts only numeric usage fields from the current local trace artifact after trace normalization; missing usage, non-finite values, inconsistent event totals, and ambiguous aggregate totals are rejected before trace or usage state is written. These commands record measured values supplied by an operator or external meter; they do not estimate usage from logs or contact provider billing APIs.

Session traces are stored under `state.json` as capped `session_trace_events` plus a compact `session_trace_summary`, then audited in `05-decision-log.md`. Runtime-launched Codex processes emit JSONL and automatically contribute bounded event metadata and terminal measured usage. `import-session-trace` adds equivalent normalized summaries from local provider exports; raw prompts, message text, provider logs, tool arguments, and unsupported rows are rejected or ignored. With `--record-usage`, imported numeric usage targets the CLI `--step-id` or the run summary. Re-importing the same artifact can overwrite the target summary, so repeated external imports remain the operator's responsibility. This is not billing-grade cost evidence or a complete raw provider archive.

Each single `codex_exec` writes a strict atomic checkpoint under its run artifacts when the streamed `thread.started` event arrives. The record contains the canonical session UUID, hashes of the workflow and original safety-wrapped prompt, model/sandbox/output bindings, mode, bounded resume count, timestamps, and terminal output hash or failure class. It contains no prompt, command argv, message content, or provider output.

Every ordinary `codex_exec` launch and each launched `agent_map` packet also creates one mode-`0600`, atomically replaced, self-hashed `conductor.codex_progress.v4` record under `.codex-progress/<step-hash>/`. It consumes the existing Codex JSONL callback and adds no provider event request or model call. The record binds workflow, step, packet index when applicable, invocation UUID, step attempt, start/update/finish timestamps, sandbox, model, effort, exact invocation cap, a SHA-256 of a canonical session UUID, bounded event/turn/item/failure counters, fixed item categories, last lifecycle event, and terminal top-level token totals when emitted. If the stream already contains native collaboration items, it also records only fixed collaboration-tool, failure, distinct-agent, and latest fixed-status counts. Receiver UUIDs are retained only in bounded observer memory for deduplication and are never persisted or hashed. V4 records whether the runner authorized native delegation plus its child-thread and depth ceilings. After a successful explicitly native call, it queries only parent/child ids, rollout paths, workspace, and gross totals from the read-only local Codex index; binds direct depth-one descendants to the private parent checkpoint and exact workspace; rejects grandchildren or more than 255 children; and maps at most the final 8 MiB of each no-follow rollout up to 512 MiB while parsing only a token-count line of at most 256 KiB. It persists only reconciliation status, parent-plus-child session/child counts, and aggregate input/cached/output/gross/weighted integers. No identity or path is retained. Index, path, schema, token, or topology failure reports `unavailable` and cannot change provider execution or step success. The observer explicitly states that it enabled no agents. It never stores raw session ids, prompts, messages, reasoning, commands, command output, file paths, diffs, tool arguments/results, agent ids, agent-state messages, or unknown payload fields. Item-only writes are throttled to at most four per second; thread, turn, collaboration, reconciliation, and terminal boundaries flush immediately. Malformed or duplicate-key stream JSON is ignored. Storage, validation, or path failure disables only that observer and never changes execution, output validation, telemetry, checkpoint, terminal, cache, or recovery behavior.

`validate-codex-progress` performs strict duplicate-key, size, mode-safe path, field, count, timestamp, hash, and cross-field validation. `inspect-codex-progress` emits only the bounded content-free summary. Static dashboard, live detail, selected-run API, diagnostics, and `run-console` treat progress as informational: invalid or stale progress is a warning, native-agent observation truncation is a visible lower-bound warning, complete reconciliation shows aggregate weighted/gross/session counts, and unavailable reconciliation keeps the top-level-usage warning. Step state, checkpoints, terminal receipts, packet traces, outputs, and telemetry receipts remain authoritative. Packet summaries are bounded, active-first, and retain the exact per-invocation cap. Strict v3, v2, and v1 receipts remain readable and exported as `codex-progress-v3`, `codex-progress-v2`, and `codex-progress-v1`. The journal does not itself enable native subagents, alter hard-budget enforcement, reconcile billing cost, or treat local provider metadata as execution authority.

The same bounded observer may write private current `conductor.codex_step_terminal.v2` before provider-process return after exactly one final `turn.completed`. The write-new self-hashed record binds the active checkpoint bytes, workflow and step, original task prompt, current invocation base/effective prompt, any lifecycle-context receipt, model, effort, exact call budget, output raw/redacted hashes, raw resumable session plus hash, bounded telemetry, resolved workspace-path hash, and either read-only authority or one exact direct-write result fingerprint. Retained v1 remains readable. It is created only after the canonical output exists; a direct-write call takes one bounded terminal-time workspace snapshot and performs no prelaunch snapshot. The exact current run directory is omitted when legacy state lives inside the workspace, but sibling source remains fingerprinted.

Ordinary retry or resume validates terminal evidence before same-thread continuation or fresh launch. It accepts only the declared raw or redacted output checkpoint, verifies the active or derived completed Codex checkpoint and exact direct-write workspace result, normalizes redaction crash-safely, records telemetry once by terminal hash, terminalizes the checkpoint, and marks the step recovered with zero provider invocations. Crashes after telemetry, checkpoint, or step commit are idempotent; a completed step consumes any final leftover marker. Artifact, output, workspace, checkpoint, prompt, budget, effort, model, sandbox, workflow, or workspace-path drift fails before provider launch. Reset, skip, restart, and affected cascades refuse to invalidate pending terminal evidence. `validate-codex-step-terminal` and `inspect-codex-step-terminal` are local read-only commands; public inspection, schemas, diagnostics, and static/live/API views expose only bounded hashes, counts, and totals, while raw sessions, provider events, output paths, and private terminal bodies are suppressed.

If a normal terminal record cannot be created, post-return validation remains unchanged. Hard local loss can leave only the streamed checkpoint active; after `recover-run --resume-codex`, state stores the approved checkpoint hash and the next invocation uses `codex exec resume`. The continuation prompt reasserts Conductor safety and scope without replaying the original task, and resumed stdout must emit the same `thread.started` UUID. Dashboard step metadata exposes only a hash of that UUID. This starts a new turn in the same provider thread. It cannot resurrect a turn still in flight, determine whether a remote turn completed before exact local terminal evidence became durable, or recover uncertain prior token spend.

Handled provider failure no longer discards already emitted answer text. On nonzero exit or timeout, ordinary `codex_exec` first normalizes a present final-message file through the existing bounded redaction path. If that file is absent or empty, only a duplicate-key-safe strict JSONL stream with one thread and one terminal event may supply completed `agent_message` text; malformed, truncated, unterminated, or ambiguous streams synthesize nothing. The text is written only to the step's existing workflow-bound capture artifact. State records no text: only stream validity, terminal class, budget-exhaustion boolean, preservation boolean, fixed source class, byte count, and SHA-256. The step, provider, checkpoint, and run remain failed, and the runtime performs no automatic retry or extra verification. Static/live detail, diagnostics, and non-persisted `conductor.run_console_snapshot.v5` independently rehash the no-follow artifact and show `valid`, `invalid`, or `none`; drift is action-required. Content-free progress and terminal receipts remain message-free and unchanged.

Pause, stop, and restart are step-boundary controls, not an OS process manager. They do not kill or restart an already-running subprocess or Codex child process; they affect the next workflow step once the runner checks state again. `terminate-run` is separate: it is an explicit local OS signal request for a validated session-isolated active runner, not an arbitrary PID killer or daemon. Handled runner exceptions kill/reap active children through the existing process wrapper and finalize any still-running step as failed. `recover-run` is reserved for state left behind when that normal unwinding could not execute, such as hard process loss. A valid local terminal marker is recovered before any provider work; otherwise fresh retry remains explicit and a valid single-step Codex checkpoint enables the separate same-thread continuation path described above. The dashboard liveness badge is intentionally quick to mark stale runners; `terminate-run` uses a separate freshness gate that rejects runner heartbeats older than 300 seconds or materially future-dated.

The live `serve` command is a browser-accessible wrapper over these same controls. It does not add new mutation semantics; selected-run workflow JSON export is a read-only download path.

## Workflow Hooks

Workflow files may define top-level `hooks` for local quality gates:

```json
{
  "hooks": [
    {
      "id": "preflight",
      "event": "before_run",
      "command": ["python3", "-m", "conductor_runtime", "doctor"],
      "on_failure": "block"
    }
  ]
}
```

Run/step lifecycle events are `before_run`, `after_run`, `before_step`, and `after_step`. Step events may include a `steps` array to target specific step IDs. Team events are `team_task_created`, `team_task_completed`, and `team_member_idle`; they target only `agent_team` steps and may add `members` and `tasks` filters. Provider-process events are `agent_start` and `agent_stop`. They cover `codex_exec`, every launched uncached `agent_map` packet, team plan/review calls, ordinary team turns, and team quality retries, with optional `steps`, `profiles`, `scopes`, `members`, and `tasks` filters.

Provider hooks receive strict `conductor.agent_lifecycle_hook_input.v1` JSON on stdin. It contains workflow and invocation hashes, bounded step/profile/member/task identity, scope, attempt/generation/round/packet/retry/revision identity, sandbox and token settings, model/prompt/session hashes, and hash-only process outcome evidence. It never contains prompt text, provider output, raw model/session values, commands, or paths. `agent_start` defaults to `block` and runs before process launch. `agent_stop` defaults to advisory `warn`, or may explicitly use `block`. A matching blocking configuration fsyncs strict private `conductor.agent_lifecycle_stop_gate.v1` state before launch, includes all matching stop hooks in workflow order, durably claims each hook before execution, and durably records its bounded hash-only result. Terminal recovery continues only unclaimed hooks. A gate found in `running`, `failed`, or `uncertain` state rejects the provider result without replay; missing, changed, renamed, symlinked, over-limit, cross-bound, or ambiguous evidence also fails closed. Advisory-only workflows do not create or read a gate. Provider hooks are local and read-only, cannot request write, destructive, network, or external-path authority, cannot use shared `capture`, and cannot request automatic retry. They run in the provider's exact execution workspace. Omission bypasses lifecycle payload construction and adds no hook process or model call.

`agent_pre_tool` is the opt-in provider-native event for those same direct, map, plan, review, turn, and quality-retry scopes. It requires `tools` with 1-32 exact native tool names or `*`, one executable name on `PATH` with no arguments or path components, `block` or `warn`, and a 1-30 second timeout. It supports the lifecycle step/profile/scope/member/task filters but cannot declare writes, destructive behavior, network, external paths, capture, context injection, or retries. Unknown or mutable verifier executables require `risk: high` plus explicit approval by hook id or `agent-pre-tool-verifier`; task/repository/model text cannot grant that approval.

Before each matching provider invocation, the runtime resolves the verifier outside the source workspace, copies a bounded hash-addressed executable snapshot into external run state, and creates a self-hashed bridge, private config, and fsynced `conductor.agent_native_tool_gate.v1`. A two-pass Codex 0.144 hook audit discovers and trusts only the generated session `PreToolUse` hook, disables unmanaged external hooks, preserves managed hooks, and excludes the config path/hash variables from model shell environments. The native hook timeout is at least 35 seconds and grows to cover the complete configured verifier chain. Restricted-profile command policy is evaluated in the same bridge before custom verifiers, so both contracts compose without handler-order ambiguity.

The bridge receives the Codex proposal, validates and bounds it, and sends strict `conductor.agent_native_tool_hook_input.v1` to matching immutable verifiers sequentially without a shell. This verifier input intentionally contains the raw proposed `tool_input`; retained gate/state/receipt evidence never does. Every matching tool-use id atomically claims a mode-`0600` `conductor.agent_native_tool_receipt.v1`; concurrent/repeated delivery waits for and validates the complete prior decision instead of rerunning the verifier. Internal failure, timeout, or nonzero blocking verifier denies the tool. Advisory failure allows it and increments warning evidence. Provider terminal delivery and recovery require the exact gate to settle against unchanged config, bridge, snapshots, and terminal receipts before downstream terminal acceptance. An indeterminate, missing, renamed, oversized, symlinked, changed, or inconsistent artifact fails closed without replaying provider work. `state.json`, dashboards, and diagnostics expose a self-hashed aggregate with gate/call/pass/deny/warning counts and gate hashes only.

Current [Codex hook coverage](https://developers.openai.com/codex/hooks) includes Bash/simple shell, `apply_patch`, and supported MCP paths but is explicitly incomplete; `unified_exec` and other uncovered paths are outside this guarantee. `agent_pre_tool` is therefore a supported-tool guardrail, not a universal sandbox. When omitted, the runner takes its original direct provider call path and performs no identity construction, verifier lookup, snapshot, bridge, config, gate, receipt, hook preflight, environment copy, or command rewrite. `conductor-workflows/native-pre-tool-gate.json` is a provider-free validation example that denies Bash and `apply_patch` through the system `false` executable.

A matching direct `workspace-write` step uses an external isolated workspace rather than the source workspace. Conductor copies one bounded tracked snapshot before `agent_start`, verifies the source did not change during preparation, and records strict private `conductor.direct_workspace_transaction.v1` state. Exact local terminal evidence and the passed stop gate bind the invocation, stage result, source-before snapshot, deterministic merge plan, and their hashes before any source mutation. The journal moves from `prepared` to `merging`, then to `merged`; overlapping source drift records `conflict`, and pre-merge rejection records `rejected`. Recovery accepts only source files still at their recorded before or incoming values, finishes remaining file operations exactly once, and rejects unrelated drift, journal/stage tampering, symlinks, missing terminal evidence, or changed gate identity without another provider or hook call. Source edits unrelated to the provider delta are preserved when the merge plan is created. The isolated copy omits the standard VCS, dependency, environment, and cache directories; hooks requiring those directories must not opt into this path. The default external run root satisfies the isolation requirement. An explicitly repo-local run root fails before provider launch. Successful or rejected stages are removed, including cleanup after a crash between terminal removal and stage deletion. Advisory-only and read-only calls keep the prior no-copy path.

An `agent_start` hook may opt into prompt context with `inject_context: true`. It must use fail-closed `block` and declare one or more explicit scopes from `codex_exec`, `agent_map_packet`, `agent_team_plan`, `agent_team_review`, `agent_team_turn`, and `agent_team_quality_retry`. Member/task filters are valid only where the targeted team scope carries that identity. `context_limit_bytes` defaults to 8 KiB and is capped at 64 KiB; stdout must be non-empty, non-truncated, valid UTF-8, secret-redacted, and within that per-hook limit. Multiple matching context hooks retain workflow order inside one untrusted frame capped at 64 KiB, and the final prompt is capped at 4 MiB. The frame states that hook output cannot widen tools, permissions, scope, policy, or higher-priority instructions and escapes its own boundary markers.

Context is appended to the exact provider invocation prompt before launch for fresh and same-thread direct calls, map packets, team planning, lead review, ordinary turns, and quality retries. Before the provider starts, the runtime durably writes a private context log and strict current `conductor.agent_lifecycle_context.v2` receipt under the external run directory. V2 binds workflow, step/scope/invocation/attempt, session mode and session hash, complete packet or member/task/generation/round/retry/revision identity, base/context/effective prompt hashes and byte count, matching/context hook counts, hook-set and hook-input hashes, log path, and timestamp. V1 fresh direct/map receipts remain readable. Exact pre-launch recovery reuses a settled receipt without replaying hooks. Direct checkpoints/terminals, map terminals/cache entries, team plan-approval state, and team turn terminal/completion journals bind the effective prompt and receipt. Recovery revalidates that evidence before accepting durable provider work; missing, tampered, or ambiguous receipt/log evidence fails closed rather than replaying an uncertain provider call.

```json
{
  "hooks": [
    {
      "id": "repository-context",
      "event": "agent_start",
      "steps": ["review"],
      "scopes": ["codex_exec"],
      "command": ["git", "status", "--short", "--branch"],
      "inject_context": true,
      "context_limit_bytes": 4096,
      "on_failure": "block"
    }
  ]
}
```

Hook commands use the same command parser, policy gates, output limits, timeout fields, and `cwd` behavior as `shell` steps. Ordinary hooks may use `capture` and `capture_mode`; repeated team/provider hooks may not, because their uniquely keyed redacted logs and state events are the evidence channel. Team hooks cannot request network, destructive, or external-path authority. A team hook declaring writes may target only workspace-write teams, and its command still may not change the tracked teammate result that is about to merge.

```json
{
  "hooks": [
    {
      "id": "implementation-complete",
      "event": "team_task_completed",
      "steps": ["implementation-team"],
      "members": ["implementer"],
      "command": ["grep", "\"turn_status\": \"completed\""],
      "on_failure": "retry",
      "max_retries": 1,
      "retry_exit_codes": [1],
      "retry_max_tokens": 8000
    }
  ]
}
```

`on_failure` controls failure semantics:

- `block` (default except `agent_stop`): a failed or policy-blocked hook marks the relevant step or run failed/blocked and stops execution. A blocking `agent_start` prevents the provider process from launching. An explicit blocking `agent_stop` rejects result acceptance through its durable gate. Matching direct writes remain isolated and unmerged until every local acceptance check passes; isolated team writes retain their existing parent-owned merge gate.
- `warn`: a failed or policy-blocked hook records a hook event but allows the workflow to continue.
- `retry` (team events only): a listed nonzero exit code resumes that teammate's existing session with bounded redacted feedback, then reruns candidate validation and gates before commit. `max_retries` defaults to 1 and is capped at 4; `retry_exit_codes` defaults to `[2]`; `retry_max_tokens` may narrow each call. Timeout, policy failure, mutation, other exit codes, exhaustion, and team-budget shortage remain blocking failures with no hidden call.

Hook stdout/stderr are written under `logs/hooks/` with redaction and truncation. Team log keys include generation, round, member, task, and quality-retry index. Provider log keys include scope and the first 16 hexadecimal characters of the invocation digest, so parallel packets and repeated turns cannot overwrite one another. Hook events are recorded in `state.json` as `hook_events` with event name, hook id, optional step id, status, timing, return code, failure mode, and bounded process metrics. Team records additionally expose member/task identity, generation/round/retry index, changed-file count, hook-input hash, and workspace fingerprint. Provider records expose only scope/identity fields plus invocation and input hashes. Neither retains the stdin payload. Static and live dashboards show hook counts, selected-run hook detail, quality-retry history counts/statuses, and pending no-auto-replay recovery.

Run/step/team and lifecycle hooks are local quality-gate and process-boundary mechanisms. Prompt injection covers the bounded fresh/resumed direct, map, plan, review, turn, and quality-retry scopes above. Only explicit `agent_pre_tool` uses provider-native interception, and only for upstream-supported hook paths; none of these features is an in-flight session controller, hidden plugin runtime, network webhook system, or universal production policy engine.

## Run Detail Drill-Downs

Dashboard and live detail views summarize run internals without serving raw files:

- advisory runtime action summaries for ready, dependency-waiting, operator-action, failed-recovery, and active steps,
- step id, status, kind, timing, detail text, and bounded agent/item metrics,
- prepared-run launch manifest status, preflight result, non-daemon process model, policy flags, supervisor contract, and resume command when present,
- `agent_map` packet rows with item labels, trace status/timing/process metrics, cache evidence, packet retry generation, bounded output/stdout/stderr excerpts, and item-specific retry/rerun controls,
- standard conductor artifact presence, byte counts, modification time, and capped excerpts,
- artifact and log file listings capped to 40 files per group,
- per-file excerpts capped to 2 KiB even when the dashboard's final-report excerpt limit is higher.

Detail collection resolves selected run IDs under the configured `--runs-dir`, while the live browser surface exposes only opaque run handles in links and opaque step handles in browser-rendered control forms. Detail collection rejects path escapes and `.`/`..`, rejects symlinked run directories, skips symlinked files/directories, redacts secret-like values, escapes rendered HTML, and keeps missing older-run fields readable. JSON detail endpoints use `application/json`, `nosniff`, no-store caching, an allowlisted run-detail schema, redacted error messages, and excerpt metadata without excerpt text; artifact excerpts remain visible in the selected-run HTML view as untrusted escaped data. Lifecycle actions still go through the existing audited recovery controls.

Runtime execution and recovery paths also use bounded no-follow reads for required local metadata and inputs. Oversized required files such as `state.json`, `workflow.json`, workflow templates, and `prompt_file` inputs fail closed with redacted errors. Oversized or malformed optional `agent_map` cache metadata is ignored and treated as an empty cache so retry/resume can continue without trusting corrupted cache state.

## Runtime Metrics

New runs record deterministic local metrics in `state.json`:

- run `started_at_utc`, `finished_at_utc`, `updated_at_utc`, and `duration_ms`,
- prepared-run launch metadata from `launch.json`, including preflight status, no-process-started guarantee, non-persisted approval values, resume command, and external supervisor contract when present,
- runner `status`, `event`, `pid`, `process_group_id`, `session_id`, `hostname`, `started_at_utc`, `heartbeat_at_utc`, `finished_at_utc`, `mode`, `workspace`, `max_workers`, and latest run status in `runner.json`; dashboard, live, API, and `status --with-runner` summaries compute `liveness`, `heartbeat_age_seconds`, and `stale_after_seconds` from that metadata,
- optional workflow step `phase` labels, summarized as phase status, step counts, elapsed local duration, agent invocations/items/cache counts, and recorded token/cost metrics when present,
- step `kind`, `attempt`, `started_at_utc`, `finished_at_utc`, `updated_at_utc`, and `duration_ms`,
- shell/Codex process return code, timeout flag, captured stdout/stderr byte counts, and truncation flags,
- `agent_map` source-item totals, packet totals, cached item/packet counts, maximum items per packet, configured packet ceiling, worker/failure counts, effort, per-call weighted rollout cap, per-attempt map cap, and budgeted provider-invocation count.

The `list-runs` CLI snapshot, static dashboard, live dashboard, selected-run detail pages, selected-goal detail pages, diagnostics, `/api/state`, and `/api/run/<handle>` expose these fields when present and keep older runs readable when the fields are absent. Selected-run detail, `/api/state`, and `/api/run/<handle>` also expose advisory runtime action summaries derived from these fields plus workflow dependencies. Steps without a `phase` render under the `default` phase. Runtime-launched Codex steps record measured terminal token usage automatically when emitted by the CLI; `record-usage`, `import-usage`, and `import-session-trace --record-usage` support other meters and cost data. The runtime never estimates usage from byte counts or log text.

`agent_map` steps write a local cache manifest under the step capture directory after successful packets complete, write one atomic strict cache-entry receipt under `.agent-map-cache-entries/` before each completed packet returns, and append local packet trace rows to `.agent-map-trace.jsonl` for executed, failed, output-missing, timed-out, and cached-reuse packets. The aggregate manifest remains batched for scalability; per-index receipts close its interruption window. On resume the runtime overlays valid receipts into the manifest and still revalidates the complete cache key and output hash, while malformed, duplicate-key, oversized, drifted, or unsafe receipts remain cache misses. Trace rows include the packet's canonical source-list SHA-256 and source-item count without raw prompts or command argv. On a compatible `retry-step`/`run --resume`, cache reuse requires the current workflow fingerprint, whole-step generation, optional per-packet generation, canonical source list, index, prompt, sandbox, model, every member's bounded workspace file/directory content fingerprint, output path, and output hash to match. Any changed or unhashable member invalidates the whole grouped packet. Oversized cached outputs remain misses. Static and live details summarize packet/item counts, manifests, and traces by output path and skip missing, invalid, tampered, malformed, oversized, or symlinked metadata instead of treating it as completed work. `diagnose-run` turns corrupt cache metadata, unreadable traces, missing cached outputs, failed packet traces, output-missing traces, and timed-out packet traces into capped recovery findings with audited `retry-packet` guidance.

`codex_exec.context_from` is the explicit artifact handoff contract. It accepts one through 32 unique direct `depends_on` step ids. Sources may be `write_artifact`, `shell` with `capture`, `codex_exec`, or `agent_map`; ordering a step with `depends_on` alone never injects artifact content. Validation rejects other targets, indirect sources, unsupported kinds, uncaptured shell sources, duplicates, unsafe ids, and over-limit lists. A map backed by a runtime file/artifact must declare `max_packets`, and the statically known combined artifact upper bound must fit 32 before any workflow process starts. Model-authored workflows use the same field and cross-step validation.

Only referenced single-output producers compute a `context_output_sha256` and byte-count receipt, so workflows that omit `context_from` retain the previous output path without extra hashing. A downstream launch requires each source state to be `completed`, never merely skipped. Single outputs must match that receipt. Map outputs must resolve every deterministic packet through the existing workflow/generation/prompt/source/sandbox/model/output cache identity; changed or uncacheable workspace sources, missing entries, and output drift fail closed. Every selected artifact is opened as a regular no-follow file, hashed and excerpted from the same descriptor, and limited to 2 MiB. Symlinks, missing receipts, malformed cache state, source drift, content or size changes, oversized files, more than 32 resolved artifacts, and read failures stop before the downstream provider starts.

The runner frames those snapshots as untrusted dependency evidence, omits run paths, removes nested evidence markers, redacts secret-like values again, limits each excerpt to 8 KiB, and caps the complete section at 64 KiB while retaining a header for every selected artifact. The exact section enters the ordinary prompt hash, checkpoint, continuation, and terminal bindings. Step state and static/live/API run detail contain only fixed contract id, source/artifact counts, section bytes, and SHA-256; they never store evidence text or artifact paths. Generated Ultracode workflows use this contract from collected/reproduction outputs into context mapping, context into analysis, analysis plus packet outputs into synthesis, synthesis into implementation, and synthesis plus implementation plus verification evidence into final review. The transfer adds no planner call, synthesis call beyond the workflow's existing step, retry, watcher, daemon, network request, or repository-local state.

`codex_exec.completion_verdict` accepts only `strict-v1` and only in a read-only sandbox. The runtime appends a fixed fail-closed contract after the task prompt. Output must be one JSON object with exactly `satisfied` boolean, bounded `feedback` string, at most 20 bounded `evidence` strings, and `confidence` in `low|medium|high`. Parsing rejects duplicate keys, non-finite constants, malformed/extra fields, invalid types, oversized values, a satisfied verdict without evidence, and an unsatisfied verdict without actionable feedback.

Provider completion and semantic acceptance remain separate. After ordinary output normalization and telemetry accounting, the runtime reads one regular no-follow snapshot and derives both its SHA-256 receipt and parsed verdict from those exact bytes. A valid `satisfied: true` verdict permits step completion. A valid false verdict or invalid output marks the step and run failed, blocks dependents, retains the redacted artifact, and launches no verifier or retry call within that workflow. State retains only fixed contract id, validity/satisfaction booleans, confidence, evidence count, feedback-presence boolean, output byte count, and SHA-256. Selected run detail independently reopens, hashes, and parses the workflow-bound no-follow artifact; drift becomes invalid and diagnostics remain action-required. Generic artifact and provider stdout/stderr excerpts omit contracted verdict content. Strict local terminal recovery applies the same semantic gate before committing completion and never replays a provider. Generated Ultracode final review always enables this contract after receiving synthesis, implementation, and verification evidence; model-authored workflows may explicitly use the same field.

An explicitly bounded `run-goal` or `run-model-goal` is a separate enclosing authority. When its current run ends on exactly one valid unsatisfied completion verdict, it may recover only that verdict's redacted feedback from the private run artifact and inject it as untrusted context into the next already-authorized iteration. It does not convert the failed run to success and does not run the external verifier for that failed iteration. The goal artifact stores only `conductor.completion_verdict_feedback.v1`: step id, contract, confidence, evidence count, feedback presence/length/hash, and output size/hash. Public goal detail exposes only those fixed fields. Resume derives the expected run id, revalidates the copied workflow and original workflow fingerprint, failed step state, one-read output receipt, parsed false verdict, and goal receipt before recovering text. Invalid, satisfied, missing, multiple, changed, or ambiguous evidence stops fail-closed. Omission, one-shot `run`, and first-pass goal success add no repair scan, prompt content, provider call, or retry.

`run-goal --workflow-completion` removes a redundant external verifier when the workflow already owns an authoritative strict final review. The flag is mutually exclusive with command/model verifier options and preflight fails before goal or run creation unless there is exactly one `strict-v1` step whose transitive ancestors are every other workflow step. After a completed run, a local zero-provider check reopens the bound output through the same one-read loader, validates original/redacted workflow fingerprints and the complete satisfied state receipt, and records `kind: workflow-completion`, `provider_calls: 0`, confidence, evidence count, output size, and SHA-256 without feedback or evidence text. Invalid or drifted completion evidence becomes a fail-closed verifier error. False verdicts still enter the private bounded repair path; generated Ultracode final review already has the required terminal topology.

Each cacheable read-only packet composes one bounded baseline provider-stream observer. A stable UUID-bearing `thread.started` followed by exactly one final `turn.completed` can atomically write private `conductor.agent_map_packet_terminal.v1` before process return only when the canonical output, deterministic redacted output, prompt/cache identity, and complete source-item fingerprint are exact. Recovery runs before receipt overlay, cache lookup, or provider launch. It revalidates the original launch-time fair-share cap, all bindings, output checkpoints, and current source fingerprint; then it writes or verifies the ordinary cache receipt, imports bounded telemetry exactly once using the terminal hash, and removes the marker only after accounting is durable. A crash after accounting reconciles missing step metrics without duplicating the usage event.

`validate-agent-map-packet-terminal` and `inspect-agent-map-packet-terminal` are local read-only commands. Inspection, static/live/API packet views, and schema output expose only bounded identity, counts, totals, and hashes; raw session, provider events, output path, and terminal body remain private and generic excerpts are suppressed. Targeted packet retry, reset, skip, cascade invalidation, and restart fail before state changes when they would discard pending terminal evidence; ordinary `retry-step` remains the recovery route. Missing terminal events, uncacheable source identity, unstable or oversized output, a turn still in flight, and remote completion lost before exact local evidence retain the prior explicit-retry boundary. The observer adds no provider call, prompt branch, token reservation, dependency, permission, network access, daemon, or background process.

`retry-packet` increments only the selected packet generation and binds it to the current canonical source-list hash, so a later `run --resume` reruns that complete single- or multi-item scope while preserving compatible sibling cache entries. Before launch, the runner removes its prior regular output through fd-relative no-follow-safe IO so stale output cannot masquerade as new success. The command requires an inactive run and an observed packet trace; older inline or artifact-backed traces without item hashes derive the same deterministic packetization before binding, while historical mutable `items_file` traces without a hash must fall back to a whole-step control. Item or grouping drift fails closed before provider launch. `reset-step` increments the whole map generation and clears targeted packet bindings.

Optional `agent_map.max_packets` limits model invocations independently from source-item count. When the source exceeds that ceiling, deterministic contiguous groups are encoded as JSON string arrays; no group may exceed 64 validated items. A ceiling that cannot safely represent the resolved source fails before provider launch. If `max_packets` is absent, legacy one-item-per-call behavior is preserved. When `max_total_tokens` or the top-level map default is present, the runner divides that weighted rollout budget fairly over only packets that are not valid cache hits, then takes the lower of that share and the per-call cap. Cached packets consume no new allocation. An underfunded share fails before provider launch. The total is a limit for one map execution attempt: an explicit later retry authorizes a newly bounded attempt, while hard local interruption can leave prior provider spend uncertain.

## Workflow Draft Generation

`plan-workflow` turns task text into a conservative `conductor.workflow.v1` draft:

- task text is embedded as untrusted, redacted artifact data,
- `--profile standard` is the default simple planner; `--profile ultracode` adds multi-phase charter/risk/context/review/synthesis/verification/final-review structure,
- the `ultracode` profile automatically creates a bounded `agent_map` review from the collected repository file list when agents are enabled and no packet source is supplied,
- `rg --files` context capture is included by default,
- `--failing-command` creates reproduction shell steps,
- `--check-command` creates verification shell steps,
- `--packet-item` can be repeated to create explicit read-only review packets,
- `--packet-items-file` points at a workspace-relative line-oriented item file for packet expansion at runtime,
- `--packet-glob` can be repeated to resolve workspace files into packet items at draft time,
- `--packet-max-items` caps generated packet sources from 1 to 1,000 items,
- `--packet-max-packets` sets the generated model-packet ceiling and defaults to 25; resolved groups still cannot exceed 64 items,
- `--saved-command` writes the validated draft as a safe saved workflow command using JSON-compatible exports,
- `--saved-output` chooses a `.js` or `.mjs` destination for `--saved-command`; existing destinations and symlink paths are rejected,
- read-only Codex analysis is included unless `--no-agent` is supplied,
- `--write-capable` adds a manual gate and a workspace-write Codex implementation step,
- generated agent workflows carry `balanced-v2`: top-level `high` effort, 12,000 weighted rollout tokens per ordinary call, 24,000 for write calls, and `agent_map` overrides of `medium`, 4,000 per call, 100,000 per map attempt, and at most 25 model packets,
- packetized drafts add a bounded `agent_map` review step and reject `--no-agent`,
- packet items from inline lists, globs, workspace files, and artifact files are validated as single-line workspace-relative scopes, bounded by size/count, rejected when secret-like, and sanitized when they contain task boundary markers,
- packet scopes containing high-risk terms such as auth, billing, secrets, production, or migrations escalate the generated or runtime agent step to high risk,
- task boundary markers are escaped before task text is embedded in prompts or artifacts,
- secret-like workflow names and command arguments are rejected,
- command strings are parsed by the same shell normalizer/classifier used by runtime execution,
- pathful executables such as `./tool` or `/tmp/tool` are not trusted by basename and require external-path plus write/network/destructive gates,
- embedded-language text tools such as `awk` and `sed` are not classified as read-only, write-capable options such as `sort -o`, `find -fprint`, `git --output`, `git push`, `gh`, and mutating `curl` request/body options are write-gated, and `python -m conductor_runtime` is treated as read-only only for explicitly non-mutating subcommands such as `--help`, `validate`, profile/memory/team-state/operator-inbox/operator-chat/transcript/Codex-progress validate/list/inspect/read commands, `validate-saved-workflows`, `validate-desktop-notification`, receipt/background/routine/provider-evidence validators, `inspect`, `list`, `list-runs`, `run-console`, `list-goals`, `list-routines`, `status-routine`, background status/wait commands, `diagnose-run`, `list-saved-workflows`, `inspect-saved-workflow`, `status`, plain `doctor`, `schema`, `evidence-bundle`, `validate-evidence-bundle`, and manifest validators. `doctor --repair-codex-host` is write-capable and never receives this read-only classification.
- `python -m conductor_runtime bind-agent-profile`, `write-agent-profile`, `remember-agent-memory`, `forget-agent-memory`, `queue-team-task`, `reply-team-question`, `review-team-plan`, `export-saved-workflow`, `import-session-trace` including `--record-usage`, `import-usage`, `prepare-run`, `start-background-run`, `recover-run`, `retry-packet`, `pause-routine`, `resume-routine`, `update-routine-schedule`, `record-usage`, `score-benchmark-report`, `write-auto-topology-campaign`, `write-readonly-parity-campaign`, `write-evidence-bundle`, `write-run-manifest`, `write-routine-manifest`, and `write-schema` are classified as local writes, not read-only commands. `run-claude-readonly-task`, `run-claude-staged-task`, `run-codex-readonly-task`, `run-codex-staged-task`, `run-codex-staged-repair`, non-dry-run `run-auto-topology-arm`, and non-dry-run `run-readonly-parity-arm` are provider execution plus local write and enforce their own capability/approval contracts. A staged provider command with `--apply-verified-stage` is additionally classified as potentially destructive because accepted evidence may delete source files. Background and staged-repair validate/status/wait commands are read-only. `run-routine-now` remains conservatively classified because the referenced manifest may authorize network or destructive work.
- `validate-routine-manifest`, `validate-routine-controls`, `validate-routine-state`, `validate-routine-service-grant`, `validate-routine-service-state`, all provider/benchmark-quality validators, `list-routines`, `status-routine`, `status-routine-service`, `evidence-bundle`, and `validate-evidence-bundle` are classified as read-only. Routine-service install/update/uninstall commands remain mutating operations behind their dedicated capability and lifecycle approvals.

This is not a hidden executor. Generated workflows still require normal validation, runtime flags such as `--allow-agent`, `--allow-parallel`, or `--allow-writes`, and high-risk approvals when applicable.

## Public JSON Schemas

`schema` prints dependency-free Draft 2020-12 JSON Schemas for editor, CI, and integration tooling; `schema --list` is the authoritative current registry. Content-free provider journals use `codex-progress` (`conductor.codex_progress.v4`), with v3, v2, and v1 retained as `codex-progress-v3`, `codex-progress-v2`, and `codex-progress-v1`; direct terminal evidence uses current `codex-step-terminal` (`conductor.codex_step_terminal.v2`) with `codex-step-terminal-v1` retained; cacheable map completions use `agent-map-packet-terminal` (`conductor.agent_map_packet_terminal.v1`); provider lifecycle hooks receive `agent-lifecycle-hook-input` (`conductor.agent_lifecycle_hook_input.v1`); blocking stop acceptance uses private `agent-lifecycle-stop-gate` (`conductor.agent_lifecycle_stop_gate.v1`); matching direct-write isolation uses private `direct-workspace-transaction` (`conductor.direct_workspace_transaction.v1`); and injected provider context uses current private `agent-lifecycle-context` (`conductor.agent_lifecycle_context.v2`) receipts with `agent-lifecycle-context-v1` retained. Team defaults are `agent-team-state` (`conductor.agent_team_state.v6`) and `agent-team-turn` (`conductor.agent_team_turn.v2`); locally terminal turns use current `agent-team-turn-terminal` (`conductor.agent_team_turn_terminal.v2`) with `agent-team-turn-terminal-v1` retained, and completed provider turns use current `agent-team-turn-completion` (`conductor.agent_team_turn_completion.v2`) with `agent-team-turn-completion-v1` retained. Opt-in operator steering uses `agent-team-operator-inbox` (`conductor.agent_team_operator_inbox.v2`), teammate questions use `agent-team-operator-chat` (`conductor.agent_team_operator_chat.v1`), private teammate observation uses `agent-team-transcript` (`conductor.agent_team_transcript.v1`), and plan review uses current `agent-team-plan-approval` (`conductor.agent_team_plan_approval.v3`) with v2 and v1 retained. Interrupted turns use `agent-team-interruption` (`conductor.agent_team_interruption.v1`), team gates receive `agent-team-hook-input` (`conductor.agent_team_hook_input.v1`), and opt-in desktop delivery uses `desktop-notification` (`conductor.desktop_notification.v1`). Isolated write teams additionally emit `agent-team-merge-ledger` (`conductor.agent_team_merge_ledger.v1`), accepted `agent-team-merge-intent` (`conductor.agent_team_merge_intent.v1`), pending `agent-team-merge-transaction` (`conductor.agent_team_merge_transaction.v1`), and completed `agent-team-merge-recovery` (`conductor.agent_team_merge_recovery.v1`) evidence. Other retained team compatibility exports include `agent-team-state-v5` through `agent-team-state-v1`, `agent-team-operator-inbox-v1`, and `agent-team-turn-v1`. Legacy model-workflow receipt schemas likewise remain exported under versioned names. Schema names and artifact schema ids are both accepted:

```bash
python3 -m conductor_runtime schema --list --json
python3 -m conductor_runtime schema conductor.routine_manifest.v1
python3 -m conductor_runtime write-schema evidence-bundle "$HOME/.codex/conductor/generated/evidence-bundle.schema.json"
```

`schema` is read-only. `write-schema` refuses existing or symlinked destinations and is classified as a local write. These JSON Schemas are public integration aids; Conductor's Python validators remain authoritative for cross-field checks, command policy classification, count consistency, claim eligibility, no-follow path handling, redaction, and runtime behavior.

## Workflow Schema

Workflow files use JSON:

```json
{
  "schema": "conductor.workflow.v1",
  "name": "example",
  "description": "Describe the workflow.",
  "mode": "read_only",
  "risk": "medium",
  "max_workers": 4,
  "agent_effort": "high",
  "agent_max_tokens": 12000,
  "agent_map_max_total_tokens": 100000,
  "steps": [
    {
      "id": "list-files",
      "kind": "shell",
      "risk": "low",
      "writes": false,
      "command": ["rg", "--files"],
      "capture": "context/repo-files.txt"
    }
  ]
}
```

Supported step kinds:

- `write_artifact`: writes content under the run's `artifacts/` directory.
- `collect_results`: terminally consolidates one completed direct `codex_exec` or `agent_map` dependency into the workflow's declared `result_artifact` without a provider call.
- `manual_gate`: blocks unless `--approve <approval_id>` is supplied.
- `shell`: runs an argv-array command and captures redacted logs.
- `codex_exec`: runs `codex exec --json` with stdin prompt, captures the last message, and records normalized event and terminal measured usage metadata. Optional `effort` and `max_tokens` override workflow agent defaults.
- `agent_map`: runs bounded parallel `codex exec` packets over exactly one source: `items`, `items_file`, or `items_artifact`; successful packet outputs are cached inside the run artifacts for compatible retries. Optional `max_packets` groups multiple validated items into one canonical JSON-array scope; `effort`, `max_tokens`, and `max_total_tokens` control provider work.
- `agent_team`: runs a bounded coordinated team with profile-backed persistent sessions, a dependency task graph, deterministic parent-owned claims, next-turn messages, strict current/legacy turn/state envelopes, aggregate token authorization, explicit lead synthesis, optional bounded lead-authored task proposals, bounded operator-to-member tasks, and a private redacted operator console. Read-only is default; opt-in workspace-write uses external member copies and a strict ordered merge ledger.
- `agent_memory`: appends one bounded artifact to approved external profile memory for future runs only.

A result-contract workflow sets top-level `result_artifact` and ends with exactly one `collect_results` step whose `output` matches it. The source must be a direct dependency and must have completed rather than been skipped. Unstructured direct output becomes one JSON string; schema-backed direct output becomes its strict JSON value; map output becomes an ordered JSON array. Optional `filter_falsey` on a map drops only `false`, `null`, numeric zero, and the empty string, while empty arrays and objects remain truthy as in JavaScript. Collection uses no-follow bounded reads, rejects duplicate keys and non-finite values, enforces `output_limit_bytes` incrementally, writes canonical JSON, records source/output hashes and sizes, and rechecks source bytes plus result bytes on resume.

Shell captures default to combined stdout/stderr with labels. Set `capture_mode` to `stdout` or `stderr` when a later step needs clean line-oriented data.

Top-level `max_workers` and per-step `agent_map.max_workers` values are capped at 128. `agent_map.max_items` defaults to the workflow item cap and can be set up to 10,000 for explicit runtime sources; optional `max_packets` is 1 through 10,000 and each grouped packet is capped at 64 items. `plan-workflow` generated sources are capped separately at 1,000 items and default to 25 packets. Actual concurrency above 16 resolved packets requires `--approve high-scale-agent-map` in addition to `--allow-agent --allow-parallel`.

Top-level `agent_effort`, `agent_max_tokens`, and `agent_map_max_total_tokens` are optional defaults for Codex-backed steps. Effort is one of `low`, `medium`, `high`, `xhigh`, or `ultra`; token caps are strict integers from 100 through 1,000,000,000,000. These are experimental runtime-hard weighted rollout limits using non-cached input plus output at command-enforced 1.0 weights. They are not gross terminal-token or dollar limits. Existing workflows without these fields retain their prior unbounded command shape; deterministic/model planners and bundled workflows emit bounded defaults. Omitted-cap deterministic read-only direct planning uses 4,000/8,000/12,000/16,000/20,000 by effort; deterministic writes use 24,000. Every capped ordinary agent, map packet, team turn, and same-thread continuation treats the runtime's 50%-remaining reminder as a completion checkpoint. Serial direct prompt v4 applies the component-level shared-helper rule without adding a call or changing the cap. Native direct turns use `parent-integration-and-synthesis-v4`: child scope is one independent group, the parent immediately inspects a complementary group, unfinished child work stops at `floor(effective_cap / 2)` remaining, and no-tools synthesis starts at `min(6000, floor(effective_cap / 3))` remaining. That synthesis groups cross-method manifestations of one violated invariant into one finding, merges same-component call sites that bypass one required helper into one component-level root cause, merges candidates sharing one first faulty executable line or repair site, keeps one absent validation boundary together across clause types, moves secondary manifestations to evidence, and verifies the requested output contract once. Packet synthesis retains the general consolidation rule without the direct component clause. These are runtime-scheduled model checkpoints inside the unchanged shared hard cap, not per-session provider token reservations. The configured per-call value is stated as an upper bound because aggregate map/team allocation may lower the actual invocation cap; the emitted runtime reminder remains authoritative.

Top-level `agent_profiles` embeds strict reusable profiles; an agent step or team member selects one with `agent_profile`. Profile instructions, static context, and a bounded external memory snapshot enter the effective prompt; model/effort/tool policy enter the Codex command; and effective limits plus memory revision/hash/selector evidence enter state, traces, and dashboard metrics. The complete profile, context hashes, memory snapshot, and restricted command-policy digest enter the workflow fingerprint and `agent_map` cache key, so changed specialist knowledge or command authority cannot reuse stale packet output. Isolated profiles provide a bounded independent tool envelope; restricted profiles additionally route hook-visible Bash and patch calls through structured rules. The separate `agent_team` step supplies its own bounded shared board and mailbox; profile policy itself is not a universal native-tool allowlist.

Top-level `hooks` support local shell quality gates around run and step lifecycle events. See [Workflow Hooks](#workflow-hooks).

## Current Limits

Current limits:

- hidden, implicit, system-wide, or unrestricted daemon/process-manager installation; explicit background launches have no restart policy, while the optional routine service is one approval-gated, expiring, hash-bound launchd/systemd user unit,
- managed or recurring model-goal execution; detached model-goal verification is explicit, bounded, and read-only,
- cloud-task desktop notification delivery and real login/reboot delivery evidence; trustworthy local workflow, goal, wrapper, detached-worker, and routine/service terminal paths are covered,
- a production-grade workflow UI with browser-side approval granting, provider billing-cost capture, complete provider-native traces, arbitrary process management, or multi-user auth,
- effort-triggered or unapproved in-session execution, full LLM-authored JavaScript scripts, arbitrary script execution, or a broad external hook/plugin ecosystem; `auto` provides deterministic routing into explicitly approved foreground/detached execution and bounded per-iteration graph replacement,
- full JavaScript saved workflow `args` semantics; Conductor supports only safe `{{args.KEY}}` placeholder rendering and bounded static parallel callback expansion,
- universal equivalent Claude Code Ultracode outcome evidence; the homogeneous v0.82 implementation campaign supports product-profile broad superiority but remains cap-inequivalent, includes one both-failed incomparable row, lacks authoritative Codex dollar cost, and covers only five repeated task families,
- normalized cross-provider cost/token budgets; Claude dollar stops and Codex weighted rollout limits remain different mechanisms and cannot be treated as equivalent,
- live stress evidence for dozens/hundreds/1,000 actual agent executions; the 1,000-source-item/25-packet path is synthetic scheduler evidence only,
- cross-session/global agent result caching,
- universal provider-native interception or transactional acceptance for advisory direct writes; current local lifecycle hooks cover every actual process boundary with content-free metadata, inject bounded context before all fresh/resumed scopes, provide opt-in crash-safe blocking stop acceptance for locally durable results, and isolate matching direct writes until acceptance, while `agent_pre_tool` adds exact gate/receipt enforcement only for Codex paths currently covered by stable `PreToolUse`,
- provider-native text injection/interactive-session takeover or provider-owned split-pane sessions, recovery of discarded in-flight provider output, and live matched team-quality/interruption/plan-gate evidence; a strict private redacted multi-member terminal/browser display, bounded between-turn teammate questions, explicit named interruption plus same-session redirect, autonomous fixed-lead review, and cheaper explicit operator plan approval/rejection are available on current opt-in teams, while automatic team-hook retries remain separately opt-in, bounded, and require explicit reconciliation after hard process loss,
- distributed execution,
- package publishing to an external registry; local `.pyz`, Skill, and combined bundle artifacts are generated under `dist/`.

Those can be added, but the first layer keeps the safety model inspectable.
