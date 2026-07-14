# 0.173.1 Verification

The default runtime is the small model-first core. Specialist and historical
commands ship only in `conductor_extras`.

| Metric | Before | 0.173.1 |
| --- | ---: | ---: |
| Production Python files | 94 | 17 |
| Production code lines | 105,477 | 4,583 |
| Internal import cycles | 4 | 0 |
| Startup median | 115.346 ms | 23.737 ms |
| Direct plan-only median | 131.735 ms | 60.898 ms |
| Default zipapp | 895,962 bytes | 53,547 bytes |

Measurements used 20 local runs. The packaged runtime started in a 27.057 ms
median and completed a provider-free direct plan in an 87.931 ms median.

Verification for this release:

- 1,151 warning-strict repository tests passed.
- The 101-test core and installer matrix passed under four hash seeds.
- Mutation, process-tree, crash-recovery, staged-apply, installer, and
  parallel-summary fault cases passed.
- Default, extras, bundle, marketplace, and Skill archives reproduced
  byte-for-byte and passed archive-safety checks.
- Isolated and active installs passed first-install, idempotence, tamper
  conflict, approved repair, runtime, doctor, and Skill validation checks.
- The default archive imports no optional or development modules.

No Claude call was used for this release. Self-hashes are not signatures,
approved shell commands are not an operating-system sandbox, and these
measurements do not establish universal superiority over another model or tool.
