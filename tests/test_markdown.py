import io
import random
import re
import unittest

from ac.markdown import BOLD, CYAN, ITALIC, RESET, STRIKE, UNDERLINE, MarkdownStream, _sgr, render

ANSI = re.compile(r"\033\[[0-9;]*m")

DOCUMENT = '''# Trip plan

Here is **the plan** for the *Lisbon* trip, with `inline code`, ~~a mistake~~ and a [link to the docs](https://example.com/docs). This paragraph is long enough that it has to wrap somewhere in the middle.

## Steps
- Book the **flight** (TP1357), which leaves early and therefore needs an alarm, a taxi and a plan
  - nested: check `gate` at 07:10
- [x] Pay the hotel
- [ ] Pack
1. First
10. Tenth, snake_case_name and 2 * 3 * 4

> Travel is fatal to prejudice, bigotry, and narrow-mindedness, and many people need it sorely.

```python
def gate_closes(flight):
    return "07:10"  # **not bold** and a very long comment that must never be wrapped by the renderer at all
```

| City | Nights | Notes |
|:-----|-------:|-------|
| Lisbon | 3 | **check-in** after 15:00 and a long note that needs wrapping to fit |
| Porto | 2 | by train |

---
Done.'''


def plain(text, width=60):
    return ANSI.sub("", render(text, width))


class StreamingTest(unittest.TestCase):
    def test_output_does_not_depend_on_how_the_text_is_chunked(self):
        whole = render(DOCUMENT, 60)
        rng = random.Random(7)
        for trial in range(25):
            out = io.StringIO()
            stream = MarkdownStream(out, 60)
            i = 0
            while i < len(DOCUMENT):
                step = 1 if trial == 0 else rng.randint(1, 12)
                stream.feed(DOCUMENT[i:i + step])
                i += step
            stream.finish()
            self.assertEqual(out.getvalue().rstrip("\n"), whole)

    def test_text_appears_before_its_line_is_finished(self):
        out = io.StringIO()
        stream = MarkdownStream(out, 60)
        stream.feed("The quick brown fox jumps over")
        self.assertEqual(out.getvalue(), "The quick brown fox jumps")  # all but the open word
        stream.feed(" the lazy")
        self.assertEqual(out.getvalue(), "The quick brown fox jumps over the")

    def test_styles_never_leak_past_the_end(self):
        for text in [DOCUMENT, "an **unclosed bold", "an `unclosed span", "```\nunclosed fence",
                     "> quote", "# heading", "[broken](link"]:
            out = render(text, 40)
            codes = ANSI.findall(out)
            if codes:
                self.assertEqual(codes[-1], RESET, text)
            for line in out.split("\n"):  # and every line closes what it opened
                found = ANSI.findall(line)
                self.assertTrue(not found or found[-1] == RESET, line)


