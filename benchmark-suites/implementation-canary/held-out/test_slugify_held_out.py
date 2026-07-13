import unittest

from slugify import normalize_slug


class NormalizeSlugHeldOutTests(unittest.TestCase):
    def test_rejects_non_string_values(self):
        for value in (None, 42, b"bytes", ["words"]):
            with self.subTest(value=value), self.assertRaises(TypeError):
                normalize_slug(value)

    def test_handles_combining_marks_and_underscores(self):
        self.assertEqual(normalize_slug("Cafe\u0301___menu"), "cafe-menu")

    def test_collapses_existing_separators(self):
        self.assertEqual(normalize_slug("---Alpha--Beta---"), "alpha-beta")

    def test_returns_empty_for_input_without_ascii_alphanumerics(self):
        self.assertEqual(normalize_slug("!!!"), "")
        self.assertEqual(normalize_slug("東京"), "")

    def test_apostrophes_follow_the_separator_contract(self):
        self.assertEqual(normalize_slug("Developer's Guide"), "developer-s-guide")


if __name__ == "__main__":
    unittest.main()
