import io
import json
import os
import re
import shutil
import tempfile
import unittest
from collections import namedtuple
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
        os.environ.pop("AC_CONFIG_DIR", None)
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

    def test_trailing_backslash_continues_the_line(self):
        self.run_lines("first\\", "\\", "second\\", "third", "single")
        self.assertEqual([c for r, c in self.contents() if r == "user"],
                         ["first\n\nsecond\nthird", "single"])

    def test_pasted_lines_are_one_message(self):
        # A paste arrives at once: lines still waiting after one is read belong to it.
        # waiting[i]: whether the line after the i-th one read came with it.
        waiting = [True, True, False, False, False, True, False]
        self.repl.pasting = lambda: waiting.pop(0)
        self.run_lines("pasted", "", "block", "typed", '"""open', "pasted", 'inside"""')
        self.assertEqual([c for r, c in self.contents() if r == "user"],
                         ["pasted\n\nblock", "typed", "open\npasted\ninside"])

    def test_every_alias_is_listed_in_help_and_nowhere_else(self):
        aliases = {n[4:] for n in dir(Repl) if n.startswith("cmd_")} - set(Repl.command_names())
        self.assertEqual(aliases, {"file", "session", "model", "skill", "ls", "rm", "q", "exit"})
        shown = self.run_lines("/help")
        line = next(l + shown.split(l)[1].split("\n")[1] for l in shown.split("\n")
                    if l.startswith("Shorter names"))
        for alias in aliases:
            self.assertIn(f"/{alias}", line)       # discoverable...
            self.assertNotIn(f"/{alias}", self.repl.completions(f"/{alias[:1]}"))  # ...not offered

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

    def pick_skills(self, act, line="/skills"):
        """Run /skills with a stand-in picker: act(rows, options) plays the user."""
        seen = {}

        def picker(rows, label, **options):
            seen.update(options)
            return act(rows, dict(options, label=label))

        self.repl.picker = picker
        return self.run_lines(line), seen

    def test_skills_opens_a_list_to_attach_and_detach_from(self):
        write_skill(self.tmp / "skills", "haiku", description="Poetry mode")
        write_skill(self.tmp / "skills", "terse", description="Few words")
        self.run_lines("hi")
        said = []

        def act(rows, options):
            labels = lambda: [options["label"](r) for r in rows]
            said.append(labels())
            said.append(options["toggle"](rows[0]))
            said.append(options["toggle"](rows[1]))
            said.append(options["toggle"](rows[0]))
            said.append(([options["marked"](r) for r in rows], labels()))

        out, seen = self.pick_skills(act)
        self.assertEqual((seen["title"], seen["toggle_label"]), ("Skills", "attaches or detaches"))
        self.assertEqual(said[0], [("haiku", "Poetry mode"), ("terse", "Few words")])
        self.assertEqual(said[1:4], ["attached haiku", "attached terse", "detached haiku"])
        self.assertEqual(said[4], ([False, True], [("haiku", "Poetry mode"),
                                                   ("terse", "attached · Few words")]))
        self.assertIn("attached: terse", out)
        self.assertEqual(self.store.get(self.repl.session.id).skills, ["terse"])  # saved
        out, _ = self.pick_skills(lambda rows, options: None)
        self.assertIn("nothing changed · attached: terse", out)

    def test_skills_lists_what_is_attached_from_elsewhere_or_gone(self):
        write_skill(self.tmp / "skills", "haiku")
        gone = write_skill(self.tmp / "skills", "temp")
        style = self.tmp / "notes" / "style.md"
        style.parent.mkdir()
        style.write_text("---\ndescription: House style\n---\nBe plain.")
        self.repl.picker = None
        self.run_lines(f"/skills {style}", "/skills temp")  # a name or a path attaches it
        self.assertEqual(self.repl.session.skills, [str(style), "temp"])
        gone.unlink()
        said = []

        def act(rows, options):
            said.append([options["label"](r) for r in rows])
            said.append(options["toggle"](rows[2]))  # detaching what can't be loaded works...
            said.append(options["toggle"](rows[2]))  # ...attaching it again can't
            said.append(options["toggle"](rows[1]))
            said.append(options["toggle"](rows[1]))  # a path can be put back: its row stays

        self.pick_skills(act)
        self.assertEqual(said[0], [("haiku", "A skill"),
                                   ("style", f"attached · {style} · House style"),
                                   ("temp", "attached · can't be loaded")])
        self.assertEqual(said[1], "detached temp")
        self.assertIn("no skill named 'temp'", said[2])
        self.assertEqual(said[3:], ["detached style", "attached style"])
        self.assertEqual(self.repl.session.skills, [str(style)])

    def test_skills_with_other_text_filters_the_list(self):
        write_skill(self.tmp / "skills", "haiku", description="Poetry mode")
        out, seen = self.pick_skills(lambda rows, options: None, "/skills poet")
        self.assertEqual(seen["query"], "poet")
        self.assertNotIn("error", out)
        self.repl.picker = None  # with no list to filter, it can only be a wrong name
        self.assertIn("no skill named 'poet' (available: haiku)", self.run_lines("/skills poet"))
        self.assertEqual(self.repl.session.skills, [])

    def test_skills_without_a_terminal_prints_the_list(self):
        self.repl.picker = lambda *a, **k: self.fail("there is nothing to list")
        self.assertIn("no skills found. Create", self.run_lines("/skills"))
        write_skill(self.tmp / "skills", "haiku", description="Poetry mode")
        write_skill(self.tmp / "skills", "terse", description="Few words")
        self.repl.picker = None
        out = self.run_lines("/skills add terse nope", "/skills terse", "/skills")
        self.assertIn("no skill named 'nope'", out)  # all or nothing
        self.assertRegex(out, r"\n  haiku +Poetry mode\n\* terse +Few words\n")
        self.assertIn("/skills rm NAME detaches it", out)

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
        self.assertIn("no files yet", out)
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

    def vault(self):
        folder = self.tmp / "Mobile Documents" / "my vault" / "mom"
        folder.mkdir(parents=True)
        (folder / "visit.md").write_text("visit on sunday")
        (folder / "recipe.md").write_text("plum cake")
        (folder / "photo.png").write_bytes(b"\x89PNG")
        (self.tmp / "plan.txt").write_text("the plan")
        return folder

    def test_files_takes_a_path_with_spaces_unquoted(self):
        folder = self.vault()
        out = self.run_lines(f"/files {folder}/*.md", "what is planned?")
        self.assertIn(f"queued 2 files from {folder}/*.md (24 B)", out)
        self.assertIn("they go with your next message", out)
        self.assertEqual(len(self.fake.requests), 1)  # /files itself sends nothing
        sent = self.fake.requests[0]["messages"][0]["content"]
        self.assertIn("visit on sunday", sent)
        self.assertIn("plum cake", sent)
        self.assertTrue(sent.endswith("what is planned?"))
        self.run_lines("and then?")
        self.assertEqual(len(self.store.messages(self.repl.session.id)[2].attachments), 0)  # once

    def test_several_paths_in_one_command(self):
        folder = self.vault()
        escaped = str(folder / "recipe.md").replace(" ", "\\ ")
        # unquoted with spaces, a plain path, an escaped one and a quoted one, all at once
        out = self.run_lines(f'/files {folder}/visit.md {self.tmp}/plan.txt {escaped} '
                             f'"{folder}/photo.png"')
        self.assertEqual([Path(a.path).name for a in self.repl.queued],
                         ["visit.md", "plan.txt", "recipe.md", "photo.png"])
        self.assertIn("they go with your next message", out)
        self.assertEqual(self.store.resources(), {})  # no @NAME, no name

    def test_one_bad_path_queues_nothing(self):
        folder = self.vault()
        out = self.run_lines(f"/files {folder}/visit.md /no/such/file.md {self.tmp}/plan.txt @trip")
        self.assertIn("error: nothing matches /no/such/file.md", out)
        self.assertIn("usage: /files PATH... [@NAME]   (nothing was queued)", out)
        self.assertEqual((self.repl.queued, self.store.resources()), ([], {}))

    def test_a_last_argument_with_an_at_names_the_paths(self):
        folder = self.vault()
        out = self.run_lines(f"/files {folder}/*.md {self.tmp}/plan.txt @Trip")
        self.assertIn(f"@trip now means {folder}/*.md, {self.tmp}/plan.txt (3 files right now)", out)
        self.assertIn("write @trip in a message to attach them, or /files @trip to queue them", out)
        self.assertEqual(self.repl.queued, [])  # naming is only naming
        self.assertNotIn("queued", out)
        self.assertEqual(self.store.resources(), {"trip": [f"{folder}/*.md", f"{self.tmp}/plan.txt"]})

        (folder / "new.md").write_text("added later")
        out = self.run_lines("/new", "what does @trip say? and @Trip again")
        self.assertIn("attached 4 files from @trip", out)  # read afresh, in another session, once
        sent = self.fake.requests[-1]["messages"][0]["content"]
        for expected in ("added later", "the plan", "plum cake"):
            self.assertIn(expected, sent)
        self.assertEqual(self.store.messages(self.repl.session.id)[0].content,
                         "what does @trip say? and @Trip again")  # stored as typed

        out = self.run_lines("/files @trip", "/files trip", "/files")
        self.assertIn("queued 4 files from @trip", out)
        self.assertIn("that is already queued", out)
        self.assertRegex(out, r"  @trip\s+" + str(folder) + r"/\*\.md\n\s+" + str(self.tmp) + "/plan.txt")

    def test_defining_a_name_does_not_send_its_files_with_an_unrelated_message(self):
        folder = self.vault()
        self.run_lines(f"/files {folder}/*.md @mom", "what is the capital of France?")
        self.assertEqual(self.fake.requests[0]["messages"],
                         [{"role": "user", "content": "what is the capital of France?"}])
        self.run_lines("and what did @mom plan?")  # only when you ask for them
        self.assertIn("visit on sunday", self.fake.requests[1]["messages"][-1]["content"])

    def test_only_an_at_makes_a_name(self):
        folder = self.vault()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, Path(__file__).parent)
        out = self.run_lines(f"/files {folder}/visit.md plan.txt")  # a last word is just a path
        self.assertEqual([Path(a.path).name for a in self.repl.queued], ["visit.md", "plan.txt"])
        self.assertEqual(self.store.resources(), {})
        out = self.run_lines("/files clear", f"/files {folder}/visit.md summarize")
        self.assertIn("error: nothing matches summarize", out)  # not silently made a name
        self.assertEqual((self.repl.queued, self.store.resources()), ([], {}))

    def test_an_at_that_is_not_last_is_an_existing_name(self):
        folder = self.vault()
        self.run_lines(f"/files {folder}/*.md @mom", "/files clear",
                       f"/files @mom {self.tmp}/plan.txt @all", "/files clear")
        self.assertEqual(self.store.resources()["all"], [f"{folder}/*.md", f"{self.tmp}/plan.txt"])
        self.run_lines(f"/files {self.tmp}/plan.txt @mom", "/files clear")  # naming again moves it
        self.assertEqual(self.store.resources()["mom"], [f"{self.tmp}/plan.txt"])
        self.assertEqual(self.store.resources()["all"][0], f"{folder}/*.md")  # @all kept its paths

    def test_a_name_beats_a_file_of_the_same_name_and_single_files_can_be_named(self):
        folder = self.vault()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, Path(__file__).parent)
        (self.tmp / "plan").write_text("a file called plan")
        out = self.run_lines(f"/files {folder}/visit.md @plan", "/files @plan", "/files clear",
                             "read @plan")
        self.assertIn("(1 file right now)", out)
        self.assertIn(f"queued {folder}/visit.md (text, 15 B)", out)  # one file: no "1 files from"
        sent = self.fake.requests[-1]["messages"][0]["content"]
        self.assertIn("visit on sunday", sent)
        self.assertNotIn("a file called plan", sent)

    def test_files_problems(self):
        folder = self.vault()
        out = self.run_lines("/files /no/such/thing", f"/files {folder}/*.md @bad/name",
                             f"/files {folder}/*.md @clear", f"/files {folder}/photo.png @pic",
                             "/files forget nothing", f"/files {folder}/*.md @mom", "/files @nobody")
        self.assertIn("error: nothing matches /no/such/thing", out)
        self.assertIn("'bad/name' can't be a name", out)
        self.assertIn("'clear' can't be a name", out)
        self.assertIn("there is no @nothing", out)
        self.assertIn("error: nothing matches @nobody", out)
        self.assertIn("m1 can't see images", self.run_lines("look at @pic"))  # named image: sent as one

        shutil.rmtree(folder)
        out = self.run_lines("what about @mom ?", "/files mom")
        self.assertEqual(out.count(f"@mom includes {folder}/*.md, which isn't there now"), 2)
        self.assertIn("forgot @mom", self.run_lines("/files forget @mom"))
        self.assertEqual(self.store.resources(), {"pic": [f"{folder}/photo.png"]})

    def test_a_name_with_one_path_gone_still_gives_the_rest(self):
        folder = self.vault()
        self.run_lines(f"/files {folder}/visit.md {self.tmp}/plan.txt @both", "/files clear")
        (self.tmp / "plan.txt").unlink()
        out = self.run_lines("read @both")
        self.assertIn(f"@both includes {self.tmp}/plan.txt, which isn't there now", out)
        self.assertIn("visit on sunday", self.fake.requests[-1]["messages"][0]["content"])

    def test_queued_files_do_not_follow_you_to_another_session(self):
        folder = self.vault()
        out = self.run_lines("first", f"/files {folder}/visit.md", "/new", "hello")
        self.assertIn("dropped 1 queued attachments", out)
        self.assertEqual(self.store.messages(self.repl.session.id)[0].attachments, [])

    def test_a_file_queued_and_named_in_the_message_is_sent_once(self):
        folder = self.vault()
        self.run_lines(f"/files {folder}/visit.md", f'compare with "{folder}/visit.md"')
        self.assertEqual(self.fake.requests[0]["messages"][0]["content"].count("<file "), 1)

    def test_files_alone_shows_everything_in_one_place(self):
        folder = self.vault()
        out = self.run_lines(f'read "{folder}/visit.md"', f"/files {folder}/*.md @mom",
                             "/files @mom", "/files")
        listing = out.rsplit("they go with your next message", 1)[1]
        self.assertRegex(listing, r"in this conversation.*\n  #1  " + str(folder) + r"/visit.md \(text")
        self.assertRegex(listing, r"queued for your next message:\n.*2 files from @mom")
        self.assertRegex(listing, r"names.*\n  @mom\s+" + str(folder))
        self.assertIn("unknown command /attach", self.run_lines("/attach x"))  # one command, not two
        self.run_lines("/file clear")  # the singular works, as /session and /model do
        self.assertEqual(self.repl.queued, [])

    def work_in(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        cwd = os.getcwd()
        os.chdir(folder)
        self.addCleanup(os.chdir, cwd)

    def browse(self, act, here=None):
        """Run /files with a stand-in picker, from the folder here (an empty one by default):
        act(rows, options) returns the chosen row."""
        self.work_in(here or self.tmp / "empty")
        seen = {}

        def picker(rows, label, **options):
            seen.update(options, rows=rows, labels=[label(r) for r in rows])
            return act(rows, options)

        self.repl.picker = picker
        out = self.run_lines("/files")
        return out, seen

    def test_files_opens_a_list_of_everything(self):
        folder = self.vault()
        self.repl.picker = None
        self.run_lines(f'read "{folder}/visit.md"', f"/files {folder}/recipe.md @cake",
                       "/files @cake")
        out, seen = self.browse(lambda rows, options: None)
        self.assertEqual([(r.kind, r.title) for r in seen["rows"]],
                         [("attached", "visit.md"), ("queued", "recipe.md"), ("name", "@cake")])
        self.assertEqual(seen["labels"][0], ("visit.md", f"message #1 · text, 15 B · {folder}"))
        self.assertEqual(seen["labels"][1][1], f"queued · text, 9 B · {folder}")
        self.assertEqual(seen["labels"][2], ("@cake", f"name · {folder}/recipe.md"))
        self.assertEqual((seen["title"], seen["delete_label"]), ("Files", "remove"))
        for row in seen["rows"]:  # nobody should think a file on disk is about to be deleted
            question, done = seen["wording"](row)
            expected = {"attached": "not from disk", "queued": "Unqueue", "name": "its files stay"}
            self.assertIn(expected[row.kind], question)
            self.assertLess(len(question + " · any other key keeps it"), 100)  # fits a terminal
            self.assertNotIn("delete", (question + done).lower())

    def test_removing_a_file_takes_it_out_of_the_conversation_not_off_the_disk(self):
        folder = self.vault()
        self.run_lines(f'read "{folder}/visit.md" and "{folder}/recipe.md"')
        self.assertIn("visit on sunday", self.fake.requests[-1]["messages"][0]["content"])
        self.browse(lambda rows, options: options["delete"](rows[0]))
        self.run_lines("and now?")
        sent = self.fake.requests[-1]["messages"][0]["content"]
        self.assertNotIn("visit on sunday", sent)  # no longer costs context
        self.assertIn("plum cake", sent)           # the other file is still there
        self.assertTrue((folder / "visit.md").is_file())
        message = self.store.messages(self.repl.session.id)[0]
        self.assertEqual([Path(a.path).name for a in message.attachments], ["recipe.md"])
        self.assertIn("visit.md", message.content)  # the message itself is untouched

    def test_removing_queued_files_and_names_from_the_list(self):
        folder = self.vault()
        self.repl.picker = None
        self.run_lines(f"/files {folder}/*.md @mom", "/files @mom")

        def act(rows, options):
            for row in rows:
                if row.kind in ("queued", "name") and row.title != "visit.md":
                    options["delete"](row)

        self.browse(act)
        self.assertEqual([Path(a.path).name for a in self.repl.queued], ["visit.md"])
        self.assertEqual(self.store.resources(), {})

    def test_enter_queues_a_fresh_copy(self):
        folder = self.vault()
        self.run_lines(f'read "{folder}/visit.md"')
        (folder / "visit.md").write_text("visit moved to monday")
        self.browse(lambda rows, options: options["toggle"](rows[0]) and None)
        self.assertEqual(self.repl.queued[0].path, str(folder / "visit.md"))
        self.assertEqual(self.repl.queued[0].content, "visit moved to monday")

        self.repl.picker = None
        self.run_lines("/files clear", f"/files {folder}/*.md @mom", "/files clear")
        out, _ = self.browse(lambda rows, options: options["toggle"](
            next(r for r in rows if r.kind == "name")) and None)
        self.assertIn("queued 2 files from @mom", out)

    def test_enter_picks_several_and_unqueues_what_is_marked(self):
        folder = self.vault()
        self.repl.picker = None
        self.run_lines(f"/files {folder}/*.md @mom")
        said = []

        def act(rows, options):
            name = next(r for r in rows if r.kind == "name")
            self.assertFalse(options["marked"](name))
            said.append(options["toggle"](name))
            self.assertTrue(options["marked"](name))
            said.append(options["toggle"](name))       # Enter again takes it back out
            self.assertFalse(options["marked"](name))
            said.append(options["toggle"](name))
            self.assertEqual(options["toggle_label"], "(un)queues")

        out, _ = self.browse(act)
        self.assertEqual(said, ["queued 2 files from @mom (24 B)", "unqueued @mom",
                                "queued 2 files from @mom (24 B)"])
        self.assertEqual(sorted(Path(a.path).name for a in self.repl.queued),
                         ["recipe.md", "visit.md"])
        self.assertIn("they go with your next message", out)

        def unqueue_one(rows, options):
            options["toggle"](next(r for r in rows if r.kind == "queued"))

        out, _ = self.browse(unqueue_one)
        self.assertEqual(len(self.repl.queued), 1)
        self.assertIn("it goes with your next message", out)
        out, _ = self.browse(lambda rows, options: None)
        self.assertIn("nothing changed", out)

    def test_files_with_nothing_to_show_just_explains(self):
        self.work_in(self.tmp / "empty")
        self.repl.picker = lambda *a, **k: self.fail("there is nothing to list")
        self.assertIn("no files yet", self.run_lines("/files"))

    def test_files_offers_one_folder_at_a_time(self):
        here = self.tmp / "project"
        (here / "src" / "lib").mkdir(parents=True)
        (here / "src" / "main.py").write_text("print('hi')")
        (here / "notes.md").write_text("todo")
        (here / ".secret").write_text("no")
        (here / "node_modules").mkdir()
        (here / "node_modules" / "dep.js").write_text("no")
        seen = []

        def act(rows, options):
            seen.append(([(r.kind, r.title) for r in rows], options))
            step = len(seen)
            if step == 1:                       # the top: pick a file, then go into src/
                notes = next(r for r in rows if r.title == "notes.md")
                self.assertEqual(options["toggle"](notes), f"queued {here}/notes.md (text, 4 B)")
                return next(r for r in rows if r.title == "src/")
            if step == 2:                       # inside src/: pick one more, then back up
                options["toggle"](next(r for r in rows if r.title == "main.py"))
                return rows[0]
            return None                         # back at the top: done

        out, _ = self.browse(act, here=here)
        top, inside, back = seen
        self.assertEqual(top[0], [("dir", "src/"), ("here", "notes.md")])
        self.assertEqual(inside[0], [("up", "../"), ("dir", "lib/"), ("here", "main.py")])
        self.assertEqual((top[1]["title"], inside[1]["title"]), ("Files", "Files · src/"))
        row = namedtuple("Row", "kind")
        self.assertTrue(top[1]["opens"](row("dir")))
        self.assertFalse(top[1]["opens"](row("here")))  # a file is queued, the list stays open
        self.assertEqual(top[1]["protect"](row("here")), "not attached: Enter queues it")
        self.assertEqual(back[0], [("queued", "notes.md"), ("queued", "main.py"), ("dir", "src/"),
                                   ("here", "notes.md")])
        self.assertEqual(back[1]["start"], 2)   # the cursor comes back to src/
        self.assertEqual([a.content for a in self.repl.queued], ["todo", "print('hi')"])
        self.assertIn("they go with your next message", out)

    def test_enter_on_a_folder_queues_all_of_it(self):
        here = self.tmp / "project"
        (here / "src" / "lib").mkdir(parents=True)
        (here / "src" / "main.py").write_text("print('hi')")
        (here / "src" / "lib" / "util.py").write_text("x = 1")
        (here / "empty").mkdir()
        said = []

        def act(rows, options):
            src, empty = (next(r for r in rows if r.title == t) for t in ("src/", "empty/"))
            said.append(options["toggle"](src))
            self.assertTrue(options["marked"](src))
            said.append(options["toggle"](empty))
            self.assertFalse(options["marked"](empty))

        seen = {}

        def picker(rows, label, **options):
            seen["label"] = label
            act(rows, options)

        self.work_in(here)
        self.repl.picker = picker
        out = self.run_lines("/files")
        self.assertEqual(said, ["queued 2 files from src/ (16 B)", "nothing in empty/ to queue"])
        self.assertEqual(sorted(a.content for a in self.repl.queued), ["print('hi')", "x = 1"])
        self.assertIn("queued 2 files from src/", out)
        dir_row = namedtuple("Row", "kind key title detail item")("dir", "", "src/", "folder",
                                                                  here / "src")
        self.assertEqual(seen["label"](dir_row), ("src/", "folder · queued 2 files"))

    def test_files_arguments_complete_from_the_current_folder(self):
        self.work_in(self.tmp / "project")
        Path("notes.md").write_text("todo")
        Path("src").mkdir()
        Path("src/main.py").write_text("")
        self.store.set_resource("nora", ["/x/nora.md"])
        self.assertEqual(self.repl.completions("/files no"), ["nora", "notes.md"])
        self.assertEqual(self.repl.completions("/files notes.md s"), ["src/"])
        self.assertEqual(self.repl.completions("/files src/ma"), ["src/main.py"])
        self.assertEqual(self.repl.completions("/files cl"), ["clear"])
        self.assertEqual(self.repl.completions("/files forget n"), ["nora"])
        self.assertEqual(self.repl.completions("tell me about no"), [])  # a message needs a ./ or @

    def test_names_complete(self):
        self.store.set_resource("mom", ["/x/mom/*.md"])
        self.store.set_resource("money", ["/x/money.md"])
        self.assertEqual(self.repl.completions("/files mo"), ["mom", "money"])
        self.assertEqual(self.repl.completions("/files @mon"), ["@money"])
        self.assertEqual(self.repl.completions("what does @mo"), ["@mom", "@money"])
        (self.tmp / "data").mkdir()
        self.assertEqual(self.repl.completions(f"/files {self.tmp}/da"), [f"{self.tmp}/data/"])

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

    # -- status bar -------------------------------------------------------

    class Bar:
        """A stand-in StatusBar: records what it is told to show, and when it is open."""

        def __init__(self):
            self.open_now, self.shown, self.events = False, [], []

        def open(self):
            self.open_now = True
            self.events.append("open")

        def close(self):
            self.open_now = False
            self.events.append("close")

        def draw(self, paint):
            self.shown.append(paint(100).strip())

        def suspended(self):
            from contextlib import contextmanager

            @contextmanager
            def lend():
                self.close()
                yield
                self.open()
            return lend()

    def test_status_bar_follows_the_reply_and_replaces_the_printed_line(self):
        self.repl.bar = bar = self.Bar()
        self.fake.reply("Hi ", "there", thinking="pondering")
        out = self.run_lines("hello")
        self.assertNotIn("tok/s · skills: none", out)         # the bar has the numbers now
        self.assertEqual(bar.shown[0], "new session · m1 · no skills")
        self.assertRegex(bar.shown[1], r"^hello · m1 · no skills +waiting for m1…$")
        self.assertTrue(any(s.endswith("thinking 0s") for s in bar.shown))
        self.assertTrue(any(re.search(r"\d tokens · \d+ tok/s$", s) for s in bar.shown))
        self.assertTrue(bar.shown[-1].endswith("18/1.0k ▮▯▯▯▯▯▯▯ 2% · 7 tok/s"))
        self.assertTrue(bar.shown[-1].startswith("hello · m1 · no skills"))
        self.assertEqual(bar.events, ["open", "close"])       # opened for the run, closed after

    def test_status_bar_shows_skills_queued_files_and_a_resumed_sessions_usage(self):
        write_skill(self.tmp / "skills", "concise", "Be brief.")
        note = self.tmp / "n.md"
        note.write_text("x")
        self.repl.bar = bar = self.Bar()
        self.run_lines("hello", "/skills concise", f"/files {note}")
        self.assertRegex(bar.shown[-1], r"· m1 · concise · 1 file queued +18/1\.0k")

        resumed = self.make_repl(self.store.get(self.repl.session.id))
        resumed.bar = bar = self.Bar()
        self.repl = resumed
        out = self.run_lines("/context")
        self.assertTrue(bar.shown[0].endswith("18/1.0k ▮▯▯▯▯▯▯▯ 2%"))  # known before any reply
        self.assertIn("usage    18 of 1.0k tokens", out)

    def test_progress_counts_each_phase_from_its_first_token(self):
        from ac.repl import _Progress
        self.repl.bar, notes = self.Bar(), []
        self.repl.refresh = lambda note=None: notes.append(note)
        clock = [100.0]
        with mock.patch("ac.repl.time.monotonic", lambda: clock[0]):
            progress = _Progress(self.repl, "m1")
            clock[0] += 30                      # the model loads: not thinking time
            progress.tick("thinking")
            clock[0] += 4.4
            progress.tick("thinking")
            clock[0] += 1
            progress.tick("content")
            clock[0] += 0.5
            progress.tick("content")
        self.assertEqual(notes, ["waiting for m1…", "thinking 0s", "thinking 4s",
                                 "1 tokens · 0 tok/s", "2 tokens · 4 tok/s"])

    def test_the_editor_gets_the_whole_screen(self):
        self.repl.bar = bar = self.Bar()
        self.repl.editor = lambda text: (bar.events.append(f"editing while open={bar.open_now}"),
                                         text + "!")[1]
        self.run_lines("hi", "/edit")
        self.assertEqual(bar.events, ["open", "close", "editing while open=False", "open", "close"])
        self.assertEqual(self.contents()[0], ("user", "hi!"))

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

        def picker(sessions, label, *, delete, wording, **options):
            self.assertEqual([options["protect"](s) for s in sessions], [None] * 3)  # all fair game
            other = sessions[1]
            self.assertEqual(wording(other), ("Delete “second chat”? y deletes it for good",
                                              "deleted “second chat”"))
            delete(other)
            return None

        self.repl.picker = picker
        out = self.run_lines("/sessions")
        self.assertEqual([s.id for s in self.store.list()], [third, first])
        self.assertEqual(self.store.messages(second), [])  # its messages went with it
        self.assertEqual(self.repl.session.id, third)
        self.assertIn("stayed in this session", out)

    def test_deleting_the_current_session_moves_you_to_a_new_one_and_keeps_the_list_open(self):
        first, second, third = self.three_sessions()
        self.repl.session.model = "m2:latest"
        self.store.save(self.repl.session)
        self.repl.queued = ["something queued for the session about to go"]
        during = {}

        def picker(sessions, label, *, delete, wording, **options):
            current = sessions[0]
            question, done = wording(current)
            self.assertTrue(question.startswith("Delete the session you are in (you'll continue "
                                                "in a new one)? y deletes"))
            self.assertTrue(done.startswith("you are in a new session now"))
            shown_before = self.out.getvalue()
            delete(current)
            # The list is still open here: nothing may be printed over it...
            self.assertEqual(self.out.getvalue(), shown_before)
            during.update(session=self.repl.session, labels=[label(s)[1] for s in sessions[1:]])
            delete(sessions[1])  # ...and it can go on being used
            return None

        self.repl.picker = picker
        out = self.run_lines("/sessions")
        self.assertEqual([s.id for s in self.store.list()], [first])
        fresh = self.repl.session
        self.assertIs(fresh, during["session"])
        self.assertEqual((fresh.persisted, fresh.model, fresh.skills), (False, "m2:latest", []))
        self.assertNotIn(fresh.id, (first, second, third))
        self.assertEqual(self.repl.queued, [])
        self.assertFalse(any("current" in detail for detail in during["labels"]))
        self.assertIn("deleted the session you were in (“third chat”)", out)
        self.assertIn(f"session {fresh.id} (new) · m2:latest", out)
        self.assertNotIn("stayed in this session", out)

        self.run_lines("hello again")  # and the new session works
        self.assertEqual(self.fake.requests[-1]["messages"], [{"role": "user", "content": "hello again"}])
        self.assertEqual(len(self.store.list()), 2)

    def test_after_deleting_the_current_session_you_can_still_pick_another(self):
        first, second, third = self.three_sessions()

        def picker(sessions, label, *, delete, **options):
            delete(sessions[0])
            return sessions[2]

        self.repl.picker = picker
        out = self.run_lines("/sessions")
        self.assertEqual(self.repl.session.id, first)
        self.assertIn(">>> first chat", out)
        self.assertEqual([s.id for s in self.store.list()], [second, first])  # no stray empty session

    def test_the_picker_opens_on_the_current_session_wherever_it_is_in_the_list(self):
        first, second, third = self.three_sessions()
        seen = {}
        self.repl.picker = lambda sessions, label, **options: seen.update(options)
        self.run_lines(f"/sessions {first}", "/sessions")  # now in the oldest: last in the list
        self.assertEqual((seen["start"], seen["current"]), (2, first))
        self.run_lines("/new", "/sessions")  # a session with no messages yet is listed first
        self.assertEqual((seen["start"], seen["current"]), (0, self.repl.session.id))

    def test_a_session_with_no_messages_yet_is_in_the_list_too(self):
        first, second, third = self.three_sessions()
        seen = {}

        def picker(sessions, label, **options):
            seen.update(options, sessions=sessions, labels=[label(s) for s in sessions])
            return sessions[0]

        self.repl.picker = picker
        out = self.run_lines("/new", "/sessions")
        new = self.repl.session.id
        self.assertEqual([s.id for s in seen["sessions"]], [new, third, second, first])
        self.assertEqual(seen["labels"][0], ("(new session)", f"{new} · new · 0 msgs · current"))
        self.assertIn("you are already in that session", out)  # choosing it changes nothing
        self.assertIn("nothing to delete", seen["protect"](seen["sessions"][0]))
        self.assertIsNone(seen["protect"](seen["sessions"][1]))
        self.assertEqual(len(self.store.list()), 3)  # and listing it did not save it

        self.repl.picker = None  # the numbered list has it as well
        out = self.run_lines("/sessions", "/sessions 1", "/sessions 2")
        self.assertRegex(out, rf"1  \*  {new}  \(new session\)")
        self.assertEqual(self.repl.session.id, third)

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

    def test_the_list_opens_even_when_the_current_session_is_the_only_one(self):
        seen = []
        self.repl.picker = lambda sessions, label, **k: seen.append([label(s)[0] for s in sessions])
        out = self.run_lines("/sessions", "hi", "/sessions")
        self.assertEqual(seen, [["(new session)"], ["hi"]])  # before and after it is saved
        self.assertNotIn("no other sessions", out)
        self.assertEqual(out.count("stayed in this session"), 2)

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
        write_skill(self.tmp / "skills", "terse")
        self.assertEqual(self.repl.completions("/sk"), ["/skills"])
        self.assertEqual(self.repl.completions("/skills "), ["add", "rm", "haiku", "terse"])
        self.assertEqual(self.repl.completions("/skills add h"), ["haiku"])
        self.assertEqual(self.repl.completions("/skills rm "), ["haiku"])  # only what is attached
        self.assertEqual(self.repl.completions("/skill t"), ["terse"])  # the alias completes too
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
        self.assertEqual(self.repl.completions("/sk"), ["/skills"])  # commands still win


if __name__ == "__main__":
    unittest.main()
