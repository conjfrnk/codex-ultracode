import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from .runtime_test_shards import RUNTIME_TEST_SHARDS, SHARD_ORDER, validate_runtime_test_shards
    from .shard_metadata import (
        FULL_DISCOVERY_AGGREGATOR,
        TEST_MODULE_SHARDS,
        TEST_SHARD_ORDER,
        validate_test_module_shards,
    )
except ImportError:  # ``unittest discover -s tests`` imports top-level modules.
    from runtime_test_shards import RUNTIME_TEST_SHARDS, SHARD_ORDER, validate_runtime_test_shards
    from shard_metadata import (
        FULL_DISCOVERY_AGGREGATOR,
        TEST_MODULE_SHARDS,
        TEST_SHARD_ORDER,
        validate_test_module_shards,
    )


class TestShardMetadataTests(unittest.TestCase):
    def test_module_validation_rejects_unassigned_test_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assigned = {
                module
                for shard in TEST_SHARD_ORDER
                for module in TEST_MODULE_SHARDS[shard]
            }
            for module in assigned | {FULL_DISCOVERY_AGGREGATOR, "test_unassigned"}:
                (root / (module + ".py")).touch()

            with self.assertRaisesRegex(RuntimeError, r"missing=\['test_unassigned'\]"):
                validate_test_module_shards(root)

    def test_module_validation_rejects_duplicate_ownership(self):
        duplicate = TEST_MODULE_SHARDS["fast_core"][0]
        modified = dict(TEST_MODULE_SHARDS)
        modified["installer_package"] = modified["installer_package"] + (duplicate,)

        with patch.dict(TEST_MODULE_SHARDS, modified, clear=True):
            with self.assertRaisesRegex(RuntimeError, "duplicates=.*" + duplicate):
                validate_test_module_shards()

    def test_runtime_validation_rejects_duplicate_test_method(self):
        duplicate = RUNTIME_TEST_SHARDS[SHARD_ORDER[0]][0]
        modified = dict(RUNTIME_TEST_SHARDS)
        modified[SHARD_ORDER[1]] = modified[SHARD_ORDER[1]] + (duplicate,)

        with patch.dict(RUNTIME_TEST_SHARDS, modified, clear=True):
            with self.assertRaisesRegex(RuntimeError, "duplicates=.*" + duplicate):
                validate_runtime_test_shards()


if __name__ == "__main__":
    unittest.main()
