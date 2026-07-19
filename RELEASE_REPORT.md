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
| Source SHA-256 | `1f4b2befa57ab95f6d995f0be840e20389e3f57eaf9db26597d096ac30c4592d` |
| Report-template SHA-256 | `4db9d2a4c51cf304a451ec12cb77c31acc3f0de11d48fcfa5e561009384e820e` |
| Checksum-manifest SHA-256 | `97fa28d22971b344911ccc4643241ef3a1cfe56723ec8d52c32ed46dde2c723c` |
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
| `codex-conductor-bundle.zip` | `b53a3002c1828bdfb207e67cc3fc977705ffd0eb0720a09973846c8b347e9e26` |
| `codex-conductor-marketplace.zip` | `5541b1dec495be406736b5309685c1d564470c1344d6f4e3058ff37fd9b3fcd9` |
| `conductor-extras.pyz` | `318871db23f812a132ee856c217f2c8dcfcdb9f22110a84947f789cc842dcaf5` |
<!-- END GENERATED VERIFICATION EVIDENCE -->

The verification gate does not make a live Codex or Claude provider call.
Checksums are not signatures, approved shell commands are not an
operating-system sandbox, and local verification cannot establish correctness
on platforms outside the configured CI matrix.
