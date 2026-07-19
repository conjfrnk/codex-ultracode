# 0.175.0 Verification

The default runtime is the small model-first core. Specialist and historical
commands ship only in `conductor_extras`.

The generated section is the authoritative release record. It is bound to the
repository source, this report's non-generated text, the exact verification
contract, and the external checksum manifest. Generate it only after
`python3 tools/verify.py --evidence VERIFICATION_EVIDENCE.json` passes, then
render it with `python3 tools/render_release_report.py`.

<!-- BEGIN GENERATED VERIFICATION EVIDENCE -->
## Generated verification evidence

The table below is rendered from `VERIFICATION_EVIDENCE.json` rather than maintained by hand.

| Field | Value |
| --- | --- |
| Evidence schema | `conductor.verification_evidence.v2` |
| Runtime | `0.175.0` |
| Codex CLI | `codex-cli 0.144.5` |
| Mode | `full` |
| Python | `3.14.6` (`CPython`, `darwin`, `arm64`) |
| Tools | Ruff `0.15.21`; mypy `2.3.0`; coverage `7.15.0`; jsonschema `4.26.0` |
| Warning strict | `true` |
| Core coverage floor | `80%` |
| Hash seeds | `0, 1, 42, random` |
| Release artifacts checked | `true` |
| Source SHA-256 | `00c718f943c52aa1e54a70cf234a384020de4bebc2fc66d73f7374345bf57fce` |
| Report-template SHA-256 | `4db9d2a4c51cf304a451ec12cb77c31acc3f0de11d48fcfa5e561009384e820e` |
| Checksum-manifest SHA-256 | `f1b2956ebb29d4ecb3e65adaf6a8c7e3a250c5be92a5a0b74f9b46f373e92389` |
| Repository tests | `1264` |
| Core-gate tests | `204` |
| Core archive bytes | `77215` |

### Passed checks

- `ruff`
- `mypy-core`
- `local-skill-audit`
- `codex-custom-agent-discovery`
- `test-shard-ownership`
- `documentation-sync`
- `workflow-contract-fixtures`
- `published-schema-runtime-parity`
- `warning-strict-core-tests`
- `core-branch-coverage`
- `warning-strict-full-suite`
- `core-hash-seed-matrix`
- `two-build-reproducibility`
- `tracked-artifact-freshness`
- `external-artifact-checksums`

### Release artifact SHA-256

| Artifact | SHA-256 |
| --- | --- |
| `codex-conductor-bundle.zip` | `c614a4d2e61ee779e593809bd70bb41ce71405305252efa2061a42901b640340` |
| `codex-conductor-marketplace.zip` | `26486756615d4d079d08aac98aeb9f4195f63252bd25f904c9aac8059aa22d55` |
| `conductor-extras.pyz` | `318871db23f812a132ee856c217f2c8dcfcdb9f22110a84947f789cc842dcaf5` |
<!-- END GENERATED VERIFICATION EVIDENCE -->

The verification gate does not make a live Codex or Claude provider call.
Checksums are not signatures, approved shell commands are not an
operating-system sandbox, and local verification cannot establish correctness
on platforms outside the configured CI matrix.
