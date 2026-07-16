#!/usr/bin/env python3
"""Run and validate exhaustive standard-library unittest shards."""

import argparse
import sys
import unittest
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_DIR = PROJECT_ROOT / "tests"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(TEST_DIR))

from runtime_test_shards import (  # noqa: E402
    runtime_tests_for,
    validate_runtime_test_shards,
)
from shard_metadata import (  # noqa: E402
    RUNTIME_SHARD_BY_TEST_SHARD,
    TEST_MODULE_SHARDS,
    TEST_SHARD_ORDER,
    validate_test_module_shards,
)


DISPLAY_NAMES = {name: name.replace("_", "-") for name in TEST_SHARD_ORDER}
SHARD_NAMES = {display: name for name, display in DISPLAY_NAMES.items()}


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _suite_for(shards):
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for shard in shards:
        for module in TEST_MODULE_SHARDS[shard]:
            suite.addTests(loader.loadTestsFromName(module))
        runtime_shard = RUNTIME_SHARD_BY_TEST_SHARD[shard]
        if runtime_shard is not None:
            suite.addTests(runtime_tests_for(runtime_shard))
    return suite


def _test_ids(suite):
    return [test.id() for test in _flatten(suite)]


def validate_shards():
    """Prove metadata coverage, uniqueness, and parity with standard discovery."""
    module_counts = validate_test_module_shards(TEST_DIR)
    runtime_counts = validate_runtime_test_shards()
    sharded_ids = _test_ids(_suite_for(TEST_SHARD_ORDER))
    discovered_ids = _test_ids(
        unittest.defaultTestLoader.discover(str(TEST_DIR), pattern="test_*.py")
    )
    id_counts = Counter(sharded_ids)
    duplicate_ids = sorted(test_id for test_id, count in id_counts.items() if count != 1)
    missing_ids = sorted(set(discovered_ids) - set(sharded_ids))
    unexpected_ids = sorted(set(sharded_ids) - set(discovered_ids))
    if (
        duplicate_ids
        or len(sharded_ids) != len(discovered_ids)
        or missing_ids
        or unexpected_ids
    ):
        detail = "duplicates=%r missing=%r unexpected=%r counts=%d/%d" % (
            duplicate_ids[:10],
            missing_ids[:10],
            unexpected_ids[:10],
            len(sharded_ids),
            len(discovered_ids),
        )
        raise RuntimeError("test shard discovery parity failed: " + detail)
    return module_counts, runtime_counts, len(sharded_ids)


def _selected_shards(values):
    if not values or values == ["all"]:
        return list(TEST_SHARD_ORDER)
    if "all" in values:
        raise ValueError("all cannot be combined with named shards")
    selected = []
    for value in values:
        key = SHARD_NAMES.get(value)
        if key is None:
            raise ValueError("unknown shard %s" % value)
        if key in selected:
            raise ValueError("duplicate shard %s" % value)
        selected.append(key)
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "shards",
        nargs="*",
        metavar="SHARD",
        help="named shard(s), or all (default: all)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="validate ownership and discovery parity without running tests",
    )
    parser.add_argument(
        "--list-tests",
        action="store_true",
        help="list selected test IDs without running them",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    try:
        selected = _selected_shards(args.shards)
        module_counts, runtime_counts, total = validate_shards()
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    if args.validate:
        print("validated %d unique tests across %d shards" % (total, len(TEST_SHARD_ORDER)))
        for shard in TEST_SHARD_ORDER:
            runtime_shard = RUNTIME_SHARD_BY_TEST_SHARD[shard]
            runtime_count = runtime_counts[runtime_shard] if runtime_shard is not None else 0
            print(
                "%s: %d modules, %d legacy runtime tests"
                % (DISPLAY_NAMES[shard], module_counts[shard], runtime_count)
            )
        return 0
    suite = _suite_for(selected)
    selected_ids = _test_ids(suite)
    if len(selected_ids) != len(set(selected_ids)):
        parser.error("selected shards contain duplicate test IDs")
    if args.list_tests:
        for test_id in selected_ids:
            print(test_id)
        return 0
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
