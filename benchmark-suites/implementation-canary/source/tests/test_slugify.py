import unittest

from slugify import normalize_slug


class NormalizeSlugTests(unittest.TestCase):
    def test_normalizes_words_and_whitespace(self):
        self.assertEqual(normalize_slug("  Release   Notes  "), "release-notes")

    def test_transliterates_accents(self):
        self.assertEqual(normalize_slug("Creme brulee deja vu"), "creme-brulee-deja-vu")
        self.assertEqual(normalize_slug("Crème brûlée déjà vu"), "creme-brulee-deja-vu")

    def test_collapses_punctuation(self):
        self.assertEqual(normalize_slug("API: v2 / Migration"), "api-v2-migration")


if __name__ == "__main__":
    unittest.main()
