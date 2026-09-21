import unittest

from ac import render, titles


class CleanTest(unittest.TestCase):
    def test_clean(self):
        cases = {
            "Planning a Trip to Lisbon": "Planning a Trip to Lisbon",
            '  "Planning a Trip to Lisbon."  ': "Planning a Trip to Lisbon",
            "**Title:** Planning a Trip\n\nThis captures...": "Planning a Trip",
            "# `Refactoring` the *Parser*!": "Refactoring the Parser",
            "\n\n“Budget für März”": "Budget für März",
            "": None, ' "" ': None, "...": None,
        }
        for raw, expected in cases.items():
            self.assertEqual(titles.clean(raw), expected, raw)

    def test_long_titles_are_cut_at_a_word(self):
        title = titles.clean("word " * 40)
        self.assertLessEqual(len(title), titles.MAX_LENGTH)
        self.assertTrue(title.endswith("word"))


class FilenameTest(unittest.TestCase):
    def test_safe_filename(self):
        cases = {
            "Planning a Trip": "Planning a Trip",
            "TCP/IP vs. UDP: which?": "TCP IP vs. UDP which",
            'a\\b*c"d<e>f|g': "a b c d e f g",
            "  ..hidden and spaced..  ": "hidden and spaced",
            "tab\tand\nnewline": "tab and newline",
            "日本語のタイトル": "日本語のタイトル",
            "": "", "///": "",
        }
        for title, expected in cases.items():
            self.assertEqual(render.safe_filename(title), expected, title)
        self.assertLessEqual(len(render.safe_filename("word " * 40)), 80)


if __name__ == "__main__":
    unittest.main()
