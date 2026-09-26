import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ac import server
from ac.ollama import Client
from ac.store import Attachment, Store
from tests.fake_ollama import FakeOllama
from tests.make_pdf import make_pdf
from tests.test_skills import write_skill


class ServerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "ac.db"
        env = mock.patch.dict(os.environ, {"AC_CONFIG_DIR": str(Path(tmp.name) / "config"),
                                           "AC_SKILLS_PATH": str(Path(tmp.name) / "skills")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AC_MODEL", None)
        self.fake = FakeOllama()
        self.addCleanup(self.fake.stop)

        self.store = store = Store(self.db)
        self.addCleanup(store.close)
        self.lisbon = store.save(store.draft("m1", title="Trip to Lisbon", skills=["concise"]))
        store.add_message(self.lisbon.id, "user", "Plan three days",
                          attachments=[Attachment(path="/tmp/notes.md", kind="text", content="hi")])
        store.add_message(self.lisbon.id, "assistant", "**Day 1**: Alfama", thinking="hmm")
        self.soup = store.save(store.draft("m2:latest", title="Soup"))
        store.add_message(self.soup.id, "user", "Leek soup?")

        self.httpd = server.make_server(0, db_path=self.db, client=Client(self.fake.host))
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

    def post(self, path, body, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        self.addCleanup(conn.close)
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        conn.request("POST", path, body=data, headers={
            "Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{self.port}", **(headers or {})})
        res = conn.getresponse()
        raw = res.read()
        if res.getheader("Content-Type") == "application/x-ndjson":
            return res.status, [json.loads(line) for line in raw.splitlines()]
        return res.status, json.loads(raw)

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
                         [{"id": 1, "path": "/tmp/notes.md", "kind": "text", "note": None,
                           "size": 2}])
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


    # -- chatting ----------------------------------------------------------

    def test_chat_in_a_session_streams_and_saves_the_reply(self):
        self.fake.reply("Day 2: ", "Belém", thinking="more days")
        status, events = self.post("/api/chat", {"session": self.lisbon.id, "text": "And then?"})
        self.assertEqual(status, 200)
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds, ["session", "message", "note", "thinking", "content", "content",
                                 "message", "done"])
        self.assertEqual(events[2]["text"], "skill 'concise' can't be loaded; continuing without it")
        self.assertEqual(events[1]["message"]["content"], "And then?")
        self.assertEqual(events[-2]["message"]["content"], "Day 2: Belém")
        self.assertEqual(events[-1], {"type": "done", "used": 18, "context": 1000})
        stored = self.store.messages(self.lisbon.id)
        self.assertEqual([(m.role, m.content, m.status) for m in stored[2:]],
                         [("user", "And then?", "complete"),
                          ("assistant", "Day 2: Belém", "complete")])
        self.assertEqual(stored[3].thinking, "more days")
        # The whole conversation went to the model, the thinking of earlier replies did not.
        sent = self.fake.requests[-1]["messages"]
        self.assertEqual([m["role"] for m in sent], ["user", "assistant", "user"])
        self.assertNotIn("hmm", json.dumps(sent))

    def test_chat_without_a_session_starts_one(self):
        self.fake.reply("Hello!")
        status, events = self.post("/api/chat", {"text": "Hi there", "model": "m2"})
        self.assertEqual(status, 200)
        session = events[0]["session"]
        self.assertEqual((session["title"], session["model"]), ("Hi there", "m2:latest"))
        self.assertEqual([m.content for m in self.store.messages(session["id"])],
                         ["Hi there", "Hello!"])

    def test_chat_attaches_files_the_message_names(self):
        notes = Path(self.db).parent / "notes.txt"
        notes.write_text("buy leeks")
        self.fake.reply("Noted.")
        _, events = self.post("/api/chat", {"session": self.soup.id, "text": f"see {notes}"})
        self.assertTrue(any(e["type"] == "note" and "notes.txt" in e["text"] for e in events))
        self.assertIn("buy leeks", self.fake.requests[-1]["messages"][-1]["content"])

    def test_a_failed_reply_is_reported_and_can_be_retried(self):
        self.fake.scripts.append([("content", "half"), ("error", "out of memory")])
        _, events = self.post("/api/chat", {"session": self.soup.id, "text": "Recipe?"})
        self.assertEqual(events[-1], {"type": "error", "text": "out of memory"})
        self.assertEqual(self.store.messages(self.soup.id)[-1].status, "error")

        self.fake.reply("Leeks, potatoes, stock.")
        status, events = self.post("/api/chat", {"session": self.soup.id, "retry": True})
        self.assertEqual(status, 200)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual([(m.role, m.content) for m in self.store.messages(self.soup.id)],
                         [("user", "Leek soup?"), ("user", "Recipe?"),
                          ("assistant", "Leeks, potatoes, stock.")])

    def test_a_reply_the_page_leaves_is_kept_as_interrupted(self):
        self.fake.reply("one ", "two ", "three")
        events = server.chat(self.store, Client(self.fake.host),
                             {"session": self.soup.id, "text": "Count"})
        for event in events:
            if event["type"] == "content":
                break
        events.close()
        last = self.store.messages(self.soup.id)[-1]
        self.assertEqual((last.role, last.content, last.status), ("assistant", "one ", "interrupted"))

    def test_bad_chat_requests(self):
        self.assertEqual(self.post("/api/chat", {"session": self.soup.id, "text": "  "}),
                         (400, {"error": "nothing to send"}))
        self.assertEqual(self.post("/api/chat", {"session": "zzzz", "text": "hi"})[0], 404)
        self.assertEqual(self.post("/api/chat", {"text": "hi", "model": "nope"})[0], 502)
        empty = self.store.save(self.store.draft("m1", title="Empty"))
        self.assertEqual(self.post("/api/chat", {"session": empty.id, "retry": True}),
                         (400, {"error": "nothing to retry"}))
        self.assertEqual(self.post("/api/chat", b"[1, 2]")[0], 400)
        self.assertEqual(self.post("/api/nope", {"text": "hi"})[0], 404)
        self.assertEqual(self.fake.requests, [])

    def test_refuses_posts_from_other_sites(self):
        # A form on another site can post to 127.0.0.1 with the right Host; it can't send JSON
        # without asking first, and its Origin gives it away.
        status, _ = self.post("/api/chat", {"text": "hi"}, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.post("/api/chat", {"text": "hi"}, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertEqual(self.fake.requests, [])


    # -- files, models and skills --------------------------------------------

    def upload(self, name, data):
        return {"name": name, "data": base64.b64encode(data).decode()}

    def test_uploaded_files_go_with_the_message(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 20
        self.fake.capabilities.append("vision")
        self.fake.reply("Got them.")
        _, events = self.post("/api/chat", {
            "session": self.soup.id, "text": "Look",
            "files": [self.upload("recipe.txt", b"leeks, butter"),
                      self.upload("../../photo.png", png),
                      self.upload("recipe.txt", b"second copy")]})
        user = next(e["message"] for e in events if e["type"] == "message")
        self.assertEqual([(a["path"], a["kind"]) for a in user["attachments"]],
                         [("recipe.txt", "text"), ("photo.png", "image"), ("recipe.txt", "text")])
        sent = self.fake.requests[-1]["messages"][-1]
        self.assertIn('<file path="recipe.txt">\nleeks, butter\n</file>', sent["content"])
        self.assertIn("second copy", sent["content"])
        self.assertEqual(sent["images"], [base64.b64encode(png).decode()])
        # The picture can be shown again from the session.
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        self.addCleanup(conn.close)
        conn.request("GET", f"/api/attachments/{user['attachments'][1]['id']}",
                     headers={"Host": f"127.0.0.1:{self.port}"})
        res = conn.getresponse()
        self.assertEqual((res.status, res.getheader("Content-Type"), res.read()),
                         (200, "image/png", png))
        self.assertEqual(self.get(f"/api/attachments/{user['attachments'][0]['id']}")[0], 404)

    def test_an_uploaded_pdf_sends_its_text(self):
        self.fake.reply("Read it.")
        _, events = self.post("/api/chat", {
            "session": self.soup.id, "text": "Summarise",
            "files": [self.upload("report.pdf", make_pdf(["Quarterly leeks up"]))]})
        self.assertIn("Quarterly leeks up", self.fake.requests[-1]["messages"][-1]["content"])
        notes = [e["text"] for e in events if e["type"] == "note"]
        self.assertTrue(any(n.startswith("attached report.pdf (text") for n in notes), notes)

    def test_an_upload_that_cant_be_read_is_reported(self):
        self.fake.reply("ok")
        _, events = self.post("/api/chat", {
            "session": self.soup.id, "text": "hi",
            "files": [self.upload("blob.bin", b"\0\1\2")]})
        notes = [e["text"] for e in events if e["type"] == "note"]
        self.assertIn("blob.bin isn't text, a PDF or an image, so it can't be attached", notes)
        self.assertEqual(self.post("/api/chat", {"session": self.soup.id, "text": "hi",
                                                 "files": [{"name": "x", "data": "!!"}]}),
                         (400, {"error": "x didn't arrive intact"}))

    def test_models_and_skills(self):
        _, body = self.get("/api/models")
        self.assertEqual([m["name"] for m in body["models"]], ["m1", "m2:latest"])
        write_skill(Path(os.environ["AC_SKILLS_PATH"]), "haiku", description="Poetry mode")
        _, body = self.get("/api/skills")
        self.assertEqual(body["skills"], [{"name": "haiku", "description": "Poetry mode"}])

    def test_change_a_sessions_model_and_skills(self):
        write_skill(Path(os.environ["AC_SKILLS_PATH"]), "haiku")
        status, body = self.post(f"/api/sessions/{self.soup.id}", {"model": "m1", "skills": ["haiku"]})
        self.assertEqual(status, 200)
        self.assertEqual((body["session"]["model"], body["session"]["skills"]), ("m1", ["haiku"]))
        again = self.store.get(self.soup.id)
        self.assertEqual((again.model, again.skills), ("m1", ["haiku"]))

        status, body = self.post(f"/api/sessions/{self.soup.id}", {"skills": ["nope"]})
        self.assertEqual(status, 400)
        self.assertIn("no skill named 'nope'", body["error"])
        status, body = self.post(f"/api/sessions/{self.soup.id}", {"model": "zzz"})
        self.assertEqual(status, 400)
        self.assertIn("model 'zzz' is not installed", body["error"])
        self.assertEqual(self.store.get(self.soup.id).skills, ["haiku"])

    def test_a_new_session_takes_its_skills(self):
        write_skill(Path(os.environ["AC_SKILLS_PATH"]), "haiku", body="Answer in haiku.")
        self.fake.reply("Leaves fall")
        _, events = self.post("/api/chat", {"text": "Autumn?", "model": "m1", "skills": ["haiku"]})
        self.assertEqual(events[0]["session"]["skills"], ["haiku"])
        self.assertIn("Answer in haiku.", self.fake.requests[-1]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