class InlineTest(unittest.TestCase):
    def test_emphasis_and_code(self):
        self.assertEqual(render("a **b** c"), f"a {_sgr([BOLD])}b{RESET} c")
        self.assertEqual(render("a *b* c"), f"a {_sgr([ITALIC])}b{RESET} c")
        self.assertEqual(render("a _b_ c"), f"a {_sgr([ITALIC])}b{RESET} c")
        self.assertEqual(render("a ***b*** c"), f"a {_sgr([BOLD, ITALIC])}b{RESET} c")
        self.assertEqual(render("a ~~b~~ c"), f"a {_sgr([STRIKE])}b{RESET} c")
        self.assertEqual(render("a `b*c*` d"), f"a {_sgr([CYAN])}b*c*{RESET} d")
        self.assertEqual(render("``a ` b``"), f"{_sgr([CYAN])}a ` b{RESET}")
        self.assertEqual(plain("**bold with *nested italic* inside**"), "bold with nested italic inside")

    def test_spans_that_never_close_are_literal(self):
        for text in ["def f(*args, **kwargs): pass", "an `unclosed tick", "a ** b", "**bold* mismatch",
                     "use __init__.py and __name__ == '__main__'", "_private_var", "~~half",
                     "pointer *p and **pp"]:
            self.assertEqual(render(text), text, text)

    def test_a_span_is_held_until_it_closes(self):
        out = io.StringIO()
        stream = MarkdownStream(out, 60)
        stream.feed("this is **very impor")
        self.assertEqual(out.getvalue(), "this is")  # can't know yet whether it is bold
        stream.feed("tant** indeed ")
        self.assertEqual(ANSI.sub("", out.getvalue()), "this is very important indeed")
        self.assertIn(_sgr([BOLD]) + "very", out.getvalue())

    def test_a_very_long_span_is_given_up_on_rather_than_held_forever(self):
        out = io.StringIO()
        stream = MarkdownStream(out, 60)
        stream.feed("**" + "word " * 200)
        self.assertIn("**word word", out.getvalue())

    def test_nesting_and_edges(self):
        self.assertEqual(render("**bold *it***"),
                         f"{_sgr([BOLD])}bold {RESET}{_sgr([BOLD, ITALIC])}it{RESET}")
        self.assertEqual(plain("**a `b**c` d**"), "a b**c d")
        self.assertEqual(plain(r"**a \** b**"), "a ** b")
        self.assertEqual(plain("*a* and *b*, (**c**)."), "a and b, (c).")

    def test_things_that_are_not_markup_stay_literal(self):
        for text in ["snake_case_name and __init__.py", "2 * 3 * 4 = 24", "a * b", "5 ~ 10 minutes",
                     "~/notes/plan.md", "-5 degrees", "2024 was a year", "3.14 is pi", "1)",
                     "a < b > c", "see [1] and [2]", "[not a link] (at all)", "C:\\Users\\me",
                     "price: $5 * 2", "#hashtag", "####### seven", "_", "*", "`"]:
            self.assertEqual(render(text), text, text)

    def test_unclosed_markup_is_contained_to_its_line(self):
        self.assertEqual(render("an **unclosed bold\nnext **line**"),
                         f"an **unclosed bold\nnext {_sgr([BOLD])}line{RESET}")

    def test_escapes(self):
        self.assertEqual(render(r"\*not italic\* and 1\. not a list"), "*not italic* and 1. not a list")

    def test_links(self):
        self.assertEqual(render("[docs](https://x.io/a)"),
                         f"{_sgr([UNDERLINE])}docs{RESET}{_sgr([2])} (https://x.io/a){RESET}")
        self.assertEqual(plain("[https://x.io](https://x.io)"), "https://x.io")  # not said twice
        self.assertEqual(plain("![a diagram](d.png)"), "!a diagram (d.png)")
        self.assertEqual(plain("[**bold** text](u)"), "bold text (u)")
        self.assertEqual(plain("[[nested]] and [unfinished"), "[[nested]] and [unfinished")
        self.assertEqual(plain("[text](broken url"), "[text](broken url")
        self.assertEqual(plain("[" + "x" * 700 + "](u)"), "[" + "x" * 700 + "](u)")  # given up on


