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
| Source SHA-256 | `35227d712a4c2753c356f8b56201380fc0751ae7ec8b909b479d11d30d096f49` |
| Report-template SHA-256 | `4db9d2a4c51cf304a451ec12cb77c31acc3f0de11d48fcfa5e561009384e820e` |
| Checksum-manifest SHA-256 | `f2e17b7c219a1176613ce61091e66538f72554252a71d7fec9d2982bed483b87` |
| Repository tests | `1267` |
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
| `codex-conductor-bundle.zip` | `1ebba11789369cae429e7acc90abb9e1a51d52308cb352112864339f017327b6` |
| `codex-conductor-marketplace.zip` | `fbb9f3a45d60f5486696772d839f4d3c20d40c18291b11cfd8ba80906f375779` |
| `conductor-extras.pyz` | `82e716ac2b274d572994ae9545f96af1d36fe30978ed64110cd37baa7c98ed6d` |
<!-- END GENERATED VERIFICATION EVIDENCE -->

The verification gate does not make a live Codex or Claude provider call.
Checksums are not signatures, approved shell commands are not an
operating-system sandbox, and local verification cannot establish correctness
on platforms outside the configured CI matrix.
