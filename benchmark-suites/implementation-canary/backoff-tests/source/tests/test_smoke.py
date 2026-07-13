import unittest

from backoff import retry_delay


class BackoffSmokeTests(unittest.TestCase):
    def test_first_attempt_uses_base(self):
        self.assertEqual(retry_delay(0), 0.5)

    def test_default_growth_is_exponential(self):
        self.assertEqual(retry_delay(3), 4.0)


if __name__ == "__main__":
    unittest.main()
