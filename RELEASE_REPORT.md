# 0.173.0 Lightweight Runtime Report

This release replaces the monolithic default runtime with a small model-first
core. Specialist and historical behavior remains available only through the
separate `conductor_extras` package.

Measurements were produced with:

```bash
python3 tools/measure_runtime.py --archive dist/conductor-runtime.pyz --runs 20
```

The raw reports are outside the repository at:

```text
~/.codex/conductor/verification/lightweight-refactor/before.json
~/.codex/conductor/verification/lightweight-refactor/after.json
```

## Default Runtime

| Metric | Before | After | Change |
| --- | ---: | ---: | ---: |
| Production Python files | 94 | 17 | -81.9% |
| Production Python code lines | 105,477 | 3,858 | -96.3% |
| Production source bytes | 4,682,567 | 175,562 | -96.3% |
| Entry-reachable modules | 94 | 16 | -83.0% |
| Internal import edges | 706 | 62 | -91.2% |
| Import cycles | 4 | 0 | removed |
| Runtime modules imported by `--version` | 93 | 2 | -97.8% |
| Source startup median | 115.346 ms | 26.816 ms | -76.8% |
| Source startup p95 | 125.395 ms | 27.987 ms | -77.7% |
| Direct plan-only median | 131.735 ms | 66.175 ms | -49.8% |
| Direct plan-only p95 | 142.330 ms | 73.411 ms | -48.4% |
| Direct plan-only receipt | 2,716 bytes | 639 bytes | -76.5% |
| Default zipapp | 895,962 bytes | 47,409 bytes | -94.7% |

The packaged runtime starts in a 28.331 ms median and completes a direct
provider-free plan in an 85.576 ms median. Both measurements use 20 runs.

The requested 25,000-35,000-line target was a ceiling for a maintainable
default runtime, not a quota. The core is smaller because optional facilities
are physically absent from its package and import graph, not because comments,
tests, or generated files were counted differently.

## Package Boundary

The default zipapp has 18 entries and contains only the canonical CLI, shared
errors/redaction, and core runtime. Benchmark campaigns, evaluators,
dashboards, team consoles, services, cloud integrations, and Claude adapters
are absent.

`conductor_extras` is a separate 943,127-byte archive with 105,324 Python code
lines. It preserves compatibility but still contains four isolated import
cycles and oversized legacy modules. That is explicit optional-package debt,
not default-runtime complexity.

## Verification

- 25 focused core tests pass.
- All 1,081 warning-strict repository tests pass.
- Repository-wide Ruff, whitespace, local Skill audit, and official Skill
  validation pass.
- Default and extras archives are byte-for-byte reproducible.
- Isolated install, first install, idempotent rerun, runtime hash, doctor, and
  installed Skill validation pass.
- Active `0.173.0` install under `~/.codex` passes the same checks; Codex
  `0.144.3` reports ready.

No Claude call was used for this refactor. The evidence supports lower default
complexity and overhead plus preserved tested contracts. It does not establish
universal superiority over Claude Code, Ultracode, or any model/tool pairing.
