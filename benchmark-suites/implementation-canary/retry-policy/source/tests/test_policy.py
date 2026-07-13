import unittest

from policy import normalize_policy
from service import should_retry


class RetryPolicyTests(unittest.TestCase):
    def test_migrates_v1_to_canonical_v2(self):
        self.assertEqual(
            normalize_policy({"timeout_ms": 2500, "retries": 2, "tags": "api, urgent,api"}),
            {
                "version": 2,
                "timeout_seconds": 2.5,
                "max_attempts": 3,
                "tags": ["api", "urgent"],
            },
        )

    def test_normalizes_v2_without_mutating_input(self):
        raw = {
            "version": 2,
            "timeout_seconds": 1,
            "max_attempts": 2,
            "tags": [" jobs ", "jobs", "nightly"],
        }
        before = {**raw, "tags": list(raw["tags"])}
        self.assertEqual(
            normalize_policy(raw),
            {
                "version": 2,
                "timeout_seconds": 1.0,
                "max_attempts": 2,
                "tags": ["jobs", "nightly"],
            },
        )
        self.assertEqual(raw, before)

    def test_retry_boundary_uses_total_attempts(self):
        policy = {"timeout_ms": 100, "retries": 2}
        self.assertTrue(should_retry(policy, 1))
        self.assertTrue(should_retry(policy, 2))
        self.assertFalse(should_retry(policy, 3))

    def test_rejects_invalid_or_mixed_policies(self):
        cases = [
            {"version": 3, "timeout_seconds": 1, "max_attempts": 1},
            {"version": 2, "timeout_ms": 100, "max_attempts": 1},
            {"timeout_ms": 0, "retries": 0},
            {"timeout_ms": 100, "retries": -1},
            {"version": 2, "timeout_seconds": 1, "max_attempts": 0},
            {"version": 2, "timeout_seconds": 1, "max_attempts": 1, "extra": True},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_policy(value)


if __name__ == "__main__":
    unittest.main()
