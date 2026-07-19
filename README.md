# Codex Conductor

A dependency-free Python runtime and Codex skill for bounded coding work. The
default path makes one Codex call with no planner or daemon. State stays outside
the repository; write-capable work happens in an external staged copy and needs
independent verification before application.

## Install

Requires Python 3.12 or newer, a signed-in Codex CLI, and macOS or Linux.

Extract `dist/codex-conductor-bundle.zip`, inspect the plan, and install:

```sh
python3 install.py --dry-run
python3 install.py --allow-writes --approve conductor-install
```

The installer verifies every installed byte and writes only under the configured
Codex and Conductor homes, which both default under `~/.codex`. Replacing
different bytes also requires
`--replace --approve conductor-update`.

The bundle also installs five manifest-bound custom agent profiles without
replacing unrelated personal profiles. Start a new Codex session after
installation. See [Custom Codex agents](docs/custom-agents.md) for the profile
roles, routing defaults, and discovery check.

## Use

For normal repository work, ask Codex to “use codex-conductor” and describe the
bounded outcome. Use the CLI when you need reusable workflow JSON or direct
control over state, budgets, and permissions.

Run one read-only model call:

```sh
~/.codex/conductor/bin/conductor-runtime.pyz auto --task "Inspect the parser" \
  --workspace . --allow-agent
```

Run a staged edit with a read-only verifier:

```sh
~/.codex/conductor/bin/conductor-runtime.pyz auto --task "Fix the parser" \
  --workspace . --allow-agent --allow-writes
```

The source remains unchanged until verified evidence is applied:

```sh
~/.codex/conductor/bin/conductor-runtime.pyz apply /path/to/stages/work.json \
  --workspace . --allow-writes --approve verified-stage-apply
```

From a source checkout, replace the installed path with
`python3 -m conductor_runtime`. Goal mode supports bounded verifier-driven
repair; workflow mode is for independent read-only map-to-synthesis work.
Captured output can be inspected with `results list|get|search|outline`.

See the [runtime reference](docs/runtime.md) for commands and permissions, or
the [documentation index](docs/README.md) for optional facilities and
compatibility notes.

## Agent performance plugin

The separately installable `agent-performance` plugin supplies focused skills
for change contracts, debugging, test design, review, simplification,
completion proof, and matched comparisons. From the repository root:

```sh
codex plugin marketplace add .
codex plugin add agent-performance@codex-ultracode-local
```

Start a new Codex thread after installation. See the
[plugin README](https://github.com/conjfrnk/codex-ultracode/tree/main/plugins/agent-performance)
for behavior, validation, and the optional launch-time identity.

## Safety

Agent, write, destructive, network, parallel, risk, and shell capabilities are
explicit. Runs bind their workflow, workspace, policy, context, artifacts, and
receipts. Staged application verifies source fingerprints and rolls back on
failure.

Self-hashes are not signatures; authenticate releases with `dist/SHA256SUMS`
from a trusted channel. Approved shell commands run from an isolated working
copy, not an operating-system sandbox against deliberate writes to external
absolute paths.

## Development

Run the focused gate while editing:

```sh
python3 tools/verify.py --quick --skip-artifacts
```

The [release report](https://github.com/conjfrnk/codex-ultracode/blob/main/RELEASE_REPORT.md)
describes the pinned full gate, evidence refresh, and artifact checks.
Historical and specialist commands remain in the optional `conductor_extras`
archive and are in compatibility maintenance.

This is an unofficial project and is not affiliated with or endorsed by OpenAI
or Anthropic.

## License

MIT. See [LICENSE](LICENSE).
