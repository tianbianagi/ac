"""A local web server for reading sessions in the browser: `acc serve`.

It listens on 127.0.0.1 only and answers only requests addressed to it by that name, since it
can read every session (and, later, files). Each request opens its own connection to the
database, so the server can answer several at once.
"""

import json
import mimetypes
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, unquote, urlsplit

from . import config
from .store import Ambiguous, NotFound, Store

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def session_json(s):
    return {"id": s.id, "title": s.title, "model": s.model, "skills": s.skills,
            "system": s.system, "options": s.options, "parent_id": s.parent_id,
            "created_at": s.created_at, "updated_at": s.updated_at,
            "message_count": s.message_count}


def message_json(m):
    return {"seq": m.seq, "role": m.role, "content": m.content, "thinking": m.thinking,
            "status": m.status, "model": m.model, "skills": m.skills,
            "prompt_tokens": m.prompt_tokens, "eval_tokens": m.eval_tokens,
            "duration_ms": m.duration_ms, "created_at": m.created_at,
            "attachments": [{"path": a.path, "kind": a.kind, "note": a.note, "size": a.size}
                            for a in m.attachments]}


class Handler(BaseHTTPRequestHandler):
    db_path = None      # set by make_server
    quiet = True

    def log_message(self, format, *args):
        if not self.quiet:
            super().log_message(format, *args)

    def do_GET(self):
        if not self._host_ok():
            return self._error(403, "forbidden")
        url = urlsplit(self.path)
        path = url.path
        if path == "/" or path.startswith("/static/"):
            return self._static("index.html" if path == "/" else path[len("/static/"):])
        if not path.startswith("/api/"):
            return self._error(404, "not found")
        store = Store(self.db_path)
        try:
            return self._api(store, path[len("/api"):], parse_qs(url.query))
        except (NotFound, Ambiguous) as e:
            return self._error(404, str(e))
        finally:
            store.close()

    def _api(self, store, path, query):
        if path == "/sessions":
            search = (query.get("search") or [""])[0].strip() or None
            return self._json({"sessions": [session_json(s) for s in store.list(search=search)]})
        if path.startswith("/sessions/"):
            session = store.get(unquote(path[len("/sessions/"):]))
            return self._json({"session": session_json(session),
                               "messages": [message_json(m) for m in store.messages(session.id)]})
        if path == "/config":
            return self._json({"names": config.speaker_names()})
        return self._error(404, "not found")

    def _host_ok(self):
        """Only requests addressed to this machine by name: a page elsewhere can't reach the
        server through a hostname that it has pointed at 127.0.0.1."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def _static(self, name):
        if "/" in name or name.startswith("."):
            return self._error(404, "not found")
        try:
            body = resources.files("ac").joinpath("web", name).read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            return self._error(404, "not found")
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self._send(200, body, kind + ("; charset=utf-8" if kind.startswith("text/") else ""))

    def _json(self, data, status=200):
        self._send(status, json.dumps(data).encode(), "application/json")

    def _error(self, status, message):
        self._json({"error": message}, status)

    def _send(self, status, body, kind):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def make_server(port=DEFAULT_PORT, db_path=None, quiet=True):
    handler = type("BoundHandler", (Handler,),
                   {"db_path": str(db_path or config.db_path()), "quiet": quiet})
    Store(handler.db_path).close()      # create or upgrade the database before any request
    return ThreadingHTTPServer((HOST, port), handler)


def serve(port=DEFAULT_PORT, open_browser=True, say=print):
    server = make_server(port, quiet=False)
    url = f"http://{HOST}:{server.server_address[1]}/"
    say(f"{config.COMMAND} is serving {url}  (Ctrl-C stops it)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
