import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime
from pathlib import Path
from unittest import mock

from ac import pdf
from ac.ollama import Client
from ac.render import Style
from ac.repl import SUMMARY_HEADER, Repl, build_messages, make_title
from ac.store import Attachment, Message, Store
from tests.fake_ollama import FakeOllama
from tests.make_pdf import make_pdf
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
        os.environ.pop("AC_EXPORT_DIR", None)
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

    @unittest.skipUnless(pdf._has_pdfkit(), "needs macOS PDFKit")
    def test_pdf_text_reaches_the_model(self):
        report = self.tmp / "report.pdf"
        report.write_bytes(make_pdf(["Revenue grew 12 percent.", "Costs fell 3 percent."]))
        out = self.run_lines(f"what happened to costs in {report}#2?")
        self.assertIn(f"attached {report} (text, 30 B, PDF, page 2 of 2)", out)
        self.assertEqual(self.fake.requests[0]["messages"][0]["content"],
                         f'<file path="{report}" note="PDF, page 2 of 2">\n[page 2]\n'
                         f"Costs fell 3 percent.\n</file>\n\n"
                         f"what happened to costs in {report}#2?")

    def test_unreadable_file_warns_and_still_sends(self):
        blob = self.tmp / "blob.bin"
        blob.write_bytes(b"\x00\x01")
        out = self.run_lines(f"read {blob}")
        self.assertIn("isn't text, a PDF or an image", out)
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
        note.write_text("SECRET FILE BODY")
        self.run_lines(f"and now read {note}", f"/export md {self.tmp / 'o.md'}",
                       f"/export json {self.tmp / 'o.json'}")
        markdown, exported = (self.tmp / "o.md").read_text(), (self.tmp / "o.json").read_text()
        # Exports name the files that were attached, and never reproduce what was in them.
        self.assertIn(f"*attached: {note} (text, 16 B)*", markdown)
        self.assertEqual(json.loads(exported)["messages"][2]["attachments"],
                         [{"path": str(note), "kind": "text", "bytes": 16, "note": None}])
        for text in (markdown, exported):
            self.assertNotIn("SECRET FILE BODY", text)
            self.assertNotIn("beta", text)
        stored = self.store.messages(self.repl.session.id)[2]
        self.assertEqual(stored.attachments[0].content, "SECRET FILE BODY")  # still in the session

    def test_a_pattern_attaches_every_file_under_a_folder(self):
        for name, body in [("src/a.py", "A = 1"), ("src/sub/b.py", "B = 2"), ("src/sub/c.md", "sea"),
                           ("src/.hidden/d.py", "D = 4"), ("src/x.bin", None)]:
            path = self.tmp / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x00\x01" if body is None else body.encode())
        out = self.run_lines(f"what do these define? {self.tmp}/src/**", "and again?", "/files")
        self.assertIn(f"attached 3 files from {self.tmp}/src/** (13 B); /files lists them", out)
        self.assertIn("left out 1 not text", out)
        self.assertEqual(out.count("attached "), 1 + 0)  # one line for the lot, not one per file
        sent = self.fake.requests[0]["messages"][0]["content"]
        for expected in (f'<file path="{self.tmp}/src/a.py">\nA = 1', "B = 2", "sea"):
            self.assertIn(expected, sent)
        self.assertNotIn("D = 4", sent)
        self.assertEqual(self.fake.requests[1]["messages"][0]["content"], sent)  # stays in context
        for name in ("a.py", "sub/b.py", "sub/c.md"):
            self.assertIn(f"{self.tmp}/src/{name} (text", out)  # /files lists each one

        self.out = io.StringIO()
        for n in range(8):
            (self.tmp / "src" / f"more{n}.txt").write_text("x")
        self.run_lines(f"now {self.tmp}/src/**", f"/export md {self.tmp / 'o.md'}")
        exported = (self.tmp / "o.md").read_text()
        self.assertRegex(exported, r"\*attached: and \d+ more files \(\d+ B\)\*")
        self.assertLess(exported.count("*attached:"), 3 + 4 + 1)  # summarised, not 11 lines

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
        out = self.run_lines("first", "/models", "/models 2", "second", "/models m1", "/models 1",
                             "/models 9")
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

    def test_markdown_is_rendered_only_on_a_terminal(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        reply = "Use **bold** and `code`:\n\n- one\n- two\n"
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            os.environ.pop("NO_COLOR", None)
            self.out = Terminal()
            self.repl = Repl(self.store, Client(self.fake.host), self.store.draft("m1"),
                             out=self.out, input_fn=self.next_input)
            for _ in range(3):
                self.fake.reply(reply)
            shown = self.run_lines("hi")
            self.assertIn("\033[1mbold\033[0m", shown)
            self.assertIn("• one", shown)
            self.assertNotIn("**", shown)

            self.out.truncate(0)
            shown = self.run_lines("/markdown off", "again")
            self.assertIn("Use **bold** and `code`:", shown)

            with mock.patch.dict(os.environ, {"AC_MARKDOWN": "0"}):
                off = Repl(self.store, Client(self.fake.host), self.store.draft("m1"),
                           out=Terminal(), input_fn=self.next_input)
                self.assertFalse(off.markdown)
        # whatever was shown, what is stored is what the model wrote
        self.assertEqual(self.store.messages(self.repl.session.id)[1].content, reply)

    def test_piped_output_is_never_rendered(self):
        self.fake.reply("Use **bold**")
        self.assertIn("Use **bold**", self.run_lines("hi"))  # self.out is not a terminal

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
        self.assertIn("session " + first + " (2 messages)", self.run_lines(f"/sessions {first}"))
        self.run_lines("/new", f"/sessions {first[:5]}")
        self.assertEqual(self.repl.session.id, first)
        self.run_lines("/sessions second")  # an exact title, whatever its case: go there
        self.assertEqual(self.repl.session.id, second)
        listing = self.run_lines("/sessions first").rsplit("UPDATED", 1)[1]  # not a title: filter
        self.assertIn(first, listing)
        self.assertNotIn(second, listing)
        self.assertEqual(self.repl.session.id, second)

    def three_sessions(self):
        ids = []
        for text in ("first chat", "second chat", "third chat"):
            self.run_lines("/new", text)
            ids.append(self.repl.session.id)
        return ids

    def test_sessions_opens_the_picker(self):
        first, second, third = self.three_sessions()
        seen = {}

        def picker(sessions, label, **options):
            seen.update(options, ids=[s.id for s in sessions], labels=[label(s) for s in sessions])
            return sessions[2]

        self.repl.picker = picker
        out = self.run_lines("/sessions")
        self.assertEqual(seen["ids"], [third, second, first])  # everything, most recent first
        self.assertEqual((seen["title"], seen["query"]), ("Sessions", ""))
        self.assertEqual((seen["start"], seen["current"]), (0, third))  # opens on where you are
        self.assertEqual(seen["labels"][0][0], "third chat")
        self.assertIn(" · current", seen["labels"][0][1])
        self.assertNotIn("current", seen["labels"][1][1])
        self.assertIn(second, seen["labels"][1][1])  # the id is shown, for use on the command line
        self.assertEqual([s.id for s in seen["search"]("second")], [second])
        self.assertEqual(self.repl.session.id, first)
        self.assertIn(">>> first chat", out)  # and you see where you left off

    def test_sessions_can_be_deleted_from_the_picker(self):
        first, second, third = self.three_sessions()

        def picker(sessions, label, *, delete, protect, **options):
            current, other = sessions[0], sessions[1]
            self.assertIn("you are in this session", protect(current))
            self.assertIsNone(protect(other))
            delete(other)
            return None

        self.repl.picker = picker
        out = self.run_lines("/sessions")
        self.assertEqual([s.id for s in self.store.list()], [third, first])
        self.assertEqual(self.store.messages(second), [])  # its messages went with it
        self.assertEqual(self.repl.session.id, third)
        self.assertIn("stayed in this session", out)

    def test_the_picker_opens_on_the_current_session_wherever_it_is_in_the_list(self):
        first, second, third = self.three_sessions()
        seen = {}
        self.repl.picker = lambda sessions, label, **options: seen.update(options)
        self.run_lines(f"/sessions {first}", "/sessions")  # now in the oldest: last in the list
        self.assertEqual((seen["start"], seen["current"]), (2, first))
        self.run_lines("/new", "/sessions")  # a session with no messages yet isn't listed
        self.assertEqual(seen["start"], 0)

    def test_cancelling_or_choosing_the_current_session_changes_nothing(self):
        first, second, third = self.three_sessions()
        self.repl.picker = lambda sessions, label, **k: None
        self.assertIn("stayed in this session", self.run_lines("/session"))  # an alias
        self.repl.picker = lambda sessions, label, **k: sessions[0]
        self.assertIn("you are already in that session", self.run_lines("/sessions"))
        self.assertEqual(self.repl.session.id, third)

    def test_sessions_with_an_argument_goes_straight_there(self):
        first, second, third = self.three_sessions()
        self.repl.picker = lambda *a, **k: self.fail("no need to ask: the session was named")
        self.assertIn(f"session {first} (2 messages)", self.run_lines(f"/sessions {first[:5]}"))
        self.run_lines("/sessions second chat")  # an exact title
        self.assertEqual(self.repl.session.id, second)
        self.run_lines("/sessions 1")  # a number: most recent first
        self.assertEqual(self.repl.session.id, third)

    def test_sessions_with_other_text_filters_the_list(self):
        first, second, third = self.three_sessions()
        seen = {}
        self.repl.picker = lambda sessions, label, **k: seen.update(k)
        self.run_lines("/sessions chat about bread")
        self.assertEqual(seen["query"], "chat about bread")

    def test_sessions_without_a_terminal_numbers_them(self):
        first, second, third = self.three_sessions()
        out = self.run_lines("/sessions")
        self.assertRegex(out, r"1\s+\*\s+" + third + r"[\s\S]*2\s+" + second + r"[\s\S]*3\s+" + first)
        self.assertIn("/sessions N opens one of these", out)
        self.run_lines("/sessions 3")
        self.assertEqual(self.repl.session.id, first)
        self.run_lines("/sessions second", "/sessions 1")  # numbers follow the list last shown
        self.assertEqual(self.repl.session.id, second)
        self.assertIn("there is no session number 7", self.run_lines("/sessions 7"))

    def test_there_is_no_switch_command(self):
        out = self.run_lines("hi", "/switch")
        self.assertIn("unknown command /switch", out)

    def test_sessions_with_nowhere_to_go(self):
        self.repl.picker = lambda *a, **k: self.fail("nothing to choose between")
        self.assertIn("there are no other sessions yet", self.run_lines("hi", "/sessions"))

    def test_model_picker(self):
        seen = {}

        def picker(models, label, **options):
            seen.update(options, labels=[label(m) for m in models])
            return models[1]

        self.repl.picker = picker
        out = self.run_lines("/models")
        self.assertEqual((seen["title"], seen["start"], seen["current"]), ("Models", 0, "m1"))
        self.assertEqual(seen["labels"], [("m1", "0.0 GB · 1B · Q4 · current"),
                                          ("m2:latest", "0.0 GB · 1B · Q4")])
        self.assertIn("model set to m2:latest", out)
        self.assertEqual(self.repl.session.model, "m2:latest")

        self.repl.picker = lambda *a, **k: None
        self.assertIn("still using m2:latest", self.run_lines("/models"))
        self.repl.picker = lambda *a, **k: self.fail("named: no need to ask")
        self.run_lines("/models m1")
        self.assertEqual(self.repl.session.model, "m1")
        self.run_lines("/model m2")  # the singular still works, as /session does
        self.assertEqual(self.repl.session.model, "m2:latest")

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
        self.fake.reply('**"Saying Hello: A First Exchange."**\n\nI chose this because...')
        target = self.tmp / "out.md"
        out = self.run_lines("hi", f"/export md {target}",
                             f"/export json {self.tmp / 'out.json'}")
        self.assertIn(f"wrote {target}", out)  # the full path, so the file can be found
        self.assertIn("titled: Saying Hello: A First Exchange", out)
        self.assertEqual(len(self.fake.requests), 2)  # named once, not again for the 2nd export
        text = target.read_text()
        self.assertTrue(text.startswith("# Saying Hello: A First Exchange\n"))
        self.assertIn("## User\n\nhi", text)
        self.assertIn("## Assistant\n\nHello!", text)
        data = json.loads((self.tmp / "out.json").read_text())
        self.assertEqual([m["content"] for m in data["messages"]], ["hi", "Hello!"])
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        out = self.run_lines("/export")
        day = datetime.now().date().isoformat()  # the session began today, local time
        default = self.tmp / f"{day} Saying Hello A First Exchange.md"  # no ":" in filenames
        self.assertTrue(default.is_file())
        self.assertIn(f"wrote {default}", out)  # no export folder set: the current folder

    def test_title_request_is_bounded_and_ignores_the_sessions_prompt(self):
        write_skill(self.tmp / "skills", "haiku", body="Answer only in haiku.")
        note = self.tmp / "notes.md"
        note.write_text("FILE BODY " * 500)
        self.run_lines("/skill add haiku", "/system Be terse.", f"summarize {note} " + "x" * 5000,
                       *[f"question {n}" for n in range(20)])
        self.fake.reply("Notes Summary and Twenty Questions")
        self.run_lines("/title")
        (message,) = self.fake.requests[-1]["messages"]  # one user turn: no system, no skills
        self.assertEqual(message["role"], "user")
        self.assertNotIn("haiku", message["content"])
        self.assertNotIn("FILE BODY", message["content"])  # files are named, not included
        self.assertIn("[attached: notes.md]", message["content"])
        self.assertIn("question 19", message["content"])
        self.assertNotIn("question 10", message["content"])  # the middle is left out
        self.assertLess(len(message["content"]), 12 * 1300 + 1000)
        self.assertEqual(self.fake.requests[-1]["think"], False)
        self.assertEqual(self.store.get(self.repl.session.id).title,
                         "Notes Summary and Twenty Questions")

    def test_a_title_you_chose_is_kept(self):
        self.run_lines("hi", "/rename My own title", "/export json")
        self.assertEqual(len(self.fake.requests), 1)  # the model was not asked
        day = datetime.now().date().isoformat()
        cwd_file = Path(f"{day} My own title.json")
        self.addCleanup(cwd_file.unlink, missing_ok=True)
        self.assertTrue(cwd_file.is_file())
        self.fake.reply("A Better Title Perhaps")
        out = self.run_lines("/title")  # unless asked for by name
        self.assertIn("titled: A Better Title Perhaps", out)
        self.run_lines("/title Back to mine")
        got = self.store.get(self.repl.session.id)
        self.assertEqual((got.title, got.title_source), ("Back to mine", "user"))

    def test_export_still_happens_when_the_model_cannot_name_it(self):
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, Path(__file__).parent)
        self.run_lines("first words")
        day = datetime.now().date().isoformat()
        for script, reason in [([("error", "boom")], "couldn't ask the model for a title (boom)"),
                               ([("content", ' \n"" ')], "didn't offer a usable title")]:
            self.fake.scripts.append(script)
            out = self.run_lines("/export")
            self.assertIn(reason, out)
            self.assertTrue((self.tmp / f"{day} first words.md").is_file())
        self.assertEqual(self.store.get(self.repl.session.id).title_source, "auto")

    def test_two_sessions_with_one_title_do_not_overwrite_each_other(self):
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, Path(__file__).parent)
        day = datetime.now().date().isoformat()
        self.run_lines("hi", "/rename Same name", "/export")
        first = self.repl.session.id
        self.run_lines("/new", "hello", "/rename Same name", "/export", "/export")
        second = self.repl.session.id
        names = sorted(p.name for p in self.tmp.glob("*.md"))
        self.assertEqual(names, [f"{day} Same name ({second}).md", f"{day} Same name.md"])
        self.assertIn(first, (self.tmp / f"{day} Same name.md").read_text())

    def test_export_folder_from_config_and_environment(self):
        vault = self.tmp / "my vault" / "acc sessions"  # spaces, and it doesn't exist yet
        (self.tmp / "config" / "ac").mkdir(parents=True)
        (self.tmp / "config" / "ac" / "config.toml").write_text(f'export_dir = "{vault}"\n')
        self.fake.reply("Hello!")
        self.fake.reply("A Short Greeting")
        out = self.run_lines("hi", "/export", "/export json")
        name = f"{datetime.now().date().isoformat()} A Short Greeting"
        self.assertIn(f"wrote {vault / name}.md", out)
        self.assertIn("## Assistant\n\nHello!", (vault / f"{name}.md").read_text())
        self.assertTrue((vault / f"{name}.json").is_file())

        self.run_lines("more", "/export")  # same session, same file: updated, not duplicated
        self.assertEqual(len(list(vault.glob("*.md"))), 1)
        self.assertIn("more", (vault / f"{name}.md").read_text())

        explicit = self.tmp / "elsewhere.md"
        self.run_lines(f"/export md {explicit}")  # a named file is used as given
        self.assertTrue(explicit.is_file())

        with mock.patch.dict(os.environ, {"AC_EXPORT_DIR": str(self.tmp / "from-env")}):
            self.run_lines("/export")
        self.assertTrue((self.tmp / "from-env" / f"{name}.md").is_file())

    def test_exports_use_the_names_from_the_config(self):
        (self.tmp / "config" / "ac").mkdir(parents=True)
        (self.tmp / "config" / "ac" / "config.toml").write_text(
            'user_name = "Sam"\nassistant_name = "Robin"\n')
        self.fake.reply("Hello!")
        self.fake.reply("A Greeting")
        def chat(*args, **kw):
            yield "content", "partial"
            raise KeyboardInterrupt
        self.run_lines("hi")
        with mock.patch.object(self.repl.client, "chat", chat):
            self.run_lines("again")
        self.run_lines(f"/export md {self.tmp / 'o.md'}", f"/export json {self.tmp / 'o.json'}")
        text = (self.tmp / "o.md").read_text()
        self.assertIn("## Sam\n\nhi", text)
        self.assertIn("## Robin\n\nHello!", text)
        self.assertIn("## Robin (interrupted)\n\npartial", text)
        self.assertNotRegex(text, r"## (User|Assistant)")
        data = json.loads((self.tmp / "o.json").read_text())
        self.assertEqual(data["names"], {"user": "Sam", "assistant": "Robin"})
        self.assertEqual([m["role"] for m in data["messages"]][:2], ["user", "assistant"])
        # Labels for the reader only: the model is never told who it is supposed to be.
        self.assertNotIn("Robin", json.dumps(self.fake.requests))
        self.assertNotIn("Sam", json.dumps(self.fake.requests))

    def test_export_problems_are_reported_not_fatal(self):
        (self.tmp / "config" / "ac").mkdir(parents=True)
        (self.tmp / "config" / "ac" / "config.toml").write_text("export_dir = [broken\n")
        blocker = self.tmp / "a-file"
        blocker.write_text("")
        err = io.StringIO()
        with redirect_stderr(err), mock.patch.dict(os.environ, {"AC_EXPORT_DIR": ""}):
            out = self.run_lines("hi", f"/export md {blocker}/x.md", "still alive")
        self.assertIn(f"error: can't write {blocker}/x.md: ", out)
        self.assertEqual(len(self.fake.requests), 3)  # hi, the title, still alive
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        with redirect_stderr(err):
            self.run_lines("/export")  # broken config: warned about, then ignored
        self.assertIn("ignoring", err.getvalue())
        self.assertEqual(len(list(self.tmp.glob("????-??-?? ok.md"))), 1)

    def test_completions(self):
        write_skill(self.tmp / "skills", "haiku")
        self.repl.session.skills = ["haiku"]
        self.assertEqual(self.repl.completions("/sk"), ["/skill", "/skills"])
        self.assertEqual(self.repl.completions("/skill "), ["add", "rm"])
        self.assertEqual(self.repl.completions("/skill add h"), ["haiku"])
        self.assertEqual(self.repl.completions("/skill rm "), ["haiku"])
        self.assertEqual(self.repl.completions("/models m2"), ["m2:latest"])
        self.assertEqual(self.repl.completions("/model m2"), ["m2:latest"])  # the alias completes too
        self.assertEqual(self.repl.completions("/mo"), ["/models"])  # but only one name is offered
        self.assertEqual(self.repl.completions("/se"), ["/sessions", "/set"])
        self.assertEqual(self.repl.completions("/q"), ["/quit"])
        self.assertEqual(self.repl.completions("/think s"), ["show"])
        self.assertEqual(self.repl.completions("plain text"), [])
        (self.tmp / "notes.md").write_text("x")
        self.assertEqual(self.repl.completions(f"summarize {self.tmp}/no"), [f"{self.tmp}/notes.md"])
        self.assertEqual(self.repl.completions(f"{self.tmp}/no"), [f"{self.tmp}/notes.md"])
        self.assertEqual(self.repl.completions(f"/skill add {self.tmp}/sk"), [f"{self.tmp}/skills/"])
        self.assertEqual(self.repl.completions("/sk"), ["/skill", "/skills"])  # commands still win


if __name__ == "__main__":
    unittest.main()
