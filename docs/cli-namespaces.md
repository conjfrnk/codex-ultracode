# Conductor Extras command namespaces

`conductor-extras` is the command identity for the optional extras runtime.
Its preferred CLI is grouped by operating domain:

```text
conductor-extras workflow validate WORKFLOW.json
conductor-extras team list-inbox RUN_DIR STEP_ID
conductor-extras memory list
conductor-extras benchmark run SUITE.json
conductor-extras routine list
conductor-extras cloud status TASK_ID
```

Run `conductor-extras --help` to see the available groups, then
`conductor-extras GROUP --help` to list that group's commands. A grouped leaf
uses the same parser, validation, permissions, and dispatch handler as its old
flat form.

## Flat-command migration window

Every command that existed before namespaces remains available as a flat alias
through **2027-07-15**. Executable flat invocations emit a deprecation message
with their grouped replacement. Programmatic `main(argv)` calls stay quiet for
embedding compatibility. Flat aliases will not be removed before
**2027-07-16**.

For example:

```text
# Deprecated, but supported through 2027-07-15
conductor-extras list-agent-memory

# Preferred
conductor-extras memory list
```

The existing `benchmark SUITE.json` form is also retained. Because `benchmark`
is now both a namespace and a legacy command, use `benchmark run SUITE.json`
for the grouped form. If a suite path is literally the same as a benchmark
subcommand, prefix it with `./` when using the legacy form.

## Maintenance boundary

Extras is maintained for security, correctness, and compatibility, not new
feature growth. The exhaustive grouped registry therefore delegates to the
existing parser and handler for each command instead of duplicating or broadly
rewriting the legacy implementation. Registry tests fail closed if a flat
command is unassigned or assigned more than once. Domain modules should be
split only when a future product decision resumes feature development.
