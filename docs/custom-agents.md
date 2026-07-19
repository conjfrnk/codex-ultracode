# Custom Codex agents

The release bundle installs five hash-bound profiles as individual files under
`$CODEX_HOME/agents/` when `CODEX_HOME` is set, otherwise `~/.codex/agents/`:

- `conductor-explorer`: read-only repository mapping;
- `conductor-docs-researcher`: read-only primary-source research;
- `conductor-worker`: bounded implementation within an assigned packet;
- `conductor-reviewer`: read-only correctness and risk review;
- `conductor-verifier`: read-only completion-evidence review.

The installer never replaces the whole `agents/` directory. Unrelated personal
profiles remain untouched. A differing managed profile requires both `--replace`
and `--approve conductor-update`; missing managed profiles can be repaired without
replacement approval. Open a new Codex session after installation.

## Personal routing defaults

Installed profiles become available to subagent workflows, but installation does
not force every task to delegate. Put a compact policy in `$CODEX_HOME/AGENTS.md`
when you want specialist routing to apply across repositories; the default path
when `CODEX_HOME` is unset is `~/.codex/AGENTS.md`:

```md
# Personal Codex defaults

Use installed specialist skills and custom agents when they materially improve
correctness, coverage, or latency. Keep simple questions and tiny edits direct.

For non-trivial repository work:

- Use conductor-explorer for read-only mapping when scope or ownership is unclear.
- Use conductor-docs-researcher for current dependency or API documentation.
- Use conductor-worker only for bounded, non-overlapping implementation packets.
- Use conductor-reviewer after consequential changes or for risk-focused review.
- Use conductor-verifier before claiming consequential work is complete or fixed.
- Parallelize independent work; do not duplicate investigation or edit ownership.
- The primary agent owns scope, integration, final decisions, and final verification.
- Nested delegation is allowed only for a clearly independent second-level task.
- Never claim an agent ran unless a child thread was actually created.
```

Add the elevated capacity settings to `$CODEX_HOME/config.toml` (default
`~/.codex/config.toml`). Capacity remains a ceiling, not a target:

```toml
[agents]
max_threads = 16
max_depth = 2
```

Depth two lets a bounded specialist delegate one independent supporting task.
The routing policy prevents recursive fan-out on work that does not benefit from
it. See [`agent-routing-benchmark.md`](agent-routing-benchmark.md) for the frozen
comparison needed before making a broader quality claim.

## Verify installation and discovery

Validate the source profiles and compare them with the current installation:

```sh
python3 -B tools/check_codex_agents.py --installed
```

Run the real Codex CLI against a temporary loopback-only provider and inspect its
advertised `spawn_agent` schema:

```sh
python3 -B tools/check_codex_agents.py --installed --codex "$(command -v codex)"
```

The discovery probe creates a temporary `CODEX_HOME`, configures model-provider
traffic for `127.0.0.1`, captures one synthetic request, performs no model
inference, requires no login, and uses no real API credential. With `--installed`,
success proves that the active profile bytes match the source profiles and that
the installed Codex binary advertises an identical temporary copy as selectable
agent types. The probe is not an operating-system network sandbox; run it inside
a network-denied environment if you also need proof that the process cannot open
any unrelated outbound socket.

The canonical `tools/verify.py` release gate requires a discoverable Codex CLI
and runs the same loopback schema check. Unit-test discovery may skip when Codex
is unavailable, but release evidence cannot.

For a manual end-to-end spawn check, start Codex in a temporary repository, ask it
to spawn exactly one `conductor-explorer`, and use `/agent` to inspect the child
thread. This live check is optional because model routing is nondeterministic and
consumes account resources.
