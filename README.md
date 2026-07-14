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

The installer verifies every installed byte and writes only under `~/.codex`.
Replacing different bytes also requires
`--replace --approve conductor-update`.

## Use

Run one read-only model call:

```sh
python3 -m conductor_runtime auto --task "Inspect the parser" \
  --workspace . --allow-agent
```

Allow a staged edit with a read-only verifier:

```sh
python3 -m conductor_runtime auto --task "Fix the parser" \
  --workspace . --allow-agent --allow-writes
```

The source remains unchanged until verified evidence is applied:

```sh
python3 -m conductor_runtime apply /path/to/stages/work.json \
  --workspace . --allow-writes --approve verified-stage-apply
```

Use `--strategy goal` for bounded verifier-driven repair. Use
`--strategy workflow --allow-parallel` only for independent read-only work that
benefits from map-to-synthesis. See [`docs/runtime.md`](docs/runtime.md) for the
compact command reference.

## Safety

Agent, write, destructive, network, parallel, risk, and shell capabilities are
explicit. Runs bind workflow, workspace, policy, context, artifacts, and
receipts. Staged application verifies fingerprints and rolls back on failure.

Self-hashes are not signatures. Approved arbitrary shell commands are isolated
by working copy but are not an operating-system sandbox against deliberate
writes to external absolute paths.

## Development

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_core_runtime
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
uvx --offline ruff check .
```

The dependency-free default runtime is under 500 KiB. Historical and specialist
commands remain in the separate `conductor_extras` archive. Current verification
is summarized in [`RELEASE_REPORT.md`](RELEASE_REPORT.md).

This is an unofficial project and is not affiliated with or endorsed by OpenAI
or Anthropic.

## License

MIT. See `LICENSE`.
