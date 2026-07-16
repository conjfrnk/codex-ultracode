# Workflow examples

- `core/` contains dependency-free workflows for `conductor-runtime` and uses
  the `conductor.core.workflow.v1` schema.
- `extras/` contains optional workflows for `conductor-extras` and uses the
  legacy extras dialect, `conductor.workflow.v1`.

The JSON files at this directory's root are deprecated compatibility copies of
the extras examples. They remain for one migration cycle; new links should use
`extras/`.

Validate the two dialects with their advertised runtimes:

```sh
python3 -m conductor_runtime validate conductor-workflows/core
python3 -m conductor_extras workflow validate conductor-workflows/extras
```
