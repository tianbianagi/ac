import os
import tempfile
import unittest
from pathlib import Path

from ac import files


class FilesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / "notes.md").write_text("# Notes\nbuy persimmons\n")
        (self.root / "my file.txt").write_text("spaced")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "deep.py").write_text("print('hi')\n")
        (self.root / "pic.PNG").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (self.root / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)

    def found(self, text):
        return [str(p.relative_to(self.root)) for p in files.find_paths(text)]

    def test_finds_paths_in_prose(self):
        self.assertEqual(self.found(f"summarize {self.root}/notes.md please"), ["notes.md"])
        self.assertEqual(self.found("compare ./notes.md and sub/deep.py."), ["notes.md", "sub/deep.py"])
        self.assertEqual(self.found("(see ./notes.md), then ./notes.md again"), ["notes.md"])

    def test_spaces_quotes_and_at(self):
        self.assertEqual(self.found(f'read "{self.root}/my file.txt"'), ["my file.txt"])
        self.assertEqual(self.found("read ./my\\ file.txt now"), ["my file.txt"])
        self.assertEqual(self.found("what is in @notes.md?"), ["notes.md"])
        self.assertEqual(self.found("what is in notes.md?"), [])  # bare names need the @

    def test_home_directory(self):
        home = Path.home()
        inside = next((p for p in home.iterdir() if p.is_dir() and " " not in p.name), None)
        if inside:
            self.assertEqual(files.find_paths(f"look in ~/{inside.name}"), [inside.resolve()])

    def test_ignores_what_is_not_a_file(self):
        for text in ["and/or either/way", "see https://example.com/notes.md", "a / b", "~",
                     "email me @someone", f"{self.root}/missing.md", "/definitely/not/here"]:
            self.assertEqual(files.find_paths(text), [], text)

    def test_read_text_image_directory(self):
        note = files.read(self.root / "notes.md")
        self.assertEqual((note.kind, note.content, note.note), ("text", "# Notes\nbuy persimmons\n", None))
        pic = files.read(self.root / "pic.PNG")
        self.assertEqual((pic.kind, pic.data[:4], pic.content), ("image", b"\x89PNG", None))
        folder = files.read(self.root)
        self.assertEqual(folder.kind, "directory")
        self.assertEqual(folder.content.split("\n")[0], "sub/")  # directories first
        self.assertIn("notes.md", folder.content)

    def test_binary_and_unreadable(self):
        with self.assertRaisesRegex(files.FileError, "isn't text, a PDF or an image"):
            files.read(self.root / "blob.bin")
        (self.root / "latin.txt").write_bytes("café".encode("latin-1"))
        with self.assertRaisesRegex(files.FileError, "isn't text"):
            files.read(self.root / "latin.txt")
        locked = self.root / "locked.txt"
        locked.write_text("secret")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        if os.geteuid() != 0:
            with self.assertRaisesRegex(files.FileError, "can't read .*Permission denied"):
                files.read(locked)

    def test_large_text_is_truncated_not_refused(self):
        big = self.root / "big.log"
        big.write_text("é" * files.MAX_TEXT_BYTES)  # two bytes each: the cut lands mid-character
        got = files.read(big)
        self.assertLessEqual(len(got.content.encode()), files.MAX_TEXT_BYTES)
        self.assertEqual(got.note, "first 256.0 KB of 512.0 KB")
        self.assertIn('note="first 256.0 KB of 512.0 KB"', files.for_model(got))

    def test_collect_reports_problems_and_keeps_going(self):
        attachments, problems = files.collect("./blob.bin and ./notes.md")
        self.assertEqual([Path(a.path).name for a in attachments], ["notes.md"])
        self.assertEqual(len(problems), 1)

    def test_for_model(self):
        note = files.read(self.root / "notes.md")
        self.assertEqual(files.for_model(note),
                         f'<file path="{self.root}/notes.md">\n# Notes\nbuy persimmons\n\n</file>')
        self.assertEqual(files.for_model(files.read(self.root / "pic.PNG")),
                         f'<image path="{self.root}/pic.PNG"/>')
        self.assertTrue(files.for_model(files.read(self.root / "sub")).startswith("<directory path="))

    def test_describe_abbreviates_home(self):
        inside = files.Attachment(str(Path.home() / "docs" / "a.md"), "text", content="hello")
        self.assertEqual(files.describe(inside), "~/docs/a.md (text, 5 B)")
        self.assertIn(str(Path.home()), files.for_model(inside))  # the model gets the real path
        lookalike = files.Attachment(str(Path.home()) + "-other/a.md", "text", content="")
        self.assertTrue(files.describe(lookalike).startswith(str(Path.home()) + "-other"))

    def test_complete(self):
        self.assertEqual(files.complete("./no"), ["./notes.md"])
        self.assertEqual(files.complete("@no"), ["@notes.md"])
        self.assertEqual(files.complete("sub/"), ["sub/deep.py"])
        self.assertEqual(files.complete("./s"), ["./sub/"])
        self.assertEqual(files.complete("./my"), ["./my\\ file.txt"])
        self.assertEqual(files.complete(f"{self.root}/pi"), [f"{self.root}/pic.PNG"])
        self.assertEqual(files.complete("/definitely/not/he"), [])


if __name__ == "__main__":
    unittest.main()
