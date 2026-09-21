import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ac import cli, config
from ac.store import Store
from tests.fake_ollama import FakeOllama
from tests.test_skills import write_skill


class NotATty(io.StringIO):
    def isatty(self):
        return False


class CliTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.addCleanup(self.fake.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        env = mock.patch.dict(os.environ, {
            "AC_DB": str(self.tmp / "ac.db"), "AC_SKILLS_PATH": str(self.tmp / "skills"),
            "XDG_CONFIG_HOME": str(self.tmp / "config"), "OLLAMA_HOST": self.fake.host,
            "NO_COLOR": "1"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AC_MODEL", None)
        os.environ.pop("AC_EXPORT_DIR", None)

    def ac(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", NotATty(stdin)), redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def store(self):
        store = Store(config.db_path())
        self.addCleanup(store.close)
        return store

    def test_default_command(self):
        self.assertEqual(cli._default_command([]), ["new"])
        self.assertEqual(cli._default_command(["-m", "x"]), ["new", "-m", "x"])
        self.assertEqual(cli._default_command(["-c"]), ["resume"])
        self.assertEqual(cli._default_command(["ls"]), ["ls"])
        self.assertEqual(cli._default_command(["--help"]), ["--help"])

    def test_ask_prints_only_the_answer_and_saves_a_session(self):
        self.fake.reply("4", thinking="two plus two")
        code, out, err = self.ac("ask", "what is", "2+2?")
        self.assertEqual((code, out, err), (0, "4\n", ""))
        (session,) = self.store().list()
        self.assertEqual((session.title, session.model, session.message_count),
                         ("what is 2+2?", "m1", 2))

    def test_default_model(self):
        self.ac("ask", "on a machine without the default")
        self.assertEqual(self.fake.requests[-1]["model"], "m1")  # falls back to first installed
        self.fake.models.append(config.DEFAULT_MODEL)
        self.ac("ask", "-m", "m2", "a session on another model")
        self.ac("ask", "new sessions still start on the default")
        self.assertEqual(self.fake.requests[-1]["model"], "qwen3.8:27b")
        with mock.patch.dict(os.environ, {"AC_MODEL": "m2"}):
            self.ac("ask", "the environment overrides it")
            self.assertEqual(self.fake.requests[-1]["model"], "m2:latest")
            self.ac("ask", "-m", "m1", "and -m overrides both")
            self.assertEqual(self.fake.requests[-1]["model"], "m1")

    def test_ask_reads_stdin_where_dash_asks_for_it(self):
        self.ac("ask", "-", stdin="from a pipe\n")
        self.ac("ask", stdin="no prompt, just a pipe\n")
        self.ac("ask", "summarize", "this:", "-", "in", "one", "line", stdin="the document\n")
        prompts = [r["messages"][-1]["content"] for r in self.fake.requests]
        self.assertEqual(prompts, ["from a pipe", "no prompt, just a pipe",
                                   "summarize this:\n\nthe document\n\nin one line"])

    def test_ask_never_touches_stdin_unasked(self):
        # Non-interactive callers (cron, CI) hand us a stdin that is neither a tty nor ever
        # closed; reading it would hang forever.
        class Hangs(NotATty):
            def read(self, *args):
                raise AssertionError("ask read stdin without being asked to")
        with mock.patch("sys.stdin", Hangs()):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(["ask", "--no-save", "hi"]), 0)
        self.assertEqual(out.getvalue(), "ok\n")

    def test_ask_no_save_and_empty(self):
        code, out, _ = self.ac("ask", "--no-save", "hi")
        self.assertEqual((code, out), (0, "ok\n"))
        self.assertEqual(self.store().list(), [])
        code, _, err = self.ac("ask")
        self.assertEqual(code, 1)
        self.assertIn("nothing to ask", err)

    def test_ask_in_existing_session_with_skill(self):
        write_skill(self.tmp / "skills", "haiku", body="Haiku only.")
        self.ac("ask", "-s", "haiku", "-m", "m2", "first")
        (session,) = self.store().list()
        self.assertEqual((session.model, session.skills), ("m2:latest", ["haiku"]))
        self.ac("ask", "-S", session.id[:4], "second")
        sent = self.fake.requests[-1]
        self.assertEqual(sent["model"], "m2:latest")
        self.assertEqual([m["role"] for m in sent["messages"]],
                         ["system", "user", "assistant", "user"])

    def test_ask_failure_exit_codes(self):
        code, out, err = self.ac("ask", "-m", "nope", "hi")
        self.assertEqual(code, 1)
        self.assertIn("model 'nope' is not installed", err)
        with mock.patch.dict(os.environ, {"OLLAMA_HOST": "127.0.0.1:1", "AC_MODEL": "m1"}):
            code, out, err = self.ac("ask", "hi")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Is it running?", err)
        self.assertNotIn("Traceback", err)

    def test_session_crud(self):
        self.ac("ask", "tell me about sourdough")
        self.ac("ask", "tell me about rye")
        store = self.store()
        rye, sourdough = store.list()

        code, out, _ = self.ac("ls")
        self.assertIn(rye.id, out)
        self.assertIn("tell me about sourdough", out)
        _, out, _ = self.ac("ls", "--search", "sourdough")
        self.assertIn(sourdough.id, out)
        self.assertNotIn(rye.id, out)

        self.ac("rename", rye.id[:4], "Rye", "bread")
        self.assertEqual(store.get(rye.id).title, "Rye bread")

        _, out, _ = self.ac("show", "rye bread")
        self.assertIn(">>> tell me about rye", out)

        self.ac("set", rye.id, "-m", "m2", "--system", "Be brief.", "--think", "off",
                "-o", "temperature=0.3", "-o", "num_ctx=8192")
        got = store.get(rye.id)
        self.assertEqual((got.model, got.system, got.options), ("m2:latest", "Be brief.", {
            "think": False, "temperature": 0.3, "num_ctx": 8192}))
        self.ac("set", rye.id, "--clear-system", "--think", "default", "-o", "num_ctx=")
        got = store.get(rye.id)
        self.assertEqual((got.system, got.options), (None, {"temperature": 0.3}))

        _, out, _ = self.ac("fork", rye.id, "--at", "1", "--title", "branch")
        branch = store.get("branch")
        self.assertEqual((branch.parent_id, branch.message_count), (rye.id, 1))

        _, out, _ = self.ac("export", rye.id, "--format", "json")
        self.assertEqual(json.loads(out)["title"], "Rye bread")
        target = self.tmp / "rye.md"
        self.ac("export", rye.id, "-o", str(target))
        self.assertIn("# Rye bread", target.read_text())
        with mock.patch.dict(os.environ, {"AC_EXPORT_DIR": str(self.tmp / "vault")}):
            _, out, err = self.ac("export", rye.id, "--save")
        (saved,) = (self.tmp / "vault").glob("????-??-?? Rye bread.md")  # renamed by hand above
        self.assertEqual((out, err), ("", f"wrote {saved}\n"))

        self.fake.reply("Sourdough Starters Explained")
        with mock.patch.dict(os.environ, {"AC_EXPORT_DIR": str(self.tmp / "vault")}):
            _, out, err = self.ac("export", sourdough.id, "--save")
        self.assertIn("titled: Sourdough Starters Explained", err)
        self.assertEqual(len(list((self.tmp / "vault").glob("* Sourdough Starters Explained.md"))), 1)

        code, _, err = self.ac("rm", rye.id)
        self.assertEqual(code, 1)
        self.assertIn("pass -y", err)
        code, out, _ = self.ac("rm", "-y", rye.id, branch.id)
        self.assertEqual(code, 0)
        self.assertEqual([s.id for s in store.list()], [sourdough.id])

    def test_errors_are_one_line(self):
        code, _, err = self.ac("show", "nope")
        self.assertEqual((code, err), (1, "acc: no session matches 'nope'\n"))
        code, _, err = self.ac("resume")
        self.assertEqual(code, 1)
        self.assertIn("no sessions yet", err)

    def test_skills_and_models(self):
        _, out, _ = self.ac("skills")
        self.assertIn("no skills found", out)
        write_skill(self.tmp / "skills", "haiku", body="Haiku only.", description="Poetry mode")
        _, out, _ = self.ac("skills")
        self.assertRegex(out, r"haiku\s+Poetry mode")
        _, out, _ = self.ac("skills", "haiku")
        self.assertIn("Haiku only.", out)
        _, out, _ = self.ac("models")
        self.assertRegex(out, r"m2:latest\s+1B\s+Q4\s+0\.0 GB\s+completion, thinking")

    def test_interactive_new_with_piped_input(self):
        self.fake.reply("Hello!")
        code, out, _ = self.ac("-m", "m1", "--title", "Piped", stdin="hi\n/quit\n")
        self.assertEqual(code, 0)
        self.assertIn("Hello!", out)
        self.assertEqual(self.store().get("piped").message_count, 2)


if __name__ == "__main__":
    unittest.main()
