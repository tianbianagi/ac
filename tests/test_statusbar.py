import io
import re
import unittest
from types import SimpleNamespace

from ac.render import Status, Style, status_bar, usage_level
from ac.statusbar import StatusBar

ANSI = re.compile(r"\033[()78]|\033\[[0-9;?]*[A-Za-z]")


def session(title="Planning a trip to Lisbon", persisted=True, system=None):
    return SimpleNamespace(id="3e05", title=title, persisted=persisted, system=system)


def bar(width=130, style=Style(False), **fields):
    fields = {"session": session(), "model": "qwen3.8:27b", **fields}
    return status_bar(Status(**fields), width, style)


class StatusBarTextTest(unittest.TestCase):
    def test_where_you_are_left_and_the_numbers_right_fill_the_width(self):
        line = bar(skills=["concise", "thought-partner"], queued=2, usage=(12_000, 32_000),
                   speed=41.4)
        self.assertEqual(len(line), 130)
        self.assertTrue(line.startswith(
            " Planning a trip to Lisbon · qwen3.8:27b · concise, thought-partner · 2 files queued"))
        self.assertTrue(line.endswith("12k/32k ▮▮▮▯▯▯▯▯ 38% · 41 tok/s "))

    def test_a_new_session_before_any_reply(self):
        line = bar(session=session(None, persisted=False))
        self.assertEqual(line.strip(), "new session · qwen3.8:27b · no skills")
        self.assertEqual(len(line), 130)

    def test_system_text_and_one_queued_file(self):
        line = bar(session=session(system="Be brief."), queued=1)
        self.assertIn("no skills +sys · 1 file queued", line)

    def test_a_note_sits_next_to_the_meter_in_place_of_the_speed(self):
        line = bar(usage=(12_000, 32_000), speed=41, note="thinking 4s")
        self.assertTrue(line.endswith("  12k/32k ▮▮▮▯▯▯▯▯ 38% · thinking 4s "))
        self.assertNotIn("tok/s", line)
        self.assertTrue(bar(note="waiting for qwen3.8:27b…").endswith("  waiting for qwen3.8:27b… "))

    def test_used_without_a_known_window(self):
        self.assertTrue(bar(usage=(1200, None)).endswith("1.2k ctx "))

    def test_anything_used_fills_a_cell(self):
        self.assertIn("18/1.0k ▮▯▯▯▯▯▯▯ 2%", bar(usage=(18, 1000)))

    def test_levels_and_the_compact_hint(self):
        self.assertEqual(usage_level((12_000, 32_000)), None)
        self.assertEqual(usage_level((26_000, 32_000)), "warn")
        self.assertEqual(usage_level((31_000, 32_000)), "alert")
        self.assertEqual(usage_level(None), None)
        self.assertIn("31k/32k ▮▮▮▮▮▮▮▮ 97% · nearly full · /compact", bar(usage=(31_000, 32_000)))

    def test_narrowing_drops_the_least_useful_parts_first_and_keeps_the_meter(self):
        fields = dict(skills=["concise", "thought-partner"], usage=(12_000, 32_000), speed=41)
        widths = (110, 90, 65, 50)  # each one too narrow for the step before
        steps = [bar(width, **fields) for width in widths]
        self.assertIn("tok/s", steps[0])
        self.assertNotIn("tok/s", steps[1])                    # speed goes first
        self.assertIn("Planning", steps[1])
        self.assertNotIn("Planning", steps[2])                 # then the title
        self.assertIn(" qwen3.8:27b · concise, thought-partner", steps[2])
        self.assertIn(" qwen3.8:27b · 2 skills", steps[3])     # then the skill names
        for width, line in zip(widths, steps):
            self.assertEqual(len(line), width, line)
            self.assertIn("12k/32k ▮▮▮▯▯▯▯▯ 38%", line)       # the meter every time

    def test_never_wider_than_the_terminal(self):
        line = bar(30, session=session("x" * 80), model="a-very-long-model-name:latest",
                   usage=(12_000, 32_000))
        self.assertEqual(line, " a-ver…  12k/32k ▮▮▮▯▯▯▯▯ 38% ")

    def test_wide_characters_count_as_two_cells(self):
        line = bar(40, session=session("日本語のタイトル"), skills=[], usage=None)
        self.assertEqual(sum(2 if "　" <= c <= "￯" else 1 for c in line), 40)

    def test_each_part_has_its_own_colour_and_no_background(self):
        style = Style(True)
        line = bar(skills=["concise"], queued=1, usage=(12_000, 32_000), speed=41, style=style)
        self.assertIn(Style.MAGENTA + "Planning a trip to Lisbon" + Style.RESET, line)
        self.assertIn(Style.CYAN + "qwen3.8:27b" + Style.RESET, line)
        self.assertIn(Style.GREEN + "concise" + Style.RESET, line)
        self.assertIn(Style.YELLOW + "1 file queued" + Style.RESET, line)
        self.assertIn(Style.DIM + " · " + Style.RESET, line)          # separators fade
        self.assertIn(" 12k/32k ▮▮▮▯▯▯▯▯ 38%" + Style.DIM + " · ", line)     # numbers plain
        self.assertIn(Style.DIM + "41 tok/s" + Style.RESET, line)
        self.assertNotIn("\033[7m", line)
        self.assertEqual(len(ANSI.sub("", line)), 130)
        self.assertIn(Style.DIM + "no skills", bar(style=style))
        self.assertIn(Style.GREEN + "no skills +sys", bar(session=session(system="x"), style=style))

    def test_the_numbers_take_the_colour_of_the_level(self):
        style = Style(True)
        self.assertIn(Style.YELLOW + "26k/32k", bar(usage=(26_000, 32_000), style=style))
        alert = bar(usage=(31_000, 32_000), style=style)
        self.assertIn(Style.RED + "31k/32k", alert)
        self.assertIn(Style.RED + "nearly full · /compact", alert)
        note = bar(usage=(31_000, 32_000), note="thinking 1s", style=style)
        self.assertIn(Style.RED + "31k/32k", note)                # still there while it thinks
        self.assertIn(Style.DIM + "thinking 1s" + Style.RESET, note)

    def test_clipping_keeps_whole_coloured_parts_where_it_can(self):
        fields = dict(model="a-very-long-model-name:latest", skills=["concise"],
                      usage=(12_000, 32_000))
        self.assertEqual(bar(44, **fields), " a-very-long-model-n…  12k/32k ▮▮▮▯▯▯▯▯ 38% ")
        styled = bar(44, style=Style(True), **fields)
        self.assertIn(Style.CYAN + "a-very-long-model-n…" + Style.RESET, styled)
        self.assertNotIn(Style.GREEN, styled)                  # dropped whole, not a fragment
        self.assertEqual(len(ANSI.sub("", styled)), 44)
        self.assertEqual(bar(23, usage=(12_000, 32_000)), "  12k/32k ▮▮▮▯▯▯▯▯ 38% ")  # alone
        self.assertEqual(bar(14, usage=(12_000, 32_000)), " 12k/32k ▮▮▮… ")

    def test_one_skill_counts_as_one_skill(self):
        self.assertIn(" qwen3.8:27b · 1 skill  ", bar(45, skills=["thought-partner"],
                                                     usage=(12_000, 32_000)))


