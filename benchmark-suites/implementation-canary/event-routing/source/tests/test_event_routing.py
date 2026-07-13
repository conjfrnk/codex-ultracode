import unittest

from events import EventRouter, canonical_event_name, dedupe_subscriptions


class EventRoutingTests(unittest.TestCase):
    def test_canonicalizes_separator_runs(self):
        self.assertEqual(canonical_event_name(" Billing__Invoice - Ready "), "billing.invoice.ready")

    def test_router_uses_one_canonical_contract(self):
        router = EventRouter()
        router.register("Billing_Invoice Ready", lambda payload: payload["id"])
        self.assertEqual(router.dispatch("billing.invoice-ready", {"id": 7}), 7)

    def test_equivalent_registration_replaces_handler(self):
        router = EventRouter()
        router.register("job ready", lambda payload: "old")
        router.register("JOB_ready", lambda payload: "new")
        self.assertEqual(router.dispatch("job-ready", {}), "new")

    def test_dedupes_after_canonicalization(self):
        self.assertEqual(
            dedupe_subscriptions(["Job Ready", "job_ready", "billing.sent"]),
            ["job.ready", "billing.sent"],
        )


if __name__ == "__main__":
    unittest.main()
