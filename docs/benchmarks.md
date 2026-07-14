# Benchmarks

Benchmark and comparison tooling is optional development infrastructure. It is
excluded from the default runtime and available through `conductor_extras`.

```sh
python3 dist/conductor-extras.pyz --help
```

The repository retains deterministic fixtures for provider runs,
implementation canaries, quality scoring, manifests, and pair comparison.
Generating or validating a fixture does not launch a provider.

Live comparisons must:

- bind the exact source, task, runtime, model, effort, permissions, and limits;
- keep provider evidence and independent quality evidence separate;
- preserve failed, timed-out, and over-budget outcomes;
- avoid automatic paid retries;
- reject comparisons with incomplete or incompatible provenance.

Claude adapters are pinned to Sonnet with Ultracode effort and reject Opus. A
budget limit cannot stop an already in-flight provider response from
overshooting, so overshoot is recorded as failure rather than success.

Passing benchmark fixtures proves only that the local evidence pipeline is
internally consistent. It does not prove parity or superiority.
