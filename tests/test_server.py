import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ac import server
from ac.store import Attachment, Store


class ServerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "ac.db"
        env = mock.patch.dict(os.environ, {"AC_CONFIG_DIR": str(Path(tmp.name) / "config")})
        env.start()
        self.addCleanup(env.stop)

        store = Store(self.db)
        self.addCleanup(store.close)
        self.lisbon = store.save(store.draft("m1", title="Trip to Lisbon", skills=["concise"]))
        store.add_message(self.lisbon.id, "user", "Plan three days",
                          attachments=[Attachment(path="/tmp/notes.md", kind="text", content="hi")])
        store.add_message(self.lisbon.id, "assistant", "**Day 1**: Alfama", thinking="hmm")
        self.soup = store.save(store.draft("m2", title="Soup"))
        store.add_message(self.soup.id, "user", "Leek soup?")

        self.httpd = server.make_server(0, db_path=self.db)
        self.port = self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def get(self, path, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        self.addCleanup(conn.close)
        conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{self.port}"})
        res = conn.getresponse()
        body = res.read()
        if res.getheader("Content-Type") == "application/json":
            body = json.loads(body)
        return res.status, body

    def test_lists_sessions_newest_first(self):
        status, body = self.get("/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual([s["title"] for s in body["sessions"]], ["Soup", "Trip to Lisbon"])
        self.assertEqual(body["sessions"][1]["message_count"], 2)
        self.assertEqual(body["sessions"][1]["skills"], ["concise"])

    def test_search_matches_titles_and_messages(self):
        _, body = self.get("/api/sessions?search=leek")
        self.assertEqual([s["id"] for s in body["sessions"]], [self.soup.id])
        _, body = self.get("/api/sessions?search=lisbon")
        self.assertEqual([s["id"] for s in body["sessions"]], [self.lisbon.id])

    def test_one_session_with_its_messages(self):
        status, body = self.get(f"/api/sessions/{self.lisbon.id[:5]}")
        self.assertEqual(status, 200)
        self.assertEqual(body["session"]["id"], self.lisbon.id)
        user, reply = body["messages"]
        self.assertEqual(user["attachments"],
                         [{"path": "/tmp/notes.md", "kind": "text", "note": None, "size": 2}])
        self.assertEqual((reply["role"], reply["content"], reply["thinking"]),
                         ("assistant", "**Day 1**: Alfama", "hmm"))

    def test_unknown_session_is_404(self):
        status, body = self.get("/api/sessions/zzzz")
        self.assertEqual(status, 404)
        self.assertIn("no session matches", body["error"])

    def test_speaker_names_come_from_config(self):
        config_dir = Path(os.environ["AC_CONFIG_DIR"])
        config_dir.mkdir()
        (config_dir / "config.toml").write_text('user_name = "Sam"\n')
        _, body = self.get("/api/config")
        self.assertEqual(body["names"], {"user": "Sam", "assistant": "Assistant"})

    def test_serves_the_page(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>acc</title>", body)
        self.assertEqual(self.get("/static/../server.py")[0], 404)
        self.assertEqual(self.get("/nope")[0], 404)

    def test_refuses_other_host_names(self):
        # A page on another site could point its own hostname at 127.0.0.1 (DNS rebinding).
        status, _ = self.get("/api/sessions", host=f"evil.example:{self.port}")
        self.assertEqual(status, 403)
        self.assertEqual(self.get("/api/sessions", host=f"localhost:{self.port}")[0], 200)


if __name__ == "__main__":
    unittest.main()
