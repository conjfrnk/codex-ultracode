# 0.174.0 Verification

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
| Evidence schema | `conductor.verification_evidence.v1` |
| Runtime | `0.174.0` |
| Mode | `full` |
| Python | `3.14.6` (`CPython`, `darwin`, `arm64`) |
| Tools | Ruff `0.15.21`; mypy `2.3.0`; coverage `7.15.0`; jsonschema `4.26.0` |
| Warning strict | `true` |
| Core coverage floor | `80%` |
| Hash seeds | `0, 1, 42, random` |
| Release artifacts checked | `true` |
| Source SHA-256 | `5af98194f02e240ad6ed7c9a0d88e849e3cff8e190b7dac4ce9824c5cbd13eea` |
| Report-template SHA-256 | `c0d2bf6a8c3c46265f40134f788df4395cedcd26fcda3ede7fa0d9f20f60e1d0` |
| Checksum-manifest SHA-256 | `e037f893c16b97736235a65c76a5976a64d8c13187529f0c77844319ada87eef` |
| Repository tests | `1198` |
| Core-gate tests | `139` |
| Core archive bytes | `63079` |

### Passed checks

- `ruff`
- `mypy-core`
- `local-skill-audit`
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
| `codex-conductor-bundle.zip` | `db25c137f862af1157f4c9dc30e424a5f1e59edf6d36fadc74ff751219fa1f85` |
| `codex-conductor-marketplace.zip` | `f69fd420f1e4636bc2518338090b7463dd44078cb64c7da579735bc157c12b78` |
| `conductor-extras.pyz` | `c5a97abfc36ddab527176f8a134f9f48f21f268cf4ad0edbc931ee3ee5793641` |
| `conductor-runtime.pyz` | `60234a87d2a84db7af8ca4d0187033c41f05da17df1dc4afa36dfe528c8c1c77` |
| `release-manifest.json` | `d4141b091a4e1340a444be0b8d8ed225328738c86a7c35d4153f7be17e69fb34` |
| `skill.zip` | `7ab554be804c11793d5f36b2b0605758e57a3c058b5b902985d27b9639f3abea` |
<!-- END GENERATED VERIFICATION EVIDENCE -->

The verification gate does not make a live Codex or Claude provider call.
Checksums are not signatures, approved shell commands are not an
operating-system sandbox, and local verification cannot establish correctness
on platforms outside the configured CI matrix.
