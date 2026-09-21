import codecs
import os
import re
import unittest
from types import SimpleNamespace

from ac.picker import Picker, _read_key

ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


def session(n, title):
    return SimpleNamespace(id=f"id{n:02}", title=title)


def label(s):
    return s.title, f"{s.id} · 3 msgs"


class PickerTest(unittest.TestCase):
    def setUp(self):
        self.items = [session(1, "Planning a Trip to Lisbon"), session(2, "Rye bread recipe"),
                      session(3, "Refactoring the parser"), session(4, "Lisbon restaurants")]
        self.picker = Picker(self.items, label, title="Switch session", key=lambda s: s.id)

    def keys(self, *keys):
        result = None
        for key in keys:
            result = self.picker.handle(key)
        return result

    def plain(self, width=70):
        return [ANSI.sub("", line) for line in self.picker.lines(width)]

    def test_arrows_and_enter(self):
        self.assertEqual(self.keys("down", "down"), None)
        self.assertEqual(self.keys("enter"), "accept")
        self.assertEqual(self.picker.selected.id, "id03")

    def test_selection_stays_in_range(self):
        self.keys("up", "up")
        self.assertEqual(self.picker.selected.id, "id01")
        self.keys(*["down"] * 10)
        self.assertEqual(self.picker.selected.id, "id04")

    def test_typing_filters_by_every_word_in_any_order(self):
        self.keys(*"lisbon")
        self.assertEqual([s.id for s in self.picker.matches], ["id01", "id04"])
        self.keys(*" rest")
        self.assertEqual([s.id for s in self.picker.matches], ["id04"])
        self.assertEqual(self.keys("enter"), "accept")
        self.assertEqual(self.picker.selected.id, "id04")

    def test_filter_matches_the_id_and_backspace_and_clear_widen_it(self):
        self.keys(*"id02")
        self.assertEqual([s.id for s in self.picker.matches], ["id02"])
        self.keys("backspace")
        self.assertEqual(len(self.picker.matches), 4)
        self.keys(*"rye", "clear")
        self.assertEqual((self.picker.query, len(self.picker.matches)), ("", 4))

    def test_starting_row_and_starting_filter(self):
        picker = Picker(self.items, label, start=1)
        self.assertEqual(picker.selected.id, "id02")
        self.assertEqual(Picker(self.items, label, start=99).selected.id, "id04")
        picker = Picker(self.items, label, query="lisbon", start=3)
        self.assertEqual((picker.query, [s.id for s in picker.matches]), ("lisbon", ["id01", "id04"]))
        self.assertEqual(picker.selected.id, "id01")  # a filter puts the cursor on the best row
        picker.handle("backspace")
        self.assertEqual(picker.query, "lisbo")  # and can be edited like one that was typed
        self.assertIsNone(Picker([], label).selected)

    def deletable(self, **options):
        self.deleted = []
        return Picker(self.items, label, key=lambda s: s.id, title="Sessions",
                      delete=self.deleted.append, **options)

    def test_delete_needs_a_y(self):
        picker = self.deletable()
        picker.handle("down")
        self.assertIsNone(picker.handle("ctrl-d"))
        head = ANSI.sub("", picker.lines(90)[0])
        self.assertEqual(head, "Delete “Rye bread recipe”? y deletes it for good · any other key keeps it")
        for key in ("n", "enter", "esc", "down", "ctrl-d"):  # none of these is a yes
            picker.handle("ctrl-d") if picker.confirming is None else None
            self.assertIsNone(picker.handle(key), key)       # and none closes the picker either
            self.assertEqual((self.deleted, len(picker.items)), ([], 4), key)
        self.assertEqual(ANSI.sub("", picker.lines(90)[0]), "kept.")
        self.assertEqual(picker.selected.id, "id02")  # the refusal didn't move the cursor

    def test_delete_removes_the_row_and_keeps_the_list_open(self):
        picker = self.deletable()
        picker.handle("down")
        picker.handle("delete")  # the forward-delete key works as well as Ctrl-D
        self.assertIsNone(picker.handle("y"))
        self.assertEqual([s.id for s in self.deleted], ["id02"])
        self.assertEqual([s.id for s in picker.items], ["id01", "id03", "id04"])
        self.assertEqual(picker.selected.id, "id03")  # the cursor stays put: the next row moved up
        self.assertEqual(ANSI.sub("", picker.lines(90)[0]), "deleted “Rye bread recipe”")
        picker.handle("down")
        self.assertIn("Ctrl-D delete", ANSI.sub("", picker.lines(90)[0]))  # the message was for one key
        picker.handle("ctrl-d")
        picker.handle("Y")
        self.assertEqual(picker.selected.id, "id03")  # deleted the last row: cursor moves up
        self.assertEqual(picker.handle("enter"), "accept")

    def test_every_letter_filters_even_where_rows_can_be_deleted(self):
        picker = self.deletable()
        for ch in "bread":
            picker.handle(ch)
        self.assertEqual((picker.query, picker.confirming, self.deleted), ("bread", None, []))
        self.assertEqual(ANSI.sub("", picker.lines(90)[0]),
                         "Sessions (1 of 4) · type to filter · ↑↓ · Enter · Ctrl-D delete · Esc")

    def test_delete_keeps_the_filter(self):
        picker = self.deletable()
        for ch in "lisbon":
            picker.handle(ch)
        picker.handle("ctrl-d")
        picker.handle("y")
        self.assertEqual((picker.query, [s.id for s in picker.matches]), ("lisbon", ["id04"]))
        picker.handle("ctrl-d")
        picker.handle("y")
        self.assertEqual(picker.matches, [])
        picker.handle("ctrl-d")  # nothing highlighted: nothing to confirm, nothing deleted
        picker.handle("y")
        self.assertEqual(len(self.deleted), 2)
        self.assertEqual(picker.query, "lisbony")  # that y was just typing

    def test_protected_rows_cannot_be_deleted(self):
        picker = self.deletable(protect=lambda s: "you are in this session" if s.id == "id01" else None)
        picker.handle("ctrl-d")
        self.assertIsNone(picker.confirming)
        self.assertEqual(ANSI.sub("", picker.lines(90)[0]), "you are in this session")
        picker.handle("y")
        self.assertEqual((self.deleted, picker.query), ([], "y"))

    def test_lists_can_use_their_own_words_for_removing_a_row(self):
        picker = self.deletable(delete_label="remove", wording=lambda s: (
            f"Take {s.title} out? The file is not touched · y removes it", f"removed {s.title}"))
        self.assertIn("Ctrl-D remove", ANSI.sub("", picker.lines(120)[0]))
        picker.handle("ctrl-d")
        self.assertEqual(ANSI.sub("", picker.lines(120)[0]),
                         "Take Planning a Trip to Lisbon out? The file is not touched · y removes it "
                         "· any other key keeps it")
        picker.handle("y")
        self.assertEqual(ANSI.sub("", picker.lines(120)[0]), "removed Planning a Trip to Lisbon")
        self.assertNotIn("for good", "".join(picker.lines(120)))

    def test_a_picker_without_delete_does_not_offer_it(self):
        self.assertNotIn("delete", ANSI.sub("", self.picker.lines(90)[0]).lower())
        self.assertIsNone(self.picker.handle("delete"))
        self.assertEqual(self.picker.handle("ctrl-d"), "cancel")  # Ctrl-D just closes it, as before
        self.assertEqual(len(self.picker.items), 4)

    def test_nothing_matches(self):
        self.keys(*"zzz")
        self.assertIsNone(self.picker.selected)
        self.assertIsNone(self.keys("enter"))  # Enter does nothing rather than choosing nothing
        self.assertIn("  nothing matches", self.plain())
        self.assertEqual(self.keys("esc"), "cancel")

    def test_search_finds_sessions_by_what_was_said_in_them(self):
        calls = []

        def search(query):
            calls.append(query)
            return [self.items[1]] if query == "sourdough" else []

        picker = Picker(self.items, label, search=search, key=lambda s: s.id)
        for ch in "so":
            picker.handle(ch)
        self.assertEqual(calls, [])  # too short to be worth a database query
        for ch in "urdough":
            picker.handle(ch)
        self.assertEqual([s.id for s in picker.matches], ["id02"])
        self.assertIn("matched in messages", ANSI.sub("", picker.lines(90)[2]))

    def test_drawing(self):
        lines = self.plain(60)
        self.assertEqual(lines[0], "Switch session (4) · type to filter · ↑↓ · Enter · Esc")
        self.assertEqual(lines[1], "› ")
        self.assertTrue(lines[2].startswith("❯ Planning a Trip to Lisbon"))
        self.assertTrue(lines[3].startswith("  Rye bread recipe"))
        self.assertEqual({line.index("id0") for line in lines[2:]}, {lines[2].index("id0")})  # aligned
        self.keys(*"lis")
        self.assertIn("(2 of 4)", self.plain(60)[0])

    def test_the_current_item_keeps_a_mark_when_the_cursor_moves_away(self):
        picker = Picker(self.items, label, key=lambda s: s.id, current="id02", start=1)
        rows = [ANSI.sub("", line)[:2] for line in picker.lines(70)[2:]]
        self.assertEqual(rows, ["  ", "❯ ", "  ", "  "])  # under the cursor, the cursor shows
        picker.handle("down")
        rows = [ANSI.sub("", line)[:2] for line in picker.lines(70)[2:]]
        self.assertEqual(rows, ["  ", "• ", "❯ ", "  "])
        for ch in "lisbon":  # filtered out of view: no mark on anything else
            picker.handle(ch)
        self.assertNotIn("•", "".join(picker.lines(70)[2:]))
        self.assertNotIn("•", "".join(self.picker.lines(70)))  # and none where nothing is current

    def test_no_line_is_ever_as_wide_as_the_terminal(self):
        # A row that wraps makes the picker repaint one line short, stacking up stale headers.
        long_detail = "name · ~/Library/Mobile Documents/com~apple~CloudDocs/Vault/notes/mom/*.md +3 more"
        rows = [session(1, "@mom"), session(2, "a-very-long-file-name-" * 6 + ".md"),
                session(3, "日本語のとても長いファイル名" * 4)]
        details = {"id01": long_detail, "id02": "queued · text, 3 KB · ~/x", "id03": long_detail * 2}
        picker = Picker(rows, lambda s: (s.title, details[s.id]), key=lambda s: s.id,
                        delete=lambda s: None, title="Files", delete_label="remove")
        for width in (24, 40, 57, 80, 114, 200):
            for state in ("list", "confirm", "message", "filter"):
                if state == "confirm":
                    picker.handle("ctrl-d")
                elif state == "message":
                    picker.handle("n")
                elif state == "filter":
                    picker.handle("m")
                for line in picker.lines(width):
                    shown = ANSI.sub("", line)
                    columns = sum(2 if ord(c) > 0x2E80 else 1 for c in shown)
                    self.assertLess(columns, width, (width, state, shown))
            picker.handle("clear")

    def test_rows_never_wrap(self):
        wide = [session(n, "とても長い日本語のタイトル " * 6 + "and a long English tail " * 4)
                for n in range(3)]
        picker = Picker(wide, label)
        for width in (30, 50, 80):
            for line in picker.lines(width):
                shown = ANSI.sub("", line)
                columns = sum(2 if ord(c) > 0x2E80 else 1 for c in shown)
                self.assertLess(columns, width, shown)

    def test_long_lists_scroll(self):
        many = [session(n, f"Session number {n}") for n in range(30)]
        picker = Picker(many, label, height=5)
        self.assertEqual(len(picker.lines(70)), 2 + 5 + 1)
        self.assertIn("… 25 more", ANSI.sub("", picker.lines(70)[-1]))
        for _ in range(7):
            picker.handle("down")
        shown = [ANSI.sub("", line) for line in picker.lines(70)]
        self.assertTrue(shown[-2].startswith("❯ Session number 7"))  # selection scrolled into view
        picker.handle("pagedown")
        self.assertEqual(picker.selected.title, "Session number 12")
        picker.handle("pageup")
        picker.handle("pageup")
        self.assertEqual(picker.selected.title, "Session number 2")


class KeysTest(unittest.TestCase):
    def read_all(self, data):
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        os.write(writer, data)
        os.close(writer)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
        keys = []
        while True:
            key = _read_key(reader, decoder)
            if key == "esc":  # end of input reads as Escape
                return keys
            keys.append(key)

    def test_keys_arriving_in_one_burst_are_each_read(self):
        # A held-down arrow key, then a fast typist: nothing may be swallowed.
        self.assertEqual(self.read_all(b"\x1b[B\x1b[B\x1b[A\x1bOB\x1b[5~\x1b[6~ab\r"),
                         ["down", "down", "up", "down", "pageup", "pagedown", "a", "b", "enter"])

    def test_other_keys(self):
        self.assertEqual(self.read_all(b"\x7f\x15\t\x10\x0e\x04\x1b[3~"),
                         ["backspace", "clear", "down", "up", "down", "ctrl-d", "delete"])
        self.assertEqual(self.read_all(b"\x1b[1;5C" + b"x"), [None, "x"])  # unknown: ignored whole
        self.assertEqual([k for k in self.read_all("é日".encode()) if k], ["é", "日"])


if __name__ == "__main__":
    unittest.main()
