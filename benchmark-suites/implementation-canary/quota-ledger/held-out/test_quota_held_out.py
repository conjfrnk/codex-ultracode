import unittest

from quota import QuotaLedger, Reservation, parse_reservation, reserve_quota


class QuotaHeldOutTests(unittest.TestCase):
    def test_parser_rejects_non_dict_input(self):
        for value in (None, [], "request", 3):
            with self.subTest(value=value), self.assertRaises(TypeError):
                parse_reservation(value)

    def test_parser_rejects_missing_unknown_and_empty_fields(self):
        cases = [
            {"tenant": "acme", "request_id": "r"},
            {"tenant": "acme", "request_id": "r", "units": 1, "extra": True},
            {"tenant": " ", "request_id": "r", "units": 1},
            {"tenant": "acme", "request_id": " ", "units": 1},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_reservation(value)

    def test_parser_rejects_wrong_value_types_and_ranges(self):
        cases = [
            {"tenant": 3, "request_id": "r", "units": 1},
            {"tenant": "acme", "request_id": 3, "units": 1},
            {"tenant": "acme", "request_id": "r", "units": True},
            {"tenant": "acme", "request_id": "r", "units": 0},
            {"tenant": "acme", "request_id": "r", "units": 1.5},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                parse_reservation(value)

    def test_request_id_case_is_preserved_for_idempotency(self):
        ledger = QuotaLedger({"acme": 4})
        self.assertTrue(reserve_quota({"tenant": "ACME", "request_id": "Req", "units": 2}, ledger))
        self.assertTrue(reserve_quota({"tenant": "acme", "request_id": "req", "units": 2}, ledger))
        self.assertEqual(ledger.usage("acme"), 4)

    def test_conflicting_idempotency_key_raises(self):
        ledger = QuotaLedger({"acme": 5})
        self.assertTrue(ledger.reserve(Reservation("acme", "same", 2)))
        with self.assertRaises(ValueError):
            ledger.reserve(Reservation("acme", "same", 3))
        self.assertEqual(ledger.usage("acme"), 2)

    def test_unknown_tenant_raises_without_creating_usage(self):
        ledger = QuotaLedger({"acme": 2})
        with self.assertRaises(ValueError):
            ledger.reserve(Reservation("other", "r", 1))
        with self.assertRaises(ValueError):
            ledger.usage("other")

    def test_constructor_rejects_malformed_limits(self):
        for limits in (None, [], {"": 1}, {"ACME": 1}, {"acme": 0}, {"acme": True}):
            with self.subTest(limits=limits), self.assertRaises((TypeError, ValueError)):
                QuotaLedger(limits)

    def test_reservation_object_type_is_required(self):
        ledger = QuotaLedger({"acme": 2})
        with self.assertRaises(TypeError):
            ledger.reserve({"tenant": "acme", "request_id": "r", "units": 1})


if __name__ == "__main__":
    unittest.main()
