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
| Source SHA-256 | `129325a3c3374beee9665f03fc0a2d444fbe93b0f3310d9f1d99355f0131b77c` |
| Report-template SHA-256 | `c0d2bf6a8c3c46265f40134f788df4395cedcd26fcda3ede7fa0d9f20f60e1d0` |
| Checksum-manifest SHA-256 | `c0675da4d7f12f6deede6d1b3b7c9a24f01cc2a861bc3cd07d514ab2fa8c7138` |
| Repository tests | `1200` |
| Core-gate tests | `140` |
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
| `codex-conductor-bundle.zip` | `8924e8974e742680c44828fbc5b1df4d3d5fa9c3108e45b1c0e5a12769530a99` |
| `codex-conductor-marketplace.zip` | `352d1162714a5b38b7c2e3a66856e2244902626e46680f9d12295259ad0abd17` |
| `conductor-extras.pyz` | `0ec227fad7e0479bf5714cdc8dd6687fd38a0ec29b448adf14ec0deb9a9340fd` |
| `conductor-runtime.pyz` | `407f46f0e239fdad09d2ae38d07ac254c60b7c818ea6583f9a68650fc8e23feb` |
| `release-manifest.json` | `cc1a33f0a1a6dce0344669b81bfb11366a72a77893a0b90f76a5bae0f3f42050` |
| `skill.zip` | `7ab554be804c11793d5f36b2b0605758e57a3c058b5b902985d27b9639f3abea` |
<!-- END GENERATED VERIFICATION EVIDENCE -->

The verification gate does not make a live Codex or Claude provider call.
Checksums are not signatures, approved shell commands are not an
operating-system sandbox, and local verification cannot establish correctness
on platforms outside the configured CI matrix.
