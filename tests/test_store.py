import sqlite3
import tempfile
import unittest
from pathlib import Path

from ac.store import MIGRATIONS, SCHEMA, Ambiguous, Attachment, NotFound, Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def make(self, title="chat", **kw):
        return self.store.save(self.store.draft("m1", title=title, **kw))

    def test_draft_is_not_persisted_until_saved(self):
        s = self.store.draft("m1")
        self.assertFalse(s.persisted)
        self.assertEqual(self.store.list(), [])
        self.store.save(s)
        self.assertTrue(s.persisted)
        self.assertEqual([x.id for x in self.store.list()], [s.id])

    def test_roundtrip_fields(self):
        s = self.make(system="be brief", options={"temperature": 0.2}, skills=["b", "a"])
        got = self.store.get(s.id)
        self.assertEqual((got.title, got.model, got.system), ("chat", "m1", "be brief"))
        self.assertEqual(got.options, {"temperature": 0.2})
        self.assertEqual(got.skills, ["b", "a"])  # order preserved

    def test_title_source(self):
        self.assertEqual(self.make(title="mine").title_source, "user")
        untitled = self.store.save(self.store.draft("m1"))
        self.assertIsNone(self.store.get(untitled.id).title_source)
        untitled.title, untitled.title_source = "Written by the model", "model"
        self.store.save(untitled)
        self.assertEqual(self.store.get(untitled.id).title_source, "model")
        self.assertEqual(self.store.fork(untitled.id).title_source, "model")
        self.assertEqual(self.store.fork(untitled.id, title="my branch").title_source, "user")

    def test_named_paths(self):
        self.assertEqual(self.store.resources(), {})
        self.store.set_resource("mom", ["/vault/mom/*.md"])
        self.store.set_resource("trip", ["/notes/plan.md", "/notes/my tickets/*.pdf"])
        self.store.set_resource("mom", ["/vault/mum/*.md"])  # naming again moves the name
        self.assertEqual(self.store.resources(), {
            "mom": ["/vault/mum/*.md"], "trip": ["/notes/plan.md", "/notes/my tickets/*.pdf"]})
        self.assertTrue(self.store.delete_resource("trip"))
        self.assertFalse(self.store.delete_resource("trip"))
        session = self.make()
        self.store.delete(session.id)
        self.assertEqual(self.store.resources(), {"mom": ["/vault/mum/*.md"]})  # not per session

    def test_a_name_saved_before_names_could_hold_several_paths(self):
        self.store.db.execute("INSERT INTO resources VALUES ('old', '/vault/old/*.md', '2026-01-01')")
        self.assertEqual(self.store.resources(), {"old": ["/vault/old/*.md"]})

    def test_update(self):
        s = self.make(skills=["a"])
        s.title, s.model, s.skills = "renamed", "m2", []
        self.store.save(s)
        got = self.store.get(s.id)
        self.assertEqual((got.title, got.model, got.skills), ("renamed", "m2", []))
        self.assertEqual(len(self.store.list()), 1)

    def test_get_by_prefix_and_title(self):
        s = self.make(title="My Chat")
        self.assertEqual(self.store.get(s.id[:4]).id, s.id)
        self.assertEqual(self.store.get("my chat").id, s.id)
        with self.assertRaises(NotFound):
            self.store.get("zzzz")
        with self.assertRaises(NotFound):
            self.store.get("")

    def test_ambiguous_prefix(self):
        a, b = self.store.draft("m"), self.store.draft("m")
        a.id, b.id = "abcd0001", "abcd0002"
        self.store.save(a)
        self.store.save(b)
        with self.assertRaises(Ambiguous):
            self.store.get("abcd")
        self.assertEqual(self.store.get("abcd0002").id, b.id)

    def test_messages_sequence_and_fields(self):
        s = self.make()
        self.store.add_message(s.id, "user", "hi")
        m = self.store.add_message(s.id, "assistant", "hello", thinking="hmm", model="m1",
                                   skills=[{"name": "a", "sha": "x"}], prompt_tokens=3,
                                   eval_tokens=5, duration_ms=10)
        self.assertEqual(m.seq, 2)
        msgs = self.store.messages(s.id)
        self.assertEqual([(x.seq, x.role, x.content) for x in msgs],
                         [(1, "user", "hi"), (2, "assistant", "hello")])
        self.assertEqual(msgs[1].thinking, "hmm")
        self.assertEqual(msgs[1].skills, [{"name": "a", "sha": "x"}])
        self.assertEqual(self.store.get(s.id).message_count, 2)

    def test_delete_cascades(self):
        s = self.make(skills=["a"])
        self.store.add_message(s.id, "user", "unicorn")
        self.store.delete(s.id)
        self.assertEqual(self.store.list(), [])
        for table in ("messages", "session_skills"):
            count = self.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.assertEqual(count, 0, table)
        self.assertEqual(self.store.list(search="unicorn"), [])

    def test_delete_messages_from(self):
        s = self.make()
        for text in ("one", "two", "three"):
            self.store.add_message(s.id, "user", text)
        self.store.delete_messages_from(s.id, 2)
        self.assertEqual([m.content for m in self.store.messages(s.id)], ["one"])
        self.assertEqual(self.store.add_message(s.id, "user", "again").seq, 2)

    def test_fork(self):
        s = self.make(title="orig", system="sys", skills=["a"], options={"think": False})
        for i in range(4):
            self.store.add_message(s.id, "user" if i % 2 == 0 else "assistant", f"m{i}")
        full = self.store.fork(s.id)
        self.assertEqual(full.message_count, 4)
        self.assertEqual((full.parent_id, full.forked_at_seq), (s.id, 4))
        self.assertEqual((full.title, full.system, full.skills, full.options),
                         ("orig (fork)", "sys", ["a"], {"think": False}))
        part = self.store.fork(s.id, at_seq=2, title="branch")
        self.assertEqual([m.content for m in self.store.messages(part.id)], ["m0", "m1"])
        self.store.add_message(part.id, "user", "diverged")
        self.assertEqual(self.store.get(s.id).message_count, 4)  # original untouched

    def test_attachments_roundtrip_cascade_and_fork(self):
        s = self.make()
        sent = [Attachment("/tmp/a.md", "text", content="alpha", note="first 1 KB of 2 KB"),
                Attachment("/tmp/b.png", "image", data=b"\x89PNG")]
        self.store.add_message(s.id, "user", "look", attachments=sent)
        self.store.add_message(s.id, "assistant", "seen")
        first, second = self.store.messages(s.id)
        self.assertEqual(first.attachments, sent)  # order and every field preserved
        self.assertEqual(second.attachments, [])
        self.assertEqual((sent[0].size, sent[1].size), (5, 4))

        fork = self.store.fork(s.id)
        self.assertEqual(self.store.messages(fork.id)[0].attachments, sent)
        self.store.delete(s.id)
        self.assertEqual(self.store.messages(fork.id)[0].attachments, sent)  # fork owns its copy
        self.store.delete_messages_from(fork.id, 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0], 0)
        self.assertEqual(self.store.list(search="alpha"), [])  # file text isn't searched as chat

    def test_delete_one_attachment(self):
        s = self.make()
        self.store.add_message(s.id, "user", "look", attachments=[
            Attachment("/a.md", "text", content="alpha"), Attachment("/b.md", "text", content="beta")])
        first, second = self.store.messages(s.id)[0].attachments
        self.assertIsNotNone(first.id)
        self.assertEqual(first, Attachment("/a.md", "text", content="alpha"))  # the id isn't identity
        self.store.delete_attachment(first.id)
        (message,) = self.store.messages(s.id)
        self.assertEqual(([a.path for a in message.attachments], message.content), (["/b.md"], "look"))

    def test_list_order_and_search(self):
        a = self.make(title="alpha")
        b = self.make(title="beta")
        self.store.add_message(a.id, "user", "tell me about sourdough starters")
        self.assertEqual([x.id for x in self.store.list()], [a.id, b.id])  # a touched last
        self.assertEqual([x.id for x in self.store.list(search="sourdough")], [a.id])
        self.assertEqual([x.id for x in self.store.list(search="bet")], [b.id])
        self.assertEqual(self.store.list(search='"quoted" AND (weird'), [])  # no FTS syntax error

    def test_search_follows_deleted_messages(self):
        s = self.make()
        self.store.add_message(s.id, "user", "keep")
        self.store.add_message(s.id, "user", "ephemeral")
        self.store.delete_messages_from(s.id, 2)
        self.assertEqual(self.store.list(search="ephemeral"), [])
        self.assertEqual(len(self.store.list(search="keep")), 1)

    def test_upgrades_a_database_from_before_attachments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            old = sqlite3.connect(path)
            old.executescript(SCHEMA)
            old.execute("INSERT INTO sessions VALUES ('abcd1234', 'old chat', 'm1', NULL, '{}', "
                        "NULL, NULL, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
            old.execute("INSERT INTO messages (session_id, seq, role, content, created_at) "
                        "VALUES ('abcd1234', 1, 'user', 'from before', '2026-01-01T00:00:00+00:00')")
            old.execute("INSERT INTO sessions VALUES ('beef5678', 'how do I bake rye bread?', "
                        "'m1', NULL, '{}', NULL, NULL, '2026-01-02T00:00:00+00:00', "
                        "'2026-01-02T00:00:00+00:00')")
            old.execute("INSERT INTO messages (session_id, seq, role, content, created_at) VALUES "
                        "('beef5678', 1, 'user', 'how do I  bake\nrye bread?', "
                        "'2026-01-02T00:00:00+00:00')")
            old.execute("PRAGMA user_version = 1")
            old.commit()
            old.close()

            store = Store(path)
            self.addCleanup(store.close)
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0],
                             len(MIGRATIONS))
            # 'old chat' isn't its first message, so someone chose it: never replace it.
            self.assertEqual(store.get("abcd1234").title_source, "user")
            self.assertEqual(store.get("beef5678").title_source, "auto")  # just the first message
            (message,) = store.messages("abcd1234")
            self.assertEqual((message.content, message.attachments), ("from before", []))
            store.add_message("abcd1234", "user", "now with a file",
                              attachments=[Attachment("/x", "text", content="x")])
            self.assertEqual(len(store.messages("abcd1234")[1].attachments), 1)

    def test_persists_across_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "ac.db"
            first = Store(path)
            s = first.save(first.draft("m1", title="kept"))
            first.add_message(s.id, "user", "hi")
            first.close()
            second = Store(path)
            self.assertEqual(second.get(s.id).title, "kept")
            self.assertEqual(len(second.messages(s.id)), 1)
            second.close()


if __name__ == "__main__":
    unittest.main()
