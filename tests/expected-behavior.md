# Expected Behavior

This file summarizes the invariants exercised by the provider-free test suite.
Runtime code and executable tests are authoritative.

## Default Routing

- `auto` uses one direct Codex call by default and makes no planning call.
- Read-only work does not request write capability.
- Write-capable work runs in an external staged copy and requires a downstream
  verifier.
- Parallel work is explicit, bounded, read-only, collected once, and
  synthesized once.
- Goal mode retries only verifier rejection; it does not retry provider,
  transport, policy, or launch failures.
- Optional extras are not imported or initialized by the default runtime.

## Permissions And Security

- Agent, write, destructive, network, parallel, high-risk, and shell
  capabilities fail closed without their required flags or approvals.
- Approval values are never persisted.
- Workflow, provider-stream, state, goal, evidence, snapshot, and receipt inputs
  use strict bounded schemas.
- State stays outside the workspace and is bound to workflow, workspace, policy,
  context, artifacts, and generation.
- File reads reject symlinks and non-regular files where integrity matters;
  writes use bounded atomic replacement.
- Model output, dependency context, map items, task text, and prior feedback are
  framed as untrusted data.
- Secrets and terminal control characters are redacted before public output.
- Approved arbitrary shell commands run in an isolated working copy, but are
  not an operating-system sandbox against external absolute paths.

## Recovery

- Resume requires the original workflow, workspace, permissions, approvals, and
  iteration context.
- Completed steps and map packets are reused only when their recorded artifacts
  and identities still verify.
- Unknown provider outcomes are not replayed automatically.
- Timeouts terminate the process group, including descendants that retain pipes.
- Staged application is locked, transactional, idempotent, and bound to
  independently verified evidence.
- Interrupted application verifies its backup, rolls back partial changes, and
  rejects unrelated source drift.

## Evidence

- Receipts are deterministic, bounded, redacted, and hash-bound to the evidence
  they summarize.
- Token and cost values are reported only when the provider supplied compatible
  measured fields.
- Self-hashes detect accidental or uncoordinated mutation; they are not
  signatures against an attacker who can rewrite both content and hashes.
- Benchmark fixtures and validators never imply parity or superiority by
  themselves.
- Cross-provider comparisons require matched source, task, model, effort,
  permissions, limits, and independent quality evidence.

## Optional Facilities

Background runs, routines, dashboards, cloud adapters, team coordination,
native tool hooks, saved-workflow compatibility, and provider comparisons live
in `conductor_extras`. When selected, they retain explicit permissions, bounded
state, deterministic evidence, recovery checks, and focused tests. When
omitted, they add no default-runtime imports, services, or provider calls.
