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
        (self.root / "sub").mkdir()
        (self.root / "pic.PNG").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (self.root / "blob.bin").write_bytes(b"\x00\x01\x02binary")

    def test_read_text_and_image(self):
        note = files.read(self.root / "notes.md")
        self.assertEqual((note.kind, note.content, note.note), ("text", "# Notes\nbuy persimmons\n", None))
        pic = files.read(self.root / "pic.PNG")
        self.assertEqual((pic.kind, pic.data[:4], pic.content), ("image", b"\x89PNG", None))

    def test_only_files_are_read(self):
        with self.assertRaisesRegex(files.FileError, "sub isn't a file"):
            files.read(self.root / "sub")
        with self.assertRaisesRegex(files.FileError, "missing.md isn't a file"):
            files.read(self.root / "missing.md")
        os.mkfifo(self.root / "pipe")               # never opened: it would wait forever
        with self.assertRaisesRegex(files.FileError, "pipe isn't a file"):
            files.read(self.root / "pipe")

    def test_binary_and_unreadable(self):
        with self.assertRaisesRegex(files.FileError, "blob.bin isn't text, a PDF or an image"):
            files.read(self.root / "blob.bin")
        (self.root / "latin.txt").write_bytes("café".encode("latin-1"))
        with self.assertRaisesRegex(files.FileError, "isn't text"):
            files.read(self.root / "latin.txt")
        locked = self.root / "locked.txt"
        locked.write_text("secret")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        if os.geteuid() != 0:
            with self.assertRaisesRegex(files.FileError, "can't read locked.txt: Permission denied"):
                files.read(locked)

    def test_large_text_is_truncated_not_refused(self):
        big = self.root / "big.log"
        big.write_text("é" * files.MAX_TEXT_BYTES)  # two bytes each: the cut lands mid-character
        got = files.read(big)
        self.assertLessEqual(len(got.content.encode()), files.MAX_TEXT_BYTES)
        self.assertEqual(got.note, "first 256.0 KB of 512.0 KB")
        self.assertIn('note="first 256.0 KB of 512.0 KB"', files.for_model(got))

    def test_read_all(self):
        (got,), notes = files.read_all(self.root / "notes.md")
        self.assertEqual((got.kind, notes), ("text", []))
        with self.assertRaises(files.FileError):
            files.read_all(self.root / "blob.bin")

    def test_for_model(self):
        note = files.read(self.root / "notes.md")
        self.assertEqual(files.for_model(note),
                         f'<file path="{self.root}/notes.md">\n# Notes\nbuy persimmons\n\n</file>')
        self.assertEqual(files.for_model(files.read(self.root / "pic.PNG")),
                         f'<image path="{self.root}/pic.PNG"/>')
        listing = files.Attachment("/old", "directory", content="a/\nb")     # from before
        self.assertTrue(files.for_model(listing).startswith('<directory path="/old">'))

    def test_describe_announce_and_summarize(self):
        sent = files.Attachment("recipe.txt", "text", content="hello")
        self.assertEqual(files.describe(sent), "recipe.txt (text, 5 B)")
        self.assertEqual(files.announce([sent]), ["attached recipe.txt (text, 5 B)"])
        inside = files.Attachment(str(Path.home() / "docs" / "a.md"), "text", content="hello")
        self.assertEqual(files.describe(inside), "~/docs/a.md (text, 5 B)")   # attached by path, before
        self.assertIn(str(Path.home()), files.for_model(inside))
        lookalike = files.Attachment(str(Path.home()) + "-other/a.md", "text", content="")
        self.assertTrue(files.describe(lookalike).startswith(str(Path.home()) + "-other"))
        many = [files.Attachment(f"f{i}.txt", "text", content="x" * i) for i in range(1, 8)]
        self.assertEqual(files.summarize(many)[-1], "and 4 more files (22 B)")
        self.assertEqual(len(files.summarize(many)), 4)


if __name__ == "__main__":
    unittest.main()
