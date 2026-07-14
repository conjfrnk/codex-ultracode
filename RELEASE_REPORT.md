# 0.173.1 Lightweight Runtime Report

This patch release hardens the small model-first core introduced in `0.173.0`.
Specialist and historical behavior remains available only through the separate
`conductor_extras` package.

Measurements were produced with:

```bash
python3 tools/measure_runtime.py --archive dist/conductor-runtime.pyz --runs 20
```

The raw reports are outside the repository:

```text
~/.codex/conductor/verification/lightweight-refactor/before.json
~/.codex/conductor/verification/lightweight-refactor-0.173.1/after.json
```

## Default Runtime

| Metric | Before | 0.173.1 | Change |
| --- | ---: | ---: | ---: |
| Production Python files | 94 | 17 | -81.9% |
| Production Python code lines | 105,477 | 4,583 | -95.7% |
| Production source bytes | 4,682,567 | 208,973 | -95.5% |
| Entry-reachable modules | 94 | 16 | -83.0% |
| Internal import edges | 706 | 62 | -91.2% |
| Import cycles | 4 | 0 | removed |
| Runtime modules imported by `--version` | 93 | 2 | -97.8% |
| Source startup median | 115.346 ms | 23.737 ms | -79.4% |
| Source startup p95 | 125.395 ms | 25.425 ms | -79.7% |
| Direct plan-only median | 131.735 ms | 60.898 ms | -53.8% |
| Direct plan-only p95 | 142.330 ms | 64.740 ms | -54.5% |
| Direct plan-only receipt | 2,716 bytes | 639 bytes | -76.5% |
| Default zipapp | 895,962 bytes | 53,547 bytes | -94.0% |

The packaged runtime starts in a 27.057 ms median and completes a provider-free
direct plan in an 87.931 ms median. Both measurements use 20 runs. The default
archive has 18 entries and contains 209,055 uncompressed bytes.

The requested 25,000-35,000-line target was a ceiling, not a quota. The core is
smaller because optional facilities are absent from its package and import
graph, not because metrics exclude production code.

## Package Boundary

The default archive contains only the canonical CLI, shared errors/redaction,
and core runtime. Importing it loads no `conductor_extras` or `tools` modules.
All 181 optional commands build from source and from the packaged extras
archive.

`conductor_extras` is a separate 949,328-byte archive with 105,334 Python code
lines. It preserves compatibility but retains four import cycles and oversized
legacy modules. That is explicit optional-package debt, not default-runtime
complexity.

## Hardening Evidence

- Strict workflow, state, goal, provider-stream, evidence, snapshot, and
  receipt shapes reject extra, missing, duplicate, malformed, and type-confused
  values even when an attacker recomputes self-hashes.
- Staged application is lock-serialized, binds the exact run artifact, verifies
  backup and rollback fingerprints, recovers deterministic interrupted merges,
  and preserves arbitrary source drift.
- Process timeouts terminate surviving process-group descendants; provider
  retries preserve prior output and require complete measured usage.
- Permission gates cover inferred Git mutations, destructive and network
  commands, custom receipts, destination overlap, and approved-shell isolation.
- Installer reads use descriptor-based no-follow checks, configured symlink
  roots and overlapping destinations are rejected, and ZIP metadata is fixed
  for reproducible builds.
- Seeded mutation runs covered 40,000 workflow/verdict/stream inputs and 20,000
  state/stage/goal inputs with no uncaught exception after validation fixes.
- Real `os._exit` fault injection covers partial merge, post-merge/pre-receipt,
  rollback, retry, concurrent apply, and tampered-backup recovery paths.

## Verification

- All 1,151 warning-strict repository tests pass.
- The 101-test core and installer matrix passes under four independent hash
  seeds; 93 of those tests exercise the core runtime and CLI directly.
- All 20 native-tool hook tests pass; the parallel summary regression passes
  100 consecutive forced iterations.
- Core line coverage is 82.1%-88.3% across substantive modules; the canonical
  CLI is 80.7% covered. Coverage was measured with the standard-library trace
  tool because no third-party coverage package is installed.
- Ruff, compilation, whitespace, local skill audits, and the official skill
  validator pass.
- Default, extras, bundle, marketplace, and skill archives are byte-for-byte
  reproducible, path-safe, integrity-checked, and free of bytecode leakage.
- Isolated install tests cover dry-run, denied writes, first install,
  idempotence, tamper conflict, approval-gated replacement, rollback, symlink
  rejection, runtime execution, doctor, and skill-tree integrity.
- The active `0.173.1` install under `~/.codex` matches the release hashes;
  Codex `0.144.3` reports ready.

No Claude call was used for this release. Self-hashes are not signatures against
an attacker who can rewrite both data and hashes. An explicitly approved
arbitrary shell command is isolated by working copy but is not an OS sandbox
against deliberate writes to absolute external paths. The evidence supports a
smaller, faster, more fault-tolerant default runtime; it does not establish
universal superiority over Claude Code, Ultracode, or any model/tool pairing.
