# Changelog

Git history is the archive for releases before `0.173.0`.

## 0.175.0

- Added the installable `agent-performance` plugin, its validation suite, and
  matched-comparison records and reducers.
- Added bounded, redacted process-output capture with opaque retrieval and
  integrity-checked failure handoffs.
- Added five manifest-bound Codex agent profiles, offline discovery proof, and
  optional launch-time identity support.
- Reduced the release surface to the install bundle, local marketplace,
  optional extras, and checksum manifest.

## 0.174.0

- Closed shell-approval, environment-secret, concurrent-apply, staged-resume,
  stage-quota, and lifecycle stop-gate defects found in the repository audit.
- Split the dependency-free core workflow contract into
  `conductor.core.workflow.v1`; added schema, template, migration, and validated
  core/extras example surfaces.
- Made result exports no-clobber by default and clarified direct, goal, and
  planned execution contracts.
- Added grouped extras namespaces with a one-year flat-alias migration window.
- Added pinned CI, mypy, branch coverage, sharded tests, reproducible tracked
  artifact verification, external checksums, and machine-readable release
  evidence.

## 0.173.1

- Hardened validation, staged-apply recovery, process cleanup, installer
  boundaries, and terminal redaction.
- Added adversarial, crash, installer, CLI, packaging, and concurrency coverage.
- Made every release archive reproducible.

## 0.173.0

- Introduced the small model-first runtime and canonical CLI.
- Moved specialist and historical behavior into the optional extras package.
- Kept runtime state outside repositories and verified writes before explicit
  staged application.
