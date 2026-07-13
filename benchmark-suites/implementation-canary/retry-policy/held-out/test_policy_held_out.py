import unittest

from policy import normalize_policy
from service import should_retry


class RetryPolicyHeldOutTests(unittest.TestCase):
    def test_rejects_non_dict_input(self):
        for value in (None, [], "policy", 1):
            with self.subTest(value=value), self.assertRaises(TypeError):
                normalize_policy(value)

    def test_rejects_boolean_numeric_fields(self):
        cases = [
            {"timeout_ms": True, "retries": 0},
            {"timeout_ms": 100, "retries": False},
            {"version": 2, "timeout_seconds": False, "max_attempts": 1},
            {"version": 2, "timeout_seconds": 1, "max_attempts": True},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_policy(value)

    def test_rejects_unknown_and_cross_version_keys(self):
        cases = [
            {"timeout_ms": 100, "retries": 1, "unknown": 2},
            {"timeout_ms": 100, "max_attempts": 2},
            {"version": 2, "timeout_seconds": 1, "retries": 1},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_policy(value)

    def test_tag_cleanup_is_stable_and_non_mutating(self):
        raw = {
            "version": 2,
            "timeout_seconds": 0.25,
            "max_attempts": 1,
            "tags": ["", " alpha ", "beta", "alpha", "  "],
        }
        original_tags = list(raw["tags"])
        self.assertEqual(normalize_policy(raw)["tags"], ["alpha", "beta"])
        self.assertEqual(raw["tags"], original_tags)

    def test_rejects_malformed_tags(self):
        cases = [
            {"timeout_ms": 100, "retries": 0, "tags": ["not", "v1"]},
            {"version": 2, "timeout_seconds": 1, "max_attempts": 1, "tags": "not-v2"},
            {"version": 2, "timeout_seconds": 1, "max_attempts": 1, "tags": ["ok", 2]},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_policy(value)

    def test_v1_omitted_version_and_fractional_timeout(self):
        self.assertEqual(
            normalize_policy({"timeout_ms": 125.5, "retries": 0}),
            {
                "version": 2,
                "timeout_seconds": 0.1255,
                "max_attempts": 1,
                "tags": [],
            },
        )

    def test_retry_rejects_invalid_completed_attempts(self):
        policy = {"version": 2, "timeout_seconds": 1, "max_attempts": 2}
        for value in (-1, True, 1.5, "1"):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                should_retry(policy, value)


if __name__ == "__main__":
    unittest.main()
