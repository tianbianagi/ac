"""A local web server for chatting in the browser: `acc serve`.

It listens on 127.0.0.1 only and answers only requests addressed to it by that name, since it
can read every session and the files a message names. Each request opens its own connection
to the database, so the server can answer several at once.
"""

import json
import mimetypes
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, unquote, urlsplit

from . import config, files, skills
from .ollama import Client, OllamaError, pick_model
from .repl import RESERVED_OPTIONS, build_messages
from .store import Ambiguous, NotFound, Store, StoreError, first_message_title

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY = 1 << 20


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


class BadRequest(Exception):
    pass


def chat(store, client, request):
    """One turn, as events for the page: the session, the user's message, the reply as it
    streams, then the saved reply. {"session": ID?, "text": ..., "model": ...?} sends a
    message, starting a new session when no id is given; {"session": ID, "retry": true} asks
    again for the reply to the last message.

    Closing the generator mid-reply is the page going away: the partial reply is kept, marked
    interrupted, as Ctrl-C keeps it in the terminal.
    """
    text = str(request.get("text") or "").strip()
    retry = bool(request.get("retry"))
    if not retry and not text:
        raise BadRequest("nothing to send")
    if request.get("session"):
        session = store.get(str(request["session"]))
    elif retry:
        raise BadRequest("nothing to retry: no session given")
    else:
        session = store.draft(pick_model(client, request.get("model") or None))

    if retry:
        messages = store.messages(session.id)
        if messages and messages[-1].role == "assistant":
            store.delete_messages_from(session.id, messages[-1].seq)
            messages.pop()
        if not messages or messages[-1].role != "user":
            raise BadRequest("nothing to retry")
        yield {"type": "session", "session": session_json(store.get(session.id))}
    else:
        if not session.persisted:
            session.title, session.title_source = first_message_title(text), "auto"
            store.save(session)
        attachments, problems = files.collect(text, store.resources())
        yield {"type": "session", "session": session_json(store.get(session.id))}
        for line in problems + files.announce(attachments):
            yield {"type": "note", "text": line}
        sent = store.add_message(session.id, "user", text, attachments=attachments)
        yield {"type": "message", "message": message_json(sent)}

    system, active, missing = skills.compose_system(session.system, session.skills)
    for ref in missing:
        yield {"type": "note", "text": f"skill '{skills.label(ref)}' can't be loaded; "
                                       "continuing without it"}
    history = store.messages(session.id)
    images = sum(a.kind == "image" for m in history for a in m.attachments)
    vision = not images or client.supports(session.model, "vision")
    if not vision:
        yield {"type": "note", "text": f"{session.model} can't see images; leaving {images} out"}

    options = {k: v for k, v in session.options.items() if k not in RESERVED_OPTIONS}
    content, thinking, stats, status, error = [], [], {}, "interrupted", None
    try:
        stream = client.chat(session.model, build_messages(system, history, vision),
                             think=session.options.get("think"), options=options,
                             keep_alive=session.options.get("keep_alive"))
        try:
            for kind, data in stream:
                if kind == "done":
                    stats = data
                    continue
                (thinking if kind == "thinking" else content).append(data)
                yield {"type": kind, "text": data}
            status = "complete"
        finally:
            stream.close()
    except OllamaError as e:
        status, error = "error", e
    finally:
        reply = None
        if content or thinking:
            total_ns = stats.get("total_duration")
            reply = store.add_message(
                session.id, "assistant", "".join(content), thinking="".join(thinking) or None,
                status=status, model=session.model,
                skills=[{"name": k.name, "sha": k.sha} for k in active],
                prompt_tokens=stats.get("prompt_eval_count"), eval_tokens=stats.get("eval_count"),
                duration_ms=total_ns // 1_000_000 if total_ns else None)
    if reply is not None:
        yield {"type": "message", "message": message_json(reply)}
    if error is not None:
        yield {"type": "error", "text": str(error)}
    else:
        used = None
        if stats.get("prompt_eval_count") is not None:
            used = stats["prompt_eval_count"] + stats.get("eval_count", 0)
        yield {"type": "done", "used": used,
               "context": client.context_length(session.model)}


class Handler(BaseHTTPRequestHandler):
    db_path = None      # set by make_server
    client = None
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

    def do_POST(self):
        if not self._host_ok() or not self._same_origin():
            return self._error(403, "forbidden")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._error(415, "send JSON")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return self._error(400, "bad request body")
        try:
            request = json.loads(self.rfile.read(length))
        except ValueError:
            return self._error(400, "bad JSON")
        if not isinstance(request, dict):
            return self._error(400, "bad JSON")
        if urlsplit(self.path).path != "/api/chat":
            return self._error(404, "not found")
        store = Store(self.db_path)
        try:
            self._chat(store, request)
        finally:
            store.close()

    def _chat(self, store, request):
        events = chat(store, self.client, request)
        try:
            first = next(events)
        except StopIteration:
            return self._error(500, "no reply")
        except BadRequest as e:
            return self._error(400, str(e))
        except (NotFound, Ambiguous) as e:
            return self._error(404, str(e))
        except (StoreError, OllamaError) as e:
            return self._error(502 if isinstance(e, OllamaError) else 400, str(e))
        # Events go out as JSON lines while the reply streams; the connection's end ends them.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        try:
            self._event(first)
            for event in events:
                self._event(event)
        except (BrokenPipeError, ConnectionResetError):
            events.close()          # the page went away: keep what came, marked interrupted
        except (StoreError, OllamaError) as e:
            try:
                self._event({"type": "error", "text": str(e)})
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _event(self, event):
        self.wfile.write(json.dumps(event).encode() + b"\n")
        self.wfile.flush()

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

    def _same_origin(self):
        """A change must come from this server's own page, not from a form on another site."""
        origin = self.headers.get("Origin")
        return origin is None or urlsplit(origin).netloc == self.headers.get("Host")

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


def make_server(port=DEFAULT_PORT, db_path=None, client=None, quiet=True):
    handler = type("BoundHandler", (Handler,),
                   {"db_path": str(db_path or config.db_path()), "client": client or Client(),
                    "quiet": quiet})
    Store(handler.db_path).close()      # create or upgrade the database before any request
    return ThreadingHTTPServer((HOST, port), handler)


def serve(port=DEFAULT_PORT, open_browser=True, say=print):
    server = make_server(port, quiet=False)
    url = f"http://{HOST}:{server.server_address[1]}/"
    say(f"{config.COMMAND} is serving {url}  (Ctrl-C stops it)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
