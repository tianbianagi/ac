import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

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
                                           "AC_SKILLS_PATH": str(Path(tmp.name) / "skills"),
                                           "AC_UPLOADS_DIR": str(Path(tmp.name) / "uploads")})
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

    def get(self, path, host=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        self.addCleanup(conn.close)
        conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{self.port}",
                                           **(headers or {})})
        res = conn.getresponse()
        body = res.read()
        if res.getheader("Content-Type") == "application/json":
            body = json.loads(body)
        return res.status, body

    def delete(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        self.addCleanup(conn.close)
        conn.request("DELETE", path, headers={
            "Host": f"127.0.0.1:{self.port}", "Origin": f"http://127.0.0.1:{self.port}",
            **(headers or {})})
        res = conn.getresponse()
        return res.status, json.loads(res.read())

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
                           "size": 2, "real": os.path.realpath("/tmp/notes.md")}])
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

    def test_each_user_has_their_own_sessions_and_skills(self):
        config_dir = Path(os.environ["AC_CONFIG_DIR"])
        config_dir.mkdir()
        (config_dir / "config.toml").write_text(
            'user_name = "Sam"\n[users.huiwen]\nassistant_name = "Codi"\n')
        write_skill(config_dir / "skills", "shared")
        write_skill(config_dir / "users" / "huiwen" / "skills", "hers", body="Hers only.")
        self.store.set_resource(self.soup.id, "notes", ["/tmp/notes.md"])
        her = {"X-Acc-User": "Huiwen"}

        _, body = self.get("/api/sessions", headers=her)
        self.assertEqual((body["sessions"], body["archived_count"]), ([], 0))
        self.assertEqual(self.get(f"/api/sessions/{self.lisbon.id}", headers=her)[0], 404)
        self.assertEqual(self.post(f"/api/sessions/{self.lisbon.id}", {"title": "x"}, her)[0], 404)
        self.assertEqual(self.delete(f"/api/sessions/{self.lisbon.id}", her)[0], 404)
        self.assertEqual(self.delete("/api/attachments/1", her)[0], 404)
        self.assertEqual(self.get(f"/api/names?session={self.soup.id}", headers=her)[0], 404)
        self.assertEqual(self.post("/api/tags", {"session": self.soup.id, "path": "/tmp",
                                                 "tag": "x"}, her)[0], 404)
        self.assertEqual(self.get("/api/config", headers=her)[1],
                         {"names": {"user": "Huiwen", "assistant": "Codi"}, "user": "huiwen"})
        self.assertEqual([s["name"] for s in self.get("/api/skills", headers=her)[1]["skills"]],
                         ["hers", "shared"])
        self.assertEqual([s["name"] for s in self.get("/api/skills")[1]["skills"]], ["shared"])

        self.fake.reply("Hi Huiwen")
        status, events = self.post("/api/chat", {"text": "Hello", "model": "m1",
                                                 "skills": ["hers"]}, her)
        self.assertEqual(status, 200)
        mine = self.store.list()
        self.assertEqual([s.title for s in mine], ["Soup", "Trip to Lisbon"])
        self.assertIn("Hers only.", self.fake.requests[-1]["messages"][0]["content"])
        (hers,) = self.get("/api/sessions", headers=her)[1]["sessions"]
        self.assertEqual(hers["title"], "Hello")
        self.assertEqual(self.get(f"/api/sessions/{hers['id']}")[0], 404)   # nor she mine
        self.assertEqual(self.post("/api/chat", {"text": "Hi", "skills": ["hers"]})[0], 400)
        self.assertEqual(self.get("/api/config")[1]["names"]["user"], "Sam")

    def test_a_user_config_does_not_know_is_turned_away(self):
        stranger = {"X-Acc-User": "mallory"}
        self.assertEqual(self.get("/api/sessions", headers=stranger)[0], 403)
        self.assertEqual(self.get("/", headers=stranger)[0], 403)
        self.assertEqual(self.post("/api/chat", {"text": "hi"}, stranger)[0], 403)
        self.assertEqual(self.delete(f"/api/sessions/{self.soup.id}", stranger)[0], 403)
        self.assertEqual(self.get("/api/sessions", headers={"X-Acc-User": ""})[0], 200)  # owner

    def test_serves_the_page(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>acc</title>", body)
        self.assertEqual(self.get("/static/../server.py")[0], 404)
        self.assertEqual(self.get("/nope")[0], 404)

    def test_serves_what_installing_the_app_needs(self):
        status, body = self.get("/static/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["start_url"], "/")
        for icon in json.loads(body)["icons"]:
            self.assertEqual(self.get(icon["src"])[0], 200)
        self.assertEqual(self.get("/static/apple-touch-icon.png")[0], 200)
        self.assertEqual(self.get("/sw.js")[0], 200)

    def test_refuses_other_host_names(self):
        # A page on another site could point its own hostname at 127.0.0.1 (DNS rebinding).
        status, _ = self.get("/api/sessions", host=f"evil.example:{self.port}")
        self.assertEqual(status, 403)
        self.assertEqual(self.get("/api/sessions", host=f"localhost:{self.port}")[0], 200)

    def test_allowed_host_behind_a_proxy(self):
        httpd = server.make_server(0, db_path=self.db, client=Client(self.fake.host),
                                   allowed_hosts=["Acc.Example.me"])
        self.addCleanup(httpd.server_close)
        threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(httpd.shutdown)

        def request(method, path, host, origin=None):
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
            self.addCleanup(conn.close)
            headers = {"Host": host, **({"Origin": origin} if origin else {})}
            conn.request(method, path, headers=headers)
            res = conn.getresponse()
            res.read()
            return res.status

        self.assertEqual(request("GET", "/api/sessions", "acc.example.me"), 200)
        self.assertEqual(request("GET", "/api/sessions", "evil.example"), 403)
        # Changes still have to come from the proxied page itself.
        path = f"/api/sessions/{self.soup.id}"
        self.assertEqual(request("DELETE", path, "acc.example.me", "https://evil.example"), 403)
        self.assertEqual(request("DELETE", path, "acc.example.me", "https://acc.example.me"), 200)


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
        self.assertEqual(events[-1], {"type": "done", "used": 18, "context": 1000, "exact": True,
                                      "speed": 7.0})
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
        self.assertEqual(body["skills"],
                         [{"name": "haiku", "description": "Poetry mode", "scope": "other"}])

    def test_skills_are_managed_from_the_page(self):
        config_dir = Path(os.environ["AC_CONFIG_DIR"])
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("[users.hagi]\n")
        mine, shared = config_dir / "users" / self.store.user / "skills", config_dir / "skills"
        hers = {"X-Acc-User": "hagi"}

        status, body = self.post("/api/skills", {"scope": "personal", "name": "brief",
                                                 "description": "Short: \"direct\"", "body": "Be brief."})
        self.assertEqual((status, body["skill"]), (200, {
            "scope": "personal", "name": "brief", "description": "Short: \"direct\"", "body": "Be brief."}))
        self.assertEqual(body["library"], {"personal": [
            {"name": "brief", "description": "Short: \"direct\"", "scope": "personal"}], "shared": []})
        self.assertTrue((mine / "brief" / "SKILL.md").is_file())
        self.assertEqual(self.get("/api/skills", headers=hers)[1]["skills"], [])    # not hers

        (mine / "brief" / "notes.txt").write_text("kept")         # other files go along with it
        status, body = self.post("/api/skills", {"scope": "shared", "name": "terse", "description": "",
                                                 "body": "Be terse.", "was": {"scope": "personal",
                                                                              "name": "brief"}})
        self.assertEqual(status, 200)
        self.assertFalse((mine / "brief").exists())
        self.assertEqual((shared / "terse" / "notes.txt").read_text(), "kept")
        self.assertEqual(self.get("/api/skills", headers=hers)[1]["skills"],
                         [{"name": "terse", "description": "", "scope": "shared"}])
        self.assertEqual(self.get("/api/skills/shared/terse")[1]["body"], "Be terse.")

        self.post("/api/skills", {"scope": "personal", "name": "terse", "body": "Mine wins."})
        _, body = self.get("/api/skills")
        self.assertEqual(body["skills"], [{"name": "terse", "description": "", "scope": "personal"}])
        self.assertEqual([k["name"] for k in body["library"]["shared"]], ["terse"])

        for bad in ({"scope": "shared", "name": "Bad Name", "body": "x"},
                    {"scope": "shared", "name": "ok", "body": "  "},
                    {"scope": "elsewhere", "name": "ok", "body": "x"},
                    {"scope": "shared", "name": "ok", "body": "x", "was": {"scope": "shared", "name": "gone"}},
                    {"scope": "personal", "name": "terse", "body": "x",       # would overwrite another
                     "was": {"scope": "shared", "name": "terse"}}):
            self.assertEqual(self.post("/api/skills", bad)[0], 400, bad)
        self.assertEqual(self.get("/api/skills/shared/nope")[0], 404)
        self.assertEqual(self.get("/api/skills/personal/..")[0], 404)

        status, body = self.delete("/api/skills/shared/terse", hers)       # shared: anyone may
        self.assertEqual((status, body["deleted"]), (200, "terse"))
        self.assertEqual((shared / "terse" / "notes.txt").read_text(), "kept")
        self.assertFalse((shared / "terse" / "SKILL.md").exists())
        self.assertEqual(self.delete("/api/skills/personal/terse", hers)[0], 404)   # not hers
        self.assertEqual(self.delete("/api/skills/personal/terse")[0], 200)
        self.assertFalse((mine / "terse").exists())

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


    # -- rename, delete, export ------------------------------------------------

    def test_rename(self):
        status, body = self.post(f"/api/sessions/{self.soup.id}", {"title": "  Leek   soup  "})
        self.assertEqual((status, body["session"]["title"]), (200, "Leek soup"))
        again = self.store.get(self.soup.id)
        self.assertEqual((again.title, again.title_source), ("Leek soup", "user"))
        self.assertEqual(self.post(f"/api/sessions/{self.soup.id}", {"title": " "}),
                         (400, {"error": "a title can't be empty"}))

    def test_delete(self):
        self.assertEqual(self.delete(f"/api/sessions/{self.soup.id}"), (200, {"deleted": self.soup.id}))
        self.assertEqual([s.id for s in self.store.list()], [self.lisbon.id])
        self.assertEqual(self.delete(f"/api/sessions/{self.soup.id}")[0], 404)
        # Another site can't delete through the browser either.
        status, _ = self.delete(f"/api/sessions/{self.lisbon.id}", {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(len(self.store.list()), 1)

    def test_export_names_the_session_then_offers_the_text(self):
        self.fake.reply("Three Days in Lisbon")
        self.store.db.execute("UPDATE sessions SET title_source = 'auto' WHERE id = ?", (self.lisbon.id,))
        self.store.db.commit()
        status, body = self.post(f"/api/sessions/{self.lisbon.id}/export", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["session"]["title"], "Three Days in Lisbon")
        self.assertIn("titled: Three Days in Lisbon", body["notes"])
        self.assertRegex(body["filename"], r"^\d{4}-\d{2}-\d{2} Three Days in Lisbon\.md$")
        self.assertIn("# Three Days in Lisbon", body["text"])
        self.assertIn("**Day 1**: Alfama", body["text"])
        self.assertNotIn("hmm", body["text"])
        _, body = self.post(f"/api/sessions/{self.lisbon.id}/export", {"thinking": True})
        self.assertIn("hmm", body["text"])
        self.assertEqual(len(self.fake.requests), 1)    # a title the model gave is kept

    def test_export_saves_into_the_export_folder(self):
        folder = Path(self.db).parent / "exports"
        with mock.patch.dict(os.environ, {"AC_EXPORT_DIR": str(folder)}):
            status, body = self.post(f"/api/sessions/{self.lisbon.id}/export", {"save": True})
        self.assertEqual(status, 200)
        written = folder / body["filename"]
        self.assertEqual(body["path"], str(written))
        self.assertIn("# Trip to Lisbon", written.read_text())
        self.assertNotIn("text", body)
        self.assertEqual(self.fake.requests, [])        # a title the user gave is kept

    def test_export_problems(self):
        empty = self.store.save(self.store.draft("m1", title="Empty"))
        self.assertEqual(self.post(f"/api/sessions/{empty.id}/export", {}),
                         (400, {"error": "nothing to export: this session has no messages yet"}))
        blocker = Path(self.db).parent / "a-file"
        blocker.write_text("")
        with mock.patch.dict(os.environ, {"AC_EXPORT_DIR": str(blocker / "sub")}):
            status, body = self.post(f"/api/sessions/{self.lisbon.id}/export", {"save": True})
        self.assertEqual(status, 400)
        self.assertIn("can't write", body["error"])


    # -- files on the server ---------------------------------------------------

    def tree(self):
        root = (Path(self.db).parent / "project").resolve()
        (root / "src" / "deep").mkdir(parents=True)
        (root / "src" / "app.py").write_text("print('hi')\n")
        (root / "src" / "deep" / "util.py").write_text("def f(): pass\n")
        (root / "src" / ".secret").write_text("hidden\n")
        (root / "node_modules").mkdir()
        (root / "notes.md").write_text("# notes\n")
        (root / ".hidden").mkdir()
        return root

    def test_browse_a_folder(self):
        root = self.tree()
        status, body = self.get(f"/api/browse?dir={root}")
        self.assertEqual(status, 200)
        self.assertEqual((body["dir"], body["parent"]), (str(root), str(root.parent)))
        self.assertEqual([(e["name"], e["dir"], e["size"]) for e in body["entries"]],
                         [("src", True, None), ("notes.md", False, 8)])
        _, body = self.get("/api/browse")
        self.assertEqual(body["dir"], str(Path.home().resolve()))
        self.assertEqual(self.get(f"/api/browse?dir={root}/notes.md")[0], 404)
        # A folder reached through a symlink keeps the name it was reached by.
        link = root.parent / "shortcut"
        link.symlink_to(root)
        _, body = self.get(f"/api/browse?dir={link}")
        self.assertEqual((body["dir"], body["entries"][0]["path"]), (str(link), str(link / "src")))

    def test_match_says_what_an_entry_covers(self):
        root = self.tree()
        self.store.set_resource(self.soup.id, "proj",
                                [str(root / "notes.md"), str(root / "src" / "app.py")])
        _, body = self.get(f"/api/match?path={root}/src/")
        self.assertEqual({k: body[k] for k in ("kind", "count", "size", "path", "real")},
                         {"kind": "folder", "count": 2, "size": 26, "path": str(root / "src"),
                          "real": str(root / "src")})
        self.assertEqual(body["covers"], [str(root / "src" / "app.py"),
                                          str(root / "src" / "deep" / "util.py")])
        _, body = self.get(f"/api/match?path={root}/**/*.py")
        self.assertEqual((body["kind"], body["count"], body["real"]), ("pattern", 2, None))
        _, body = self.get(f"/api/match?path={root}/notes.md")
        self.assertEqual((body["kind"], body["count"], body["size"]), ("file", 1, 8))
        self.assertEqual(self.get(f"/api/match?path=@proj&session={self.soup.id}")[1]["kind"],
                         "name")
        self.assertEqual(self.get(f"/api/match?path=@proj&session={self.lisbon.id}")[0], 404)
        status, body = self.get(f"/api/match?path={root}/nope.txt")
        self.assertEqual((status, body["error"]), (404, f"nothing matches {root}/nope.txt"))
        _, body = self.get(f"/api/names?session={self.soup.id}")
        self.assertEqual(body["names"][0]["name"], "proj")
        self.assertEqual(self.get(f"/api/names?session={self.lisbon.id}")[1], {"names": []})

    def test_tags_name_files_one_at_a_time(self):
        root = self.tree()
        notes, app = str(root / "notes.md"), str(root / "src" / "app.py")
        soup = {"session": self.soup.id}
        status, body = self.post("/api/tags", {**soup, "path": notes, "tag": "@Plan"})
        self.assertEqual((status, body["tag"], body["paths"]), (200, "plan", [notes]))
        self.post("/api/tags", {**soup, "path": app, "tag": "plan"})
        self.post("/api/tags", {**soup, "path": notes, "tag": "plan"})     # once is enough
        self.assertEqual(self.store.resources(self.soup.id)["plan"], [notes, app])
        self.assertEqual(self.store.resources(self.lisbon.id), {})    # only in that session
        _, body = self.get(f"/api/names?session={self.soup.id}")
        self.assertEqual(body["names"][0]["real"], [os.path.realpath(notes), os.path.realpath(app)])

        status, body = self.post("/api/tags", {**soup, "path": str(root / "nope.md"), "tag": "plan"})
        self.assertEqual((status, body["error"]), (400, f"nothing matches {root}/nope.md"))
        self.assertEqual(self.post("/api/tags", {**soup, "path": "upload.png", "tag": "plan"})[0], 400)
        self.assertEqual(self.post("/api/tags", {**soup, "path": notes, "tag": "two words"})[0], 400)
        self.assertEqual(self.post("/api/tags", {"session": "zzzz", "path": notes, "tag": "x"})[0], 404)

        link = root.parent / "shortcut"          # untagging finds the file however it's reached
        link.symlink_to(root)
        self.post("/api/tags", {**soup, "path": str(link / "notes.md"), "tag": "plan", "remove": True})
        self.assertEqual(self.store.resources(self.soup.id)["plan"], [app])
        self.post("/api/tags", {**soup, "path": app, "tag": "plan", "remove": True})
        self.assertNotIn("plan", self.store.resources(self.soup.id))  # no files left: the tag goes
        self.assertTrue((root / "notes.md").exists())

    def test_uploads_are_kept_when_tagged(self):
        uploads = Path(os.environ["AC_UPLOADS_DIR"]) / self.store.user     # each user their own
        data = base64.b64encode(b"ship on friday").decode()
        soup = {"session": self.soup.id}
        status, body = self.post("/api/tags", {**soup, "upload": {"name": "../plan.md", "data": data},
                                               "tag": "plan"})
        kept = uploads / "plan.md"                  # its own name, never a path
        self.assertEqual((status, kept.read_text()), (200, "ship on friday"))
        self.assertEqual((body["path"], self.store.resources(self.soup.id)["plan"]),
                         (os.path.realpath(kept), [str(kept)]))
        self.post("/api/tags", {**soup, "upload": {"name": "plan.md", "data": data}, "tag": "work"})
        self.assertEqual(len(list(uploads.iterdir())), 1)       # the same bytes: the same copy
        other = base64.b64encode(b"ship on monday").decode()
        _, body = self.post("/api/tags", {**soup, "upload": {"name": "plan.md", "data": other},
                                          "tag": "plan"})
        self.assertEqual(Path(body["path"]).name, "plan 2.md")

        # Already sent: kept as the conversation holds it, and the conversation told where.
        session = self.store.draft("m1")
        self.store.save(session)
        self.store.add_message(session.id, "user", "read these", attachments=[
            Attachment("report.pdf", "text", content="[page 1]\nprofits up"),
            Attachment("pic.png", "image", data=b"\x89PNG")])
        text, pic = self.store.messages(session.id)[0].attachments
        _, body = self.post("/api/tags", {"session": session.id, "attachment": text.id, "tag": "q3"})
        self.assertEqual(Path(body["path"]).name, "report.pdf.txt")
        self.assertEqual(Path(body["path"]).read_text(), "[page 1]\nprofits up")
        self.assertEqual(self.store.attachment(text.id).path, str(uploads / "report.pdf.txt"))
        _, body = self.post("/api/tags", {"session": session.id, "attachment": pic.id, "tag": "q3"})
        self.assertEqual(Path(body["path"]).read_bytes(), b"\x89PNG")
        self.assertEqual(self.store.resources(session.id), {"q3": [
            str(uploads / "report.pdf.txt"), str(uploads / "pic.png")]})
        self.assertEqual(self.post("/api/tags", {**soup, "attachment": 999, "tag": "q3"})[0], 400)

    def test_a_kept_upload_cleaned_up_since_is_kept_again_when_tagged(self):
        uploads = Path(os.environ["AC_UPLOADS_DIR"]) / self.store.user
        session = self.store.save(self.store.draft("m1"))
        self.store.add_message(session.id, "user", "read this", attachments=[
            Attachment("report.pdf", "text", content="[page 1]\nprofits up")])
        (a,) = self.store.messages(session.id)[0].attachments
        _, body = self.post("/api/tags", {"session": session.id, "attachment": a.id, "tag": "q3"})
        kept = uploads / "report.pdf.txt"
        self.assertEqual(self.store.attachment(a.id).path, str(kept))
        kept.unlink()                                               # acc uploads --clean
        status, body = self.post("/api/tags", {"session": session.id, "attachment": a.id,
                                               "tag": "q4"})
        self.assertEqual((status, kept.read_text()), (200, "[page 1]\nprofits up"))
        self.assertEqual(self.store.attachment(a.id).path, str(kept))

        # A file of the user's own that has gone is not brought back: it was never ours to keep.
        self.store.add_message(session.id, "user", "and this", attachments=[
            Attachment("/nowhere/notes.md", "text", content="x")])
        b = self.store.messages(session.id)[1].attachments[0]
        status, body = self.post("/api/tags", {"session": session.id, "attachment": b.id, "tag": "x"})
        self.assertEqual((status, body["error"]), (400, "nothing matches /nowhere/notes.md"))

    def test_a_new_chat_keeps_its_tags_until_the_first_message(self):
        root = self.tree()
        notes = str(root / "notes.md")
        status, body = self.post("/api/tags", {"path": notes, "tag": "plan"})     # no session yet
        self.assertEqual((status, body), (200, {"tag": "plan", "path": os.path.realpath(notes),
                                               "portable": notes}))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resources").fetchone()[0], 0)
        tags = quote(json.dumps({"plan": [notes]}))
        _, body = self.get(f"/api/names?tags={tags}")
        self.assertEqual(body["names"][0]["real"], [os.path.realpath(notes)])
        self.assertEqual(self.get(f"/api/match?path=@plan&tags={tags}")[1]["count"], 1)
        self.assertEqual(self.get("/api/names?tags=[1]")[0], 400)

        self.fake.reply("Friday.")
        _, events = self.post("/api/chat", {"text": "when, per @plan ?", "model": "m1",
                                            "tags": {"plan": [notes]}})
        session = next(e["session"] for e in events if e["type"] == "session")
        user = next(e["message"] for e in events if e["type"] == "message")
        self.assertEqual([a["path"] for a in user["attachments"]], [notes])
        self.assertEqual(self.store.resources(session["id"]), {"plan": [notes]})
        self.assertEqual(self.post("/api/chat", {"text": "hi", "tags": {"a b": [notes]}})[0], 400)

    def test_queued_files_keep_the_order_they_were_queued_in(self):
        root = self.tree()
        self.fake.reply("Seen.")
        _, events = self.post("/api/chat", {"session": self.soup.id, "text": "Review", "queue": [
            {"path": str(root / "notes.md")}, {"upload": self.upload("a.txt", b"first upload")},
            {"path": str(root / "src")}, {"upload": self.upload("b.txt", b"second upload")}]})
        user = next(e["message"] for e in events if e["type"] == "message")
        self.assertEqual([a["path"] for a in user["attachments"]],
                         [str(root / "notes.md"), "a.txt", str(root / "src" / "app.py"),
                          str(root / "src" / "deep" / "util.py"), "b.txt"])
        _, body = self.get(f"/api/sessions/{self.soup.id}")        # and so it stays
        self.assertEqual([a["path"] for a in body["messages"][-2]["attachments"]],
                         [a["path"] for a in user["attachments"]])

        before = len(self.store.list())       # a broken upload is refused before anything is kept
        self.assertEqual(self.post("/api/chat", {"text": "hi", "model": "m1", "queue": [
            {"path": str(root / "notes.md")}, {"upload": {"name": "x", "data": "!!"}}]}),
                         (400, {"error": "x didn't arrive intact"}))
        self.assertEqual(self.post("/api/chat", {"text": "hi", "queue": [{"what": 1}]})[0], 400)
        self.assertEqual(len(self.store.list()), before)

    def test_a_folder_typed_and_the_same_folder_browsed_agree(self):
        # However a file is reached, through a symlink or not, the picker sees one place.
        root = self.tree()
        link = root.parent / "shortcut"
        link.symlink_to(root)
        _, browsed = self.get(f"/api/browse?dir={link}")
        _, typed = self.get(f"/api/match?path={root}/src")
        src = next(e for e in browsed["entries"] if e["name"] == "src")
        self.assertEqual(src["path"], str(link / "src"))
        self.assertEqual(src["real"], typed["real"])
        _, typed = self.get(f"/api/match?path={link}/notes.md")
        notes = next(e for e in browsed["entries"] if e["name"] == "notes.md")
        self.assertEqual(notes["real"], typed["real"])

    def test_picked_server_files_go_with_the_message(self):
        root = self.tree()
        self.fake.reply("Seen.")
        _, events = self.post("/api/chat", {
            "session": self.soup.id, "text": "Review",
            "paths": [str(root / "src"), str(root / "notes.md"), str(root / "gone.txt")]})
        user = next(e["message"] for e in events if e["type"] == "message")
        self.assertEqual([a["path"] for a in user["attachments"]],
                         [str(root / "src" / "app.py"), str(root / "src" / "deep" / "util.py"),
                          str(root / "notes.md")])
        notes = [e["text"] for e in events if e["type"] == "note"]
        self.assertIn(f"nothing matches {root}/gone.txt", notes)
        self.assertTrue(any(n.startswith("attached 2 files from ") and "/src/" in n for n in notes), notes)
        sent = self.fake.requests[-1]["messages"][-1]["content"]
        self.assertIn("print('hi')", sent)
        self.assertNotIn("hidden", sent)
        # Naming a picked file in the text too doesn't send it twice.
        self.fake.reply("ok")
        _, events = self.post("/api/chat", {"session": self.soup.id, "text": f"again {root}/notes.md",
                                            "paths": [str(root / "notes.md")]})
        user = next(e["message"] for e in events if e["type"] == "message")
        self.assertEqual(len(user["attachments"]), 1)


    def test_remove_a_file_from_the_conversation(self):
        notes = Path(self.db).parent / "notes.txt"
        notes.write_text("buy leeks")
        self.fake.reply("Noted.")
        _, events = self.post("/api/chat", {"session": self.soup.id, "text": "see",
                                            "paths": [str(notes)]})
        attachment = next(e["message"] for e in events if e["type"] == "message")["attachments"][0]
        self.assertEqual(self.delete(f"/api/attachments/{attachment['id']}"),
                         (200, {"deleted": attachment["id"]}))
        self.assertTrue(notes.exists())                  # only the conversation's copy goes
        self.assertEqual(self.delete(f"/api/attachments/{attachment['id']}")[0], 404)
        self.assertEqual(self.delete("/api/attachments/nope")[0], 404)
        messages = self.store.messages(self.soup.id)
        self.assertEqual([m.content for m in messages], ["Leek soup?", "see", "Noted."])
        self.assertEqual(messages[1].attachments, [])
        # The model doesn't see it again.
        self.fake.reply("ok")
        self.post("/api/chat", {"session": self.soup.id, "text": "and now?"})
        self.assertNotIn("buy leeks", json.dumps(self.fake.requests[-1]))
        status, _ = self.delete(f"/api/attachments/{attachment['id']}", {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)


    def test_delete_one_message(self):
        # Lisbon: 1 user (with a file), 2 assistant. Add another exchange, then drop the first reply.
        self.store.add_message(self.lisbon.id, "user", "And day 2?")
        self.store.add_message(self.lisbon.id, "assistant", "Belém")
        status, body = self.delete(f"/api/sessions/{self.lisbon.id}/messages/2")
        self.assertEqual((status, body["deleted"], body["session"]["message_count"]), (200, 2, 3))
        self.assertEqual([(m.seq, m.content) for m in self.store.messages(self.lisbon.id)],
                         [(1, "Plan three days"), (3, "And day 2?"), (4, "Belém")])
        # Its files go with it.
        status, _ = self.delete(f"/api/sessions/{self.lisbon.id}/messages/1")
        self.assertEqual(status, 200)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0], 0)
        self.assertEqual(self.delete(f"/api/sessions/{self.lisbon.id}/messages/1")[0], 404)
        self.assertEqual(self.delete(f"/api/sessions/{self.lisbon.id}/messages/x")[0], 404)
        self.assertEqual(self.delete("/api/sessions/zzzz/messages/1")[0], 404)
        status, _ = self.delete(f"/api/sessions/{self.lisbon.id}/messages/3", {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        # The next message still gets the next number.
        self.fake.reply("Sintra")
        self.post("/api/chat", {"session": self.lisbon.id, "text": "Day 3?"})
        self.assertEqual([m.seq for m in self.store.messages(self.lisbon.id)], [3, 4, 5, 6])


    def test_archive_and_bring_back(self):
        status, body = self.post(f"/api/sessions/{self.soup.id}", {"archived": True})
        self.assertEqual(status, 200)
        self.assertIsNotNone(body["session"]["archived_at"])
        _, body = self.get("/api/sessions")
        self.assertEqual(([s["id"] for s in body["sessions"]], body["archived_count"]), ([self.lisbon.id], 1))
        _, body = self.get("/api/sessions?archived=1")
        self.assertEqual([s["id"] for s in body["sessions"]], [self.soup.id])
        _, body = self.get("/api/sessions?archived=1&search=leek")
        self.assertEqual([s["id"] for s in body["sessions"]], [self.soup.id])
        self.assertEqual(self.get(f"/api/sessions/{self.soup.id}")[0], 200)   # still opens
        # A new message brings it back.
        self.fake.reply("Leeks and potatoes.")
        _, events = self.post("/api/chat", {"session": self.soup.id, "text": "Recipe?"})
        self.assertIsNone(events[0]["session"]["archived_at"])
        _, body = self.get("/api/sessions")
        self.assertEqual((len(body["sessions"]), body["archived_count"]), (2, 0))
        # And so does unarchiving, which, like archiving, keeps its place in the list.
        before = self.store.get(self.lisbon.id).updated_at
        self.post(f"/api/sessions/{self.lisbon.id}", {"archived": True})
        self.assertEqual(self.store.get(self.lisbon.id).updated_at, before)
        _, body = self.post(f"/api/sessions/{self.lisbon.id}", {"archived": False})
        self.assertIsNone(body["session"]["archived_at"])


    def test_a_session_says_how_much_context_it_uses(self):
        _, body = self.get(f"/api/sessions/{self.soup.id}")
        self.assertEqual(body["usage"], {"used": None, "context": 1000, "exact": True})  # no reply yet
        self.fake.reply("Leeks.")
        self.post("/api/chat", {"session": self.soup.id, "text": "Recipe?"})
        _, body = self.get(f"/api/sessions/{self.soup.id}")
        self.assertEqual(body["usage"], {"used": 18, "context": 1000, "exact": True})

    def test_the_window_of_a_model_not_loaded_is_a_best_guess(self):
        self.fake.reply("Leeks.")
        self.post("/api/chat", {"session": self.soup.id, "text": "Recipe?"})
        self.fake.loaded = []                                   # Ollama has let it go
        usage = lambda: self.get(f"/api/sessions/{self.soup.id}")[1]["usage"]
        self.assertEqual(usage(), {"used": 18, "context": 8192, "exact": False})   # the model's most
        # A fresh server, so nothing is remembered: the Modelfile's num_ctx comes first...
        self.fake.parameters = "temperature                    1\nnum_ctx                        4096"
        self.assertEqual(server.usage(Client(self.fake.host), self.store.get(self.soup.id),
                                      self.store.messages(self.soup.id))["context"], 4096)
        # ...and the session's own num_ctx before that.
        soup = self.store.get(self.soup.id)
        soup.options["num_ctx"] = 2048
        self.store.save(soup)
        self.assertEqual(usage()["context"], 2048)


if __name__ == "__main__":
    unittest.main()
