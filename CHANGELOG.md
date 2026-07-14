# Changelog

Git history is the authoritative archive for releases before `0.172.0`.

## 0.173.1

- Hardened core workflow, state, goal, provider-stream, and staged-apply
  validation against malformed, rehashed, duplicated, and type-confused data.
- Made staged application lock-serialized and crash-recoverable, with exact
  evidence binding, rollback verification, and source-drift preservation.
- Closed approval, path-overlap, command-inference, process-tree timeout,
  terminal-redaction, and custom-receipt boundary gaps.
- Hardened the bundle installer against symlink roots and leaf reads, rejected
  overlapping destinations, and normalized release ZIP metadata.
- Fixed a parallel optional-runtime gate-summary race and expanded adversarial,
  CLI, installer, packaging, crash, and fault-injection coverage.

## 0.173.0

- Replaced the monolithic default runtime with a small model-first core.
- Kept direct execution as the default and made planning, goals, and bounded
  map-to-synthesis workflows explicit strategies.
- Moved legacy campaigns, evaluators, dashboards, team tooling, services, and
  cloud integrations into the separately packaged `conductor_extras` namespace.
- Kept state outside repositories and preserved strict workflow validation,
  permissions, budgets, resume binding, verification, and staged application.
- Reduced default package size, imports, startup latency, and simple-task
  receipt overhead; exact release measurements are recorded in the release
  report and reproducible with `tools/measure_runtime.py`.

## 0.172.0

- Added bounded map-to-single-agent synthesis to the static Claude saved-workflow
  compatibility layer.
- Reused bounded intermediate collection and dependency context with strict
  source/result receipts and resume drift detection.
- Added adversarial compiler and runtime tests plus one narrowly scoped matched
  comparison. The result did not establish universal Claude or Ultracode
  superiority.
