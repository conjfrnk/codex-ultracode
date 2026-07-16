"""Compatibility entry point for the sharded extras runtime test suite."""

try:
    from .runtime_test_shards import all_runtime_tests
    from .runtime_test_support import RuntimeWorkflowTests
    from .shard_metadata import validate_test_module_shards
except ImportError:  # ``unittest discover -s tests`` imports top-level modules.
    from runtime_test_shards import all_runtime_tests
    from runtime_test_support import RuntimeWorkflowTests
    from shard_metadata import validate_test_module_shards


__all__ = ("RuntimeWorkflowTests",)


validate_test_module_shards()


def load_tests(loader, standard_tests, pattern):
    del loader, standard_tests, pattern
    return all_runtime_tests()


if __name__ == "__main__":
    import unittest

    unittest.main()
