---
name: boundary-review
description: Review public object and container boundaries for documented validation behavior.
---

# Boundary Review

Inspect every changed public object or container boundary before attribute, key, or index access. Verify documented
types, shapes, mutation rules, and exception classes, then group manifestations that share one missing validation
boundary into one finding.

Read `references/checklist.md` only when a concrete boundary audit needs the detailed checklist. Use
`assets/report-template.md` only when a structured report is requested. Run `scripts/verify-boundary.sh` only when
the current tool policy already permits shell execution and the operator's scope calls for the fixture check.
