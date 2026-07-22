# Custom Codex agents

The release bundle installs five hash-bound profiles under `$CODEX_HOME/agents/`,
or `~/.codex/agents/` when `CODEX_HOME` is unset:

- `conductor-explorer`: read-only repository mapping;
- `conductor-docs-researcher`: read-only primary-source research;
- `conductor-worker`: bounded implementation within an assigned packet;
- `conductor-reviewer`: read-only correctness and risk review;
- `conductor-verifier`: read-only completion-evidence review.

Unrelated profiles remain untouched. Replacing a differing managed profile
requires `--replace --approve conductor-update`. Open a new Codex session after
installation.

## Personal routing defaults

Installation makes the profiles available but does not force delegation. To set
global routing defaults, add this policy to `$CODEX_HOME/AGENTS.md`, or
`~/.codex/AGENTS.md` when `CODEX_HOME` is unset:

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

For the quality-first profile, set the model and capacity ceiling in the
corresponding `config.toml`:

```toml
model = "gpt-5.6-sol"
model_reasoning_effort = "ultra"
service_tier = "priority"

[agents]
max_threads = 16
max_depth = 2
```

Depth two permits one independent supporting delegation. These settings are not
evidence of a quality improvement; that requires the matched protocol in
[`agent-routing-benchmark.md`](agent-routing-benchmark.md).

## Verify installation and discovery

Validate the source profiles and compare the installed copies:

```sh
python3 -B tools/check_codex_agents.py --installed
```

Also prove that the real Codex CLI discovers the profiles:

```sh
python3 -B tools/check_codex_agents.py --installed --codex "$(command -v codex)"
```

The probe uses a temporary `CODEX_HOME` and loopback-only synthetic provider. It
does not perform model inference, require login, or use a real API credential.
The canonical release gate runs the same discovery check.
