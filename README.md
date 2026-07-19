# Codex Conductor

A small, standard-library workflow runtime and Codex skill for bounded coding
work.

The default path makes one Codex call with no planner or daemon. State stays
outside the repository. Writes happen in an external staged copy and require
independent verification before application.

## Requirements

- Python 3.12 or newer
- Codex CLI installed and signed in
- macOS or Linux

## Install

Extract `dist/codex-conductor-bundle.zip`, inspect the plan, and install:

```sh
python3 install.py --dry-run
python3 install.py --allow-writes --approve conductor-install
```

The release directory contains only the install bundle, local marketplace,
optional extras archive, and `SHA256SUMS`. The core runtime and its release
manifest are private build intermediates retained inside the install bundle.

The installer verifies every installed byte and writes only under `~/.codex`.
Replacing different bytes also requires
`--replace --approve conductor-update`.

The bundle installs five manifest-bound custom agent profiles under
`~/.codex/agents/` without replacing unrelated personal profiles. Start a new
Codex session after installation so `conductor-explorer`, `conductor-worker`,
`conductor-reviewer`, `conductor-verifier`, and `conductor-docs-researcher` are
discovered. See [`docs/custom-agents.md`](docs/custom-agents.md) for global
routing defaults and an offline discovery check that uses no model inference.

The installer prints the exact runtime path and a copy-pasteable `doctor`
command. With the default state directory it is:

```sh
~/.codex/conductor/bin/conductor-runtime.pyz doctor
```

## Use the Skill

For normal repository work, ask Codex to “use codex-conductor” and describe the
bounded outcome. The Skill selects the smallest useful execution shape and
keeps permissions explicit. Use the low-level CLI when you need a reusable JSON
workflow or direct control over state, budgets, and approvals.

## Companion Performance Skills

This repository keeps reusable coding and evaluation methods separate from the
Conductor runtime. Conductor remains the execution and safety envelope under
[`codex-conductor/`](codex-conductor/); the independently installable
[`agent-performance`](plugins/agent-performance/) plugin supplies change contracts,
evidence-led debugging, behavioral testing, correctness review, safe simplification,
completion proof, and matched-comparison skills with validated controller-side reduction.

Install the companion plugin from a source checkout:

```sh
codex plugin marketplace add .
codex plugin add agent-performance@codex-ultracode-local
```

The repo-local catalog is [`.agents/plugins/marketplace.json`](.agents/plugins/marketplace.json).
Start a new Codex thread after installing so the new skills are discovered.
For a launch-time identity above the Codex welcome panel, source the zsh launcher
from this checkout in `~/.zshrc`:

```zsh
[[ -r /path/to/codex-ultracode/plugins/agent-performance/scripts/codex-ultracode.zsh ]] && \
  source /path/to/codex-ultracode/plugins/agent-performance/scripts/codex-ultracode.zsh
```

Bare `codex` launches then announce “Codex with Ultracode” before the TUI starts.
Argument-bearing commands keep their normal output. The plugin's trusted
`SessionStart` hook remains a fallback for launches without the zsh integration;
Codex runs that hook when the first prompt begins, not while rendering the idle
welcome panel. Use `/hooks` to review and trust the fallback command hook.

## Use

Run one read-only model call:

```sh
~/.codex/conductor/bin/conductor-runtime.pyz auto --task "Inspect the parser" \
  --workspace . --allow-agent
```

Allow a staged edit with a read-only verifier:

```sh
~/.codex/conductor/bin/conductor-runtime.pyz auto --task "Fix the parser" \
  --workspace . --allow-agent --allow-writes
```

The source remains unchanged until verified evidence is applied:

```sh
~/.codex/conductor/bin/conductor-runtime.pyz apply /path/to/stages/work.json \
  --workspace . --allow-writes --approve verified-stage-apply
```

From a source checkout, replace the installed path in these examples with
`python3 -m conductor_runtime`. An explicit `--output PATH` may export a result
inside the workspace; it never replaces an existing file unless
`--replace-output` is also supplied.

Use `--strategy goal` for bounded verifier-driven repair. Use
`--strategy workflow --allow-parallel` only for independent read-only work that
benefits from map-to-synthesis. See [`docs/runtime.md`](docs/runtime.md) for the
compact command reference and [`docs/README.md`](docs/README.md) for the docs
index.

When a shell or Codex stream exceeds its inline limit, Conductor keeps the
prefix bounded and stores a redacted, hash-bound overflow result outside the
workspace. `conductor-runtime results list|get|search|outline` retrieves cited
line ranges without exposing a host path or treating a truncated Codex control
stream as successful evidence. Lazy capture, bounded fan-out reservations,
serialized redaction, and non-mutating inspection keep diagnostics recoverable
without letting optional capture or result-store I/O faults overturn successful
shell work. Integrity violations still fail closed, and a core step fails when
an escaped descendant prevents either output pipe from closing cleanly.

## Safety

Agent, write, destructive, network, parallel, risk, and shell capabilities are
explicit. Runs bind workflow, workspace, policy, context, artifacts, and
receipts. Shell approvals bind the executable's canonical identity, content
hash, and exact arguments; workspace executables launch from the isolated copy.
Staged application verifies fingerprints and rolls back on failure.

Self-hashes are not signatures. Releases include a separate `dist/SHA256SUMS`
covering every top-level artifact; obtain it through a trusted repository or
release channel before using it for authenticity. Approved arbitrary shell
commands are isolated by working copy but are not an operating-system sandbox
against deliberate writes to external absolute paths.

## Development

```sh
uvx ruff==0.15.21 --version  # one-time tool-cache bootstrap
uvx mypy==2.3.0 --version
uvx coverage==7.15.0 --version
uv run --no-project --with jsonschema==4.26.0 python -c \
  "import importlib.metadata as m; print(m.version('jsonschema'))"
python3 tools/verify.py --quick --skip-artifacts  # focused edit-time gate
python3 tools/verify.py --evidence VERIFICATION_EVIDENCE.json --replace-evidence
python3 tools/render_release_report.py
python3 tools/verify.py --quick  # check tracked artifacts, evidence, and report
```

The full gate runs warning-strict tests, a four-hash-seed core matrix, the local
Skill audit, real-Codex custom-agent discovery through a loopback provider,
pinned Ruff and mypy checks, an 80% branch-aware core coverage ratchet, two clean
reproducible builds, and a byte comparison against tracked `dist`. The evidence
form writes only after every requested check passes; the renderer updates the
marked machine-generated section of `RELEASE_REPORT.md`.

The dependency-free default runtime is under 500 KiB. Historical and specialist
commands remain in the separate `conductor_extras` archive, which is maintained
for security, correctness, and compatibility rather than new feature growth.
Its preferred grouped command surface is documented in
[`docs/cli-namespaces.md`](docs/cli-namespaces.md). In the source repository,
current verification is summarized in `RELEASE_REPORT.md`.

This is an unofficial project and is not affiliated with or endorsed by OpenAI
or Anthropic.

## License

MIT. See `LICENSE`.