class StatusBarTerminalTest(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        self.size = [(40, 10)]
        self.bar = StatusBar(self.out, size=lambda: self.size[0])
        self.addCleanup(self.bar.close)

    def written(self):
        text, _ = self.out.getvalue(), self.out.truncate(0)
        self.out.seek(0)
        return text

    def test_open_keeps_the_last_row_out_of_the_scroll_region(self):
        self.bar.open()
        self.assertEqual(self.written(), "\033D\033M\0337\033[1;9r\0338")
        self.bar.open()                                        # a second open does nothing
        self.assertEqual(self.written(), "")

    def test_draw_paints_the_last_row_and_puts_the_cursor_back(self):
        self.bar.open()
        self.written()
        self.bar.draw(lambda width: f"[{width}]")
        self.assertEqual(self.written(), "\0337\033[?7l\033[10;1H\033[2K[40]\033[?7h\0338")

    def test_close_clears_the_row_and_resets_the_region(self):
        self.bar.open()
        self.bar.draw(lambda width: "x")
        self.written()
        self.bar.close()
        self.assertEqual(self.written(), "\0337\033[10;1H\033[2K\033[r\0338")
        self.bar.close()
        self.assertEqual(self.written(), "")

    def test_a_resize_rebuilds_the_region_before_painting(self):
        self.bar.open()
        self.bar.draw(lambda width: f"[{width}]")
        self.written()
        self.size[0] = (60, 20)
        self.bar._repaint()
        text = self.written()
        self.assertNotIn("\033[10;1H", text)   # the old last row may hold anything now
        self.assertIn("\033[1;19r", text)                      # a region for 20 rows
        self.assertTrue(text.endswith("\033[20;1H\033[2K[60]\033[?7h\0338"))
        self.assertEqual(self.bar.rows, 20)

    def test_drawing_before_open_or_without_paint_does_nothing(self):
        self.bar.draw(lambda width: "x")
        self.assertEqual(self.written(), "")
        self.bar.open()
        self.bar.paint = None
        self.written()
        self.bar._repaint()
        self.assertEqual(self.written(), "")

    def test_suspended_gives_the_screen_back_and_then_repaints(self):
        self.bar.open()
        self.bar.draw(lambda width: "bar")
        self.written()
        with self.bar.suspended():
            self.assertIsNone(self.bar.rows)
            self.assertIn("\033[r", self.written())
        self.assertEqual(self.bar.rows, 10)
        self.assertTrue(self.written().endswith("\033[10;1H\033[2Kbar\033[?7h\0338"))


if __name__ == "__main__":
    unittest.main()
