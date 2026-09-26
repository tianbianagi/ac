import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ac import files


class PatternTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.write("proj/main.py", "print('main')\n")
        self.write("proj/README.md", "# Project\n")
        self.write("proj/src/app.py", "def app(): pass\n")
        self.write("proj/src/deep/util.py", "def util(): pass\n")
        self.write("proj/src/deep/notes.md", "deep notes\n")
        self.write("proj/.secret/key.py", "KEY = 1\n")
        self.write("proj/.env", "TOKEN=abc\n")
        self.write("proj/node_modules/lib/index.js", "module.exports = 1\n")
        self.write("proj/src/__pycache__/app.cpython-314.pyc", b"\x00\x01binary")
        self.write("proj/src/logo.png", b"\x89PNG\r\n\x1a\nfake")
        self.write("proj/src/data.bin", b"\x00\x01\x02")
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        config = mock.patch.dict(os.environ, {"AC_CONFIG_DIR": str(self.root / "no-config" / "ac")})
        config.start()
        self.addCleanup(config.stop)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return path

    def names(self, attachments):
        return [str(Path(a.path).relative_to(self.root / "proj")) for a in attachments]

    def test_double_star_takes_every_file_underneath(self):
        attachments, notes = files.collect("review proj/** please")
        self.assertEqual(self.names(attachments),
                         ["README.md", "main.py", "src/app.py", "src/deep/notes.md",
                          "src/deep/util.py"])  # no hidden files, dependencies, caches or binaries
        self.assertEqual(sorted(notes), [
            "proj/**: left out 1 images (name one directly to send it)",
            "proj/**: left out 1 not text"])
        self.assertTrue(all(a.group == "proj/**" and a.kind == "text" for a in attachments))
        self.assertIn("def util", files.for_model(attachments[-1]))

    def test_narrower_patterns(self):
        self.assertEqual(self.names(files.collect("see proj/**/*.py")[0]),
                         ["main.py", "src/app.py", "src/deep/util.py"])
        self.assertEqual(self.names(files.collect("see proj/src/*.py.")[0]), ["src/app.py"])
        self.assertEqual(self.names(files.collect(f"see {self.root}/proj/src/deep/**")[0]),
                         ["src/deep/notes.md", "src/deep/util.py"])
        os.chdir(self.root / "proj")
        self.assertEqual(self.names(files.collect("see @*.md")[0]), ["README.md"])

    def test_what_you_spell_out_is_taken_as_meant(self):
        self.assertEqual(self.names(files.collect("see proj/.secret/**")[0]), [".secret/key.py"])
        os.chdir(self.root / "proj" / "src")  # a pattern that climbs out of the current folder
        self.assertEqual(self.names(files.collect("see ../src/deep/**")[0]),
                         ["src/deep/notes.md", "src/deep/util.py"])

    def test_things_that_are_not_patterns(self):
        for text in ["**bold** and *italic*", "2 * 3", "a/**b** c", "proj/nothing/**", "*"]:
            self.assertEqual(files.collect(text), ([], []), text)
        (listing,), _ = files.collect("what is in proj/src ?")  # no star: just a listing
        self.assertEqual(listing.kind, "directory")

    def test_a_file_named_twice_is_sent_once(self):
        attachments, _ = files.collect("start with proj/main.py then all of proj/**/*.py")
        self.assertEqual(self.names(attachments), ["main.py", "src/app.py", "src/deep/util.py"])
        self.assertEqual([a.group for a in attachments], [None, "proj/**/*.py", "proj/**/*.py"])

    def test_size_budget(self):
        for n in range(6):
            self.write(f"big/file{n}.txt", "x" * 1000)
        self.write("no-config/ac/config.toml", "max_attach_kb = 3\n")
        attachments, notes = files.collect("read big/**")
        self.assertEqual(len(attachments), 3)  # 3 KB of 1000-byte files
        self.assertEqual(notes, ["big/**: left out 3 past the 3.0 KB limit (a narrower pattern, "
                                 "such as **/*.py, fits more of what matters)"])

    def test_count_limit(self):
        for n in range(8):
            self.write(f"many/f{n}.txt", "x")
        with mock.patch.object(files, "MAX_PATTERN_FILES", 5):
            attachments, notes = files.collect("read many/**")
        self.assertEqual(len(attachments), 5)
        self.assertIn("many/**: left out 3 past the 5 files limit", notes[0])

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_gitignored_files_are_left_out(self):
        self.write("proj/.gitignore", "*.log\nsrc/deep/\n")
        self.write("proj/debug.log", "noise\n")
        subprocess.run(["git", "init", "-q", str(self.root / "proj")], check=True)
        self.assertEqual(self.names(files.collect("review proj/**")[0]),
                         ["README.md", "main.py", "src/app.py"])

    def test_announce_and_summarize(self):
        attachments, _ = files.collect("one proj/main.py and the rest proj/src/**")
        self.assertEqual(files.announce(attachments), [
            f"attached {files.display_path(self.root / 'proj/main.py')} (text, 14 B)",
            "attached 3 files from proj/src/** (44 B); /files lists them"])
        self.assertEqual(len(files.summarize(attachments)), 4)  # few enough to list
        many, _ = files.collect("all of proj/**")
        lines = files.summarize(many + attachments)
        self.assertEqual(len(lines), 4)
        self.assertRegex(lines[-1], r"and 6 more files \(\d+ B\)")


if __name__ == "__main__":
    unittest.main()
