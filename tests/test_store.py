import tempfile
import unittest
from pathlib import Path

from ac.store import Ambiguous, NotFound, Store


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
