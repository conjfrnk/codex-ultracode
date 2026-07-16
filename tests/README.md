# Test ownership and shards

Every discovered `test_*.py` module has exactly one owner in
`shard_metadata.py`. The legacy extras test methods have exhaustive class-level
ownership in `runtime_test_shards.py`; the small `test_runtime.py` loader keeps
their historical unittest identifiers stable.

Validate ownership and discovery parity without running tests:

```sh
python3 -W error tools/run_test_shard.py --validate
```

Run one focused shard:

```sh
python3 -W error tools/run_test_shard.py fast-core
python3 -W error tools/run_test_shard.py installer-package
python3 -W error tools/run_test_shard.py extras-runtime
python3 -W error tools/run_test_shard.py background-routine
python3 -W error tools/run_test_shard.py team
python3 -W error tools/run_test_shard.py benchmark
python3 -W error tools/run_test_shard.py ui-server
```

`python3 -W error -m unittest discover -s tests -q` remains the authoritative
full-suite path. Adding or renaming a test module or legacy method must update
the corresponding ownership manifest; validation fails closed on missing or
duplicate assignments.
