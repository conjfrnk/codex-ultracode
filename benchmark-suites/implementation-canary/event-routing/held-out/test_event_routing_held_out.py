import unittest

from events import EventRouter, canonical_event_name, dedupe_subscriptions


class EventRoutingHeldOutTests(unittest.TestCase):
    def test_casefolds_unicode_segments(self):
        self.assertEqual(canonical_event_name("STRASSE_Ready"), "strasse.ready")
        self.assertEqual(canonical_event_name("Stra\u00dfe Ready"), "strasse.ready")

    def test_rejects_non_strings(self):
        for value in (None, 3, b"event", ["event"]):
            with self.subTest(value=value), self.assertRaises(TypeError):
                canonical_event_name(value)

    def test_rejects_empty_and_unsupported_punctuation(self):
        for value in ("", " -._ ", "billing/invoice", "ready!"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_event_name(value)

    def test_router_surfaces_name_errors(self):
        router = EventRouter()
        with self.assertRaises(ValueError):
            router.register("bad/name", lambda payload: payload)
        with self.assertRaises(TypeError):
            router.dispatch(None, {})

    def test_unknown_canonical_event_raises_key_error(self):
        router = EventRouter()
        router.register("known event", lambda payload: payload)
        with self.assertRaises(KeyError):
            router.dispatch("other_event", {})

    def test_dedupe_validates_container_and_entries(self):
        with self.assertRaises(TypeError):
            dedupe_subscriptions(("job.ready",))
        with self.assertRaises(TypeError):
            dedupe_subscriptions(["job.ready", 4])

    def test_dedupe_preserves_first_canonical_appearance(self):
        self.assertEqual(
            dedupe_subscriptions([" A-B ", "a.b", "C_D", "c d", "e"]),
            ["a.b", "c.d", "e"],
        )


if __name__ == "__main__":
    unittest.main()