class BlockTest(unittest.TestCase):
    def test_headings(self):
        self.assertEqual(render("# One"), f"{_sgr([BOLD, UNDERLINE])}One{RESET}")
        self.assertEqual(render("### Three `x`"),
                         f"{_sgr([BOLD])}Three {RESET}{_sgr([BOLD, CYAN])}x{RESET}")

    def test_lists_wrap_with_a_hanging_indent(self):
        text = "- alpha beta gamma delta epsilon zeta eta theta\n  - nested item that is also long enough to wrap"
        self.assertEqual(plain(text, 24), "\n".join([
            "• alpha beta gamma delta", "  epsilon zeta eta theta",
            "  • nested item that is", "    also long enough to", "    wrap"]))
        self.assertEqual(plain("1. one\n12. twelve is long enough to wrap around", 20),
                         "1. one\n12. twelve is long\n    enough to wrap\n    around")
        self.assertEqual(plain("-   padded marker\n1.  padded number, wrapping here", 22),
                         "• padded marker\n1. padded number,\n   wrapping here")
        self.assertEqual(plain("* star\n+ plus\n- [x] done\n- [ ] todo\n- [link](u) first"),
                         "• star\n• plus\n• ☑ done\n• ☐ todo\n• link (u) first")

    def test_quotes_keep_their_bar_on_every_line(self):
        self.assertEqual(plain("> one two three four five six seven", 16),
                         "│ one two three\n│ four five six\n│ seven")

    def test_no_prose_line_is_wider_than_the_terminal(self):
        prose = "\n".join(line for line in DOCUMENT.split("\n") if "very long comment" not in line)
        for width in (30, 47, 80):
            for line in plain(prose, width).split("\n"):
                self.assertLessEqual(len(line), width, line)

    def test_a_word_longer_than_the_line_is_not_lost(self):
        url = "https://example.com/" + "a" * 60
        self.assertEqual(plain(f"see {url} now", 30), f"see\n{url}\nnow")

    def test_code_is_printed_exactly(self):
        code = ["def f(x):", "    return x ** 2  # **kwargs, `ticks`, [a](b), and a line far too long to fit in the width",
                "", "  ``` still code", "\tdone()", "     ```"]
        out = plain("```python\n" + "\n".join(code) + "\n```\nafter **bold**", 30)
        self.assertEqual(out.split("\n"), ["```python"] + code + ["```", "after bold"])

    def test_fence_variants(self):
        self.assertEqual(plain("~~~\na ``` b\n~~~\nx"), "~~~\na ``` b\n~~~\nx")
        self.assertEqual(plain("````\n```\ninner\n```\n````\n*x*"), "````\n```\ninner\n```\n````\nx")
        self.assertEqual(plain("```not a fence` really"), "```not a fence` really")
        self.assertEqual(plain("```js inline``` span"), "js inline span")
        self.assertEqual(plain("```\nnever closed"), "```\nnever closed")

    def test_rules(self):
        for rule in ("---", "***", "___", "- - -", "-----"):
            self.assertEqual(plain(f"a\n\n{rule}\n\nb", 10), "a\n\n" + "─" * 10 + "\n\nb", rule)
        self.assertEqual(plain("--"), "--")

    def test_table(self):
        out = plain("| Name | Qty |\n|---|--:|\n| Apple | 3 |\n| Kiwi | 12 |\n\nafter")
        self.assertEqual(out.split("\n"), [
            "┌───────┬─────┐", "│ Name  │ Qty │", "├───────┼─────┤", "│ Apple │   3 │",
            "│ Kiwi  │  12 │", "└───────┴─────┘", "", "after"])

    def test_wide_table_is_wrapped_to_fit(self):
        text = "| A | B |\n|---|---|\n| short | " + "many words " * 12 + "|\n"
        lines = plain(text, 40).split("\n")
        self.assertTrue(all(len(line) <= 40 for line in lines), lines)
        self.assertEqual(" ".join("".join(lines).split("│")[-2].split())[-10:], "many words")
        self.assertEqual(sum("many" in line for line in lines), 5)  # nothing dropped: 12 in 5 rows

    def test_pipes_that_are_not_a_table(self):
        self.assertEqual(plain("| just a line with pipes |\nthen **text**"),
                         "| just a line with pipes |\nthen text")
        self.assertEqual(plain("| a | b |"), "| a | b |")

    def test_wide_characters(self):
        text = "これは日本語の長い文章で、空白がまったくありませんが、それでも折り返されます。"
        lines = plain(text, 20).split("\n")
        self.assertEqual("".join(lines), text)
        self.assertEqual([sum(2 for _ in line) for line in lines], [20, 20, 20, 18])
        self.assertEqual(plain("这是**重点**内容"), "这是重点内容")
        self.assertIn(_sgr([BOLD]) + "重点" + RESET, render("这是**重点**内容"))

    def test_wide_text_streams_without_waiting_for_a_space(self):
        out = io.StringIO()
        MarkdownStream(out, 40).feed("これは日本語")
        self.assertEqual(out.getvalue(), "これは日本語")


if __name__ == "__main__":
    unittest.main()
