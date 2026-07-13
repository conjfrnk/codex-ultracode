import unittest

from quota import QuotaLedger, Reservation, parse_reservation, reserve_quota


class QuotaTests(unittest.TestCase):
    def test_parser_normalizes_without_mutating(self):
        raw = {"tenant": " ACME ", "request_id": " Req-1 ", "units": 3}
        before = dict(raw)
        self.assertEqual(parse_reservation(raw), Reservation("acme", "Req-1", 3))
        self.assertEqual(raw, before)

    def test_reservation_can_exactly_fill_limit(self):
        ledger = QuotaLedger({"acme": 3})
        self.assertTrue(ledger.reserve(Reservation("acme", "r1", 3)))
        self.assertEqual(ledger.usage("acme"), 3)

    def test_successful_request_is_idempotent(self):
        ledger = QuotaLedger({"acme": 5})
        request = Reservation("acme", "r1", 2)
        self.assertTrue(ledger.reserve(request))
        self.assertTrue(ledger.reserve(request))
        self.assertEqual(ledger.usage("acme"), 2)

    def test_rejected_request_is_idempotent(self):
        ledger = QuotaLedger({"acme": 1})
        request = Reservation("acme", "too-large", 2)
        self.assertFalse(ledger.reserve(request))
        self.assertFalse(ledger.reserve(request))
        self.assertEqual(ledger.usage("acme"), 0)

    def test_service_uses_parser_and_ledger_contracts(self):
        ledger = QuotaLedger({"acme": 2})
        self.assertTrue(reserve_quota({"tenant": "ACME", "request_id": "x", "units": 2}, ledger))


if __name__ == "__main__":
    unittest.main()
