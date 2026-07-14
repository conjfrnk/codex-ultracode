# Changelog

Git history is the authoritative archive for releases before `0.172.0`.

## Unreleased

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
