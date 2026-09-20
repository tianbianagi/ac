import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ac.ollama import Client
from ac.render import Style
from ac.repl import SUMMARY_HEADER, Repl, build_messages, make_title
from ac.store import Attachment, Message, Store
from tests.fake_ollama import FakeOllama
from tests.test_skills import write_skill


def msg(role, content, status="complete", thinking=None):
    return Message(id=0, session_id="s", seq=0, role=role, content=content, status=status,
                   thinking=thinking)


class BuildMessagesTest(unittest.TestCase):
    def test_no_system_message_when_nothing_attached(self):
        self.assertEqual(build_messages(None, [msg("user", "hi")]),
                         [{"role": "user", "content": "hi"}])

    def test_thinking_never_sent_and_errors_skipped(self):
        payload = build_messages("sys", [
            msg("user", "q1"), msg("assistant", "a1", thinking="secret reasoning"),
            msg("user", "q2"), msg("assistant", "broken", status="error"),
            msg("assistant", "", status="interrupted", thinking="only thought"),
            msg("user", "q3"), msg("assistant", "partial", status="interrupted")])
        self.assertEqual(payload, [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2\n\nq3"},  # neighbours merged after the skip
            {"role": "assistant", "content": "partial"}])
        self.assertNotIn("secret", json.dumps(payload))

    def test_attachments_in_payload(self):
        note = Attachment("/n.md", "text", content="persimmons")
        pic = Attachment("/p.png", "image", data=b"\x89PNG")
        first = msg("user", "what is this?")
        first.attachments = [note, pic]
        payload = build_messages(None, [first, msg("assistant", "a note"), msg("user", "and?")])
        self.assertEqual(payload[0], {
            "role": "user", "images": ["iVBORw=="],
            "content": '<file path="/n.md">\npersimmons\n</file>\n\n<image path="/p.png"/>'
                       "\n\nwhat is this?"})
        self.assertEqual(payload[2], {"role": "user", "content": "and?"})  # sent once, not per turn

        blind = build_messages(None, [first], vision=False)
        self.assertEqual(blind, [{"role": "user", "content":
                                  '<file path="/n.md">\npersimmons\n</file>\n\nwhat is this?'}])

        second = msg("user", "one more")
        second.attachments = [pic]
        merged = build_messages(None, [first, second])  # neighbours merge, images included
        self.assertEqual((len(merged), len(merged[0]["images"])), (1, 2))

    def test_make_title(self):
        self.assertEqual(make_title("  hello\n world "), "hello world")
        self.assertEqual(len(make_title("x" * 200)), 60)


class ReplTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.addCleanup(self.fake.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        env = mock.patch.dict(os.environ, {"AC_SKILLS_PATH": str(self.tmp / "skills"),
                                           "XDG_CONFIG_HOME": str(self.tmp / "config")})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.out = io.StringIO()
        self.inputs = []
        self.repl = self.make_repl(self.store.draft("m1"))

    def make_repl(self, session, **kw):
        return Repl(self.store, Client(self.fake.host), session, out=self.out,
                    input_fn=self.next_input, style=Style(False), **kw)

    def next_input(self, prompt=""):
        if not self.inputs:
            raise EOFError
        return self.inputs.pop(0)

    def run_lines(self, *lines):
        self.inputs = list(lines)
        self.repl.run()
        return self.out.getvalue()

    def contents(self, session_id=None):
        return [(m.role, m.content) for m in
                self.store.messages(session_id or self.repl.session.id)]

    # -- basics -----------------------------------------------------------

    def test_nothing_saved_until_first_message(self):
        self.run_lines("/help", "/skills", "/quit")
        self.assertEqual(self.store.list(), [])

    def test_chat_persists_and_sends_history(self):
        self.fake.reply("Hi ", "there", thinking="pondering")
        self.fake.reply("Fine")
        out = self.run_lines("hello", "how are you?")
        self.assertIn("pondering", out)
        self.assertIn("Hi there", out)
        self.assertEqual(self.contents(), [("user", "hello"), ("assistant", "Hi there"),
                                           ("user", "how are you?"), ("assistant", "Fine")])
        saved = self.store.messages(self.repl.session.id)[1]
        self.assertEqual((saved.thinking, saved.prompt_tokens, saved.eval_tokens, saved.model),
                         ("pondering", 11, 7, "m1"))
        self.assertEqual(self.store.get(self.repl.session.id).title, "hello")
        self.assertEqual(self.fake.requests[1]["messages"], [
            {"role": "user", "content": "hello"}, {"role": "assistant", "content": "Hi there"},
            {"role": "user", "content": "how are you?"}])
        self.assertIn("m1 · 18/1.0k ctx · 7 tok/s · skills: none", out)

    def test_multiline_and_escaped_slash(self):
        self.run_lines('"""first', "second", 'third"""', '"""one liner"""', "//etc/hosts is a file")
        self.assertEqual([c for r, c in self.contents() if r == "user"],
                         ["first\nsecond\nthird", "one liner", "/etc/hosts is a file"])

    def test_unknown_command_suggests(self):
        out = self.run_lines("/skils")
        self.assertIn("unknown command /skils. Did you mean /skills?", out)
        self.assertEqual(self.fake.requests, [])

    def test_resume_shows_tail_and_continues(self):
        self.run_lines("remember 42")
        session = self.store.get(self.repl.session.id)
        self.out = io.StringIO()
        self.repl = self.make_repl(session)
        out = self.run_lines("what number?")
        self.assertIn(">>> remember 42", out)
        self.assertEqual(self.fake.requests[-1]["messages"][0]["content"], "remember 42")

    # -- skills -----------------------------------------------------------

    def test_skill_attach_detach_changes_system_prompt(self):
        write_skill(self.tmp / "skills", "haiku", body="Answer only in haiku.")
        out = self.run_lines("plain", "/skill add haiku", "styled", "/context",
                             "/skill rm haiku", "plain again")
        first, second, third = (r["messages"] for r in self.fake.requests)
        self.assertEqual(first[0]["role"], "user")  # agentless: no system message at all
        self.assertEqual(second[0], {"role": "system", "content":
                                     '<skill name="haiku">\nAnswer only in haiku.\n</skill>'})
        self.assertEqual(third[0]["role"], "user")
        self.assertNotIn("haiku", json.dumps(self.contents()))  # transcript stays pure
        provenance = [m.skills for m in self.store.messages(self.repl.session.id)
                      if m.role == "assistant"]
        self.assertEqual([[k["name"] for k in p] for p in provenance], [[], ["haiku"], []])
        self.assertIn("Answer only in haiku.", out)  # /context shows the composed prompt
        self.assertEqual(self.store.get(self.repl.session.id).skills, [])

    def test_skill_errors_and_missing_skill(self):
        path = write_skill(self.tmp / "skills", "temp")
        out = self.run_lines("/skill add nope", "/skill add temp", "/skill add temp")
        self.assertIn("no skill named 'nope' (available: temp)", out)
        self.assertIn("'temp' is already attached", out)
        path.unlink()
        out = self.run_lines("hello")
        self.assertIn("skill 'temp' can't be loaded; continuing without it", out)
        self.assertEqual(self.fake.requests[-1]["messages"][0]["role"], "user")
        self.assertEqual(self.repl.session.skills, ["temp"])  # stays attached

    # -- files ------------------------------------------------------------

    def test_named_file_is_read_and_sent(self):
        note = self.tmp / "notes.md"
        note.write_text("the code word is persimmon")
        out = self.run_lines(f"what is the code word in {note}?", "and again?")
        self.assertIn(f"attached {note} (text, 26 B)", out)
        first, second = (r["messages"] for r in self.fake.requests)
        self.assertEqual(first[0]["content"], f'<file path="{note}">\nthe code word is persimmon\n'
                                              f"</file>\n\nwhat is the code word in {note}?")
        self.assertEqual(second[0]["content"], first[0]["content"])  # still in context next turn
        stored = self.store.messages(self.repl.session.id)[0]
        self.assertEqual(stored.content, f"what is the code word in {note}?")  # as typed
        self.assertEqual(stored.attachments[0].content, "the code word is persimmon")

    def test_file_is_a_snapshot_until_named_again(self):
        note = self.tmp / "notes.md"
        note.write_text("version one")
        self.run_lines(f"read {note}")
        note.write_text("version two")
        self.run_lines("what did it say?")
        self.assertIn("version one", self.fake.requests[-1]["messages"][0]["content"])
        self.assertNotIn("version two", json.dumps(self.fake.requests[-1]))
        self.run_lines(f"read {note} again")
        self.assertIn("version two", self.fake.requests[-1]["messages"][-1]["content"])

    def test_message_starting_with_a_path_is_not_a_command(self):
        note = self.tmp / "notes.md"
        note.write_text("hello")
        out = self.run_lines(f"{note} summarize this", "/nonsense/path what is this?")
        self.assertNotIn("unknown command", out)
        self.assertEqual(len(self.fake.requests), 2)
        self.assertIn("<file path=", self.fake.requests[0]["messages"][0]["content"])

    def test_unreadable_file_warns_and_still_sends(self):
        blob = self.tmp / "blob.bin"
        blob.write_bytes(b"\x00\x01")
        out = self.run_lines(f"read {blob}")
        self.assertIn("isn't text or an image", out)
        self.assertEqual(self.fake.requests[0]["messages"], [
            {"role": "user", "content": f"read {blob}"}])

    def test_images_need_a_model_with_vision(self):
        pic = self.tmp / "pic.png"
        pic.write_bytes(b"\x89PNG")
        out = self.run_lines(f"describe {pic}")
        self.assertIn("m1 can't see images; leaving 1 out", out)
        self.assertNotIn("images", self.fake.requests[0]["messages"][0])
        self.fake.capabilities.append("vision")
        self.repl.client = Client(self.fake.host)  # capabilities are cached per client
        out = self.run_lines("look again")
        self.assertEqual(self.fake.requests[1]["messages"][0]["images"], ["iVBORw=="])
        self.assertEqual(out.count("can't see images"), 1)

    def test_files_command_edit_and_exports(self):
        note = self.tmp / "notes.md"
        note.write_text("alpha ``` fence")
        out = self.run_lines("/files", f"read {note}", "/files")
        self.assertIn("no files in this conversation", out)
        self.assertIn(f"#1  {note} (text, 15 B)", out)

        note.write_text("beta")
        self.repl.editor = lambda text: text + " please"
        self.run_lines("/edit")
        (stored, _) = self.store.messages(self.repl.session.id)
        self.assertEqual((stored.content, stored.attachments[0].content),
                         (f"read {note} please", "beta"))  # editing re-reads the file

        self.run_lines(f"/export md {self.tmp / 'o.md'}", f"/export json {self.tmp / 'o.json'}")
        self.assertIn(f"<details><summary>attached: {note} (text, 4 B)</summary>",
                      (self.tmp / "o.md").read_text())
        exported = json.loads((self.tmp / "o.json").read_text())["messages"][0]["attachments"]
        self.assertEqual(exported, [{"path": str(note), "kind": "text", "bytes": 4, "note": None,
                                     "content": "beta"}])

    def test_resume_shows_what_was_attached(self):
        note = self.tmp / "notes.md"
        note.write_text("hello")
        self.run_lines(f"read {note}")
        self.out = io.StringIO()
        self.repl = self.make_repl(self.store.get(self.repl.session.id))
        self.assertIn(f"    attached {note} (text, 5 B)", self.run_lines("/quit"))

    def test_system_text(self):
        self.run_lines("/system Be terse.", "hi", "/system clear", "hi again")
        self.assertEqual(self.fake.requests[0]["messages"][0],
                         {"role": "system", "content": "Be terse."})
        self.assertEqual(self.fake.requests[1]["messages"][0]["role"], "user")

    # -- model and options ------------------------------------------------

    def test_options_and_think(self):
        self.run_lines("/set temperature 0.2", "/set num_ctx 4096", "/set seed 1", "/set seed",
                       "/think off", "/model m2", "hi")
        req = self.fake.requests[0]
        self.assertEqual((req["model"], req["think"], req["options"]),
                         ("m2:latest", False, {"temperature": 0.2, "num_ctx": 4096}))
        self.assertEqual(self.store.get(self.repl.session.id).options,
                         {"temperature": 0.2, "num_ctx": 4096, "think": False})

    def test_hidden_thinking(self):
        self.fake.reply("answer", thinking="secret")
        out = self.run_lines("/think hide", "hi")
        self.assertIn("thinking…", out)
        self.assertNotIn("secret", out)
        self.assertEqual(self.store.messages(self.repl.session.id)[1].thinking, "secret")

    def test_switch_model_mid_conversation(self):
        self.fake.loaded = ["m1"]
        out = self.run_lines("first", "/model", "/model 2", "second", "/model m1", "/model 1",
                             "/model 9")
        self.assertRegex(out, r"\* 1  m1 +0\.0 GB\n  2  m2:latest +0\.0 GB")
        self.assertIn("model set to m2:latest", out)
        self.assertIn("it isn't loaded yet", out)
        self.assertEqual(out.count("it isn't loaded yet"), 1)  # m1 is resident: no warning
        self.assertIn("already using m1", out)
        self.assertIn("no model number 9", out)
        first, second = self.fake.requests
        self.assertEqual((first["model"], second["model"]), ("m1", "m2:latest"))
        self.assertEqual([m["content"] for m in second["messages"]], ["first", "ok", "second"])
        replies = [m.model for m in self.store.messages(self.repl.session.id)
                   if m.role == "assistant"]
        self.assertEqual(replies, ["m1", "m2:latest"])  # each reply records who wrote it
        self.assertEqual(self.store.get(self.repl.session.id).model, "m1")

    def test_bad_model(self):
        out = self.run_lines("/model zzz")
        self.assertIn("model 'zzz' is not installed", out)
        self.assertEqual(self.repl.session.model, "m1")

    def test_context_warning(self):
        self.fake.prompt_tokens = 960
        out = self.run_lines("hi")
        self.assertIn("context is nearly full", out)

    # -- failures ---------------------------------------------------------

    def test_interrupt_keeps_partial_and_repl_survives(self):
        def chat(*args, **kw):
            yield "content", "partial ans"
            raise KeyboardInterrupt
        with mock.patch.object(self.repl.client, "chat", chat):
            out = self.run_lines("long question")
        self.assertIn("interrupted", out)
        saved = self.store.messages(self.repl.session.id)[1]
        self.assertEqual((saved.content, saved.status), ("partial ans", "interrupted"))
        self.fake.reply("full")
        self.run_lines("/retry")
        self.assertEqual(self.contents(), [("user", "long question"), ("assistant", "full")])

    def test_stream_error_then_retry(self):
        self.fake.scripts.append([("error", "out of memory")])
        self.fake.reply("recovered")
        out = self.run_lines("hi", "/retry")
        self.assertIn("error: out of memory", out)
        self.assertEqual(self.contents(), [("user", "hi"), ("assistant", "recovered")])

    def test_unknown_model_lists_installed(self):
        self.repl.session.model = "gone"
        out = self.run_lines("hi")
        self.assertIn("installed models: m1, m2:latest", out)

    def test_server_down(self):
        self.repl.client = Client("http://127.0.0.1:1")
        out = self.run_lines("hi", "/quit")
        self.assertIn("Is it running?", out)
        self.assertEqual(self.contents(), [("user", "hi")])

    # -- conversation editing ---------------------------------------------

    def test_undo(self):
        self.run_lines("one", "two", "/undo")
        self.assertEqual(self.contents(), [("user", "one"), ("assistant", "ok")])

    def test_edit(self):
        self.repl.editor = lambda text: text.replace("teh", "the")
        self.run_lines("fix teh typo", "/edit")
        self.assertEqual(self.contents(), [("user", "fix the typo"), ("assistant", "ok")])
        self.assertEqual(len(self.fake.requests), 2)
        self.repl.editor = lambda text: text
        out = self.run_lines("/edit")
        self.assertIn("unchanged", out)
        self.assertEqual(len(self.fake.requests), 2)

    def test_compact_is_non_destructive(self):
        self.run_lines("fact: the code is 7421", "and the colour is teal")
        original = self.repl.session.id
        self.fake.reply("The code is 7421; the colour is teal.")
        self.fake.reply("teal")
        self.run_lines("/compact", "what colour?")
        self.assertNotEqual(self.repl.session.id, original)
        self.assertEqual(len(self.contents(original)), 4)
        self.assertEqual(self.store.get(self.repl.session.id).parent_id, original)
        self.assertEqual(self.contents()[0],
                         ("user", f"{SUMMARY_HEADER}\n\nThe code is 7421; the colour is teal."))
        sent = self.fake.requests[-1]["messages"]
        self.assertEqual(len(sent), 1)  # summary and the new question travel as one user turn
        self.assertTrue(sent[0]["content"].endswith("what colour?"))

    def test_compact_failure_changes_nothing(self):
        self.run_lines("hello")
        original = self.repl.session.id
        self.fake.scripts.append([("error", "boom")])
        out = self.run_lines("/compact")
        self.assertIn("compaction abandoned", out)
        self.assertEqual(self.repl.session.id, original)
        self.assertEqual(len(self.store.list()), 1)

    # -- session management -----------------------------------------------

    def test_new_switch_rename_sessions(self):
        self.run_lines("first chat")
        first = self.repl.session.id
        self.run_lines("/new", "second chat", "/rename Second", "/sessions")
        second = self.repl.session.id
        self.assertNotEqual(first, second)
        out = self.out.getvalue()
        self.assertIn("Second", out)
        self.assertRegex(out, rf"\*\s+{second}\s+Second")
        self.assertIn("session " + first + " (2 messages)", self.run_lines(f"/switch {first}"))
        self.run_lines(f"/switch {first[:5]}")
        self.assertEqual(self.repl.session.id, first)
        self.run_lines("/sessions second")
        self.assertIn(second, self.out.getvalue().rsplit("UPDATED", 1)[1])
        self.assertNotIn(first, self.out.getvalue().rsplit("UPDATED", 1)[1])

    def test_fork(self):
        self.run_lines("one", "two")
        original = self.repl.session.id
        self.run_lines("/fork 2", "diverge")
        self.assertNotEqual(self.repl.session.id, original)
        self.assertEqual([c for _, c in self.contents()], ["one", "ok", "diverge", "ok"])
        self.assertEqual(len(self.contents(original)), 4)

    def test_delete_needs_confirmation(self):
        self.run_lines("hi")
        doomed = self.repl.session.id
        self.run_lines("/delete", "n")
        self.assertEqual(len(self.store.list()), 1)
        self.run_lines("/delete", "y")
        self.assertEqual(self.store.list(), [])
        self.assertNotEqual(self.repl.session.id, doomed)
        self.assertFalse(self.repl.session.persisted)

    def test_export(self):
        self.fake.reply("Hello!")
        target = self.tmp / "out.md"
        out = self.run_lines("hi", f"/export md {target}",
                             f"/export json {self.tmp / 'out.json'}")
        self.assertIn(f"wrote {target}", out)  # the full path, so the file can be found
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        out = self.run_lines("/export")
        default = self.tmp / f"ac-{self.repl.session.id}.md"
        self.assertTrue(default.is_file())
        self.assertIn(f"wrote {default}", out)  # default name lands in the current folder
        text = target.read_text()
        self.assertIn("## User\n\nhi", text)
        self.assertIn("## Assistant\n\nHello!", text)
        data = json.loads((self.tmp / "out.json").read_text())
        self.assertEqual([m["content"] for m in data["messages"]], ["hi", "Hello!"])

    def test_completions(self):
        write_skill(self.tmp / "skills", "haiku")
        self.repl.session.skills = ["haiku"]
        self.assertEqual(self.repl.completions("/sk"), ["/skill", "/skills"])
        self.assertEqual(self.repl.completions("/skill "), ["add", "rm"])
        self.assertEqual(self.repl.completions("/skill add h"), ["haiku"])
        self.assertEqual(self.repl.completions("/skill rm "), ["haiku"])
        self.assertEqual(self.repl.completions("/model m2"), ["m2:latest"])
        self.assertEqual(self.repl.completions("/think s"), ["show"])
        self.assertEqual(self.repl.completions("plain text"), [])
        (self.tmp / "notes.md").write_text("x")
        self.assertEqual(self.repl.completions(f"summarize {self.tmp}/no"), [f"{self.tmp}/notes.md"])
        self.assertEqual(self.repl.completions(f"{self.tmp}/no"), [f"{self.tmp}/notes.md"])
        self.assertEqual(self.repl.completions(f"/skill add {self.tmp}/sk"), [f"{self.tmp}/skills/"])
        self.assertEqual(self.repl.completions("/sk"), ["/skill", "/skills"])  # commands still win


if __name__ == "__main__":
    unittest.main()
