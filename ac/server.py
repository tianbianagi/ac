"""A local web server for chatting in the browser: `acc serve`.

It listens on 127.0.0.1 only and answers only requests addressed to it by that name (or by a
name passed with --allow-host, for a proxy in front of it), since it can read every session.
Each request opens its own connection to the database, so the server can answer several at
once.

It serves the users config.toml names, each with their own sessions and skills, none of
them shared. The proxy says who is asking in the X-Acc-User header, from the client
certificate it checked. A request that reached this machine directly (by 127.0.0.1 or
localhost, as from a browser here) is the owner's; one that came through the proxy and names
nobody is refused, so a proxy that stops saying who is asking can't make everyone the owner.

Files reach a conversation only as part of a message, sent from the device the page is on.
Nothing here names, browses or reads a path on this machine.
"""

import base64
import binascii
import hashlib
import json
import mimetypes
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from importlib import resources
from urllib.parse import parse_qs, unquote, urlsplit

from . import config, files, render, skills, titles
from .ollama import Client, OllamaError, pick_model, resolve_model
from .chat import RESERVED_OPTIONS, build_messages
from .store import Ambiguous, NotFound, Store, StoreError, first_message_title

mimetypes.add_type("application/manifest+json", ".webmanifest")

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY = 64 << 20     # a message with its files, base64 and all
USER_HEADER = "X-Acc-User"
IMAGE_TYPES = [(b"\x89PNG", "image/png"), (b"\xff\xd8", "image/jpeg"), (b"GIF8", "image/gif"),
               (b"RIFF", "image/webp")]
# What the page may load or run: its own script (by hash, so nothing a reply smuggles in can run
# even if the markdown renderer let a tag through), its own styles and images, and this server.
CSP = ("default-src 'self'; script-src 'self' 'sha256-{script}'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")


def csp_for(page):
    """The Content-Security-Policy for the page, naming its inline script by hash."""
    start = page.index(b"<script>") + len(b"<script>")
    script = page[start:page.index(b"</script>", start)]
    return CSP.format(script=base64.b64encode(hashlib.sha256(script).digest()).decode())


def session_json(s):
    return {"id": s.id, "title": s.title, "title_source": s.title_source, "model": s.model,
            "skills": s.skills,
            "system": s.system, "options": s.options, "parent_id": s.parent_id,
            "created_at": s.created_at, "updated_at": s.updated_at,
            "archived_at": s.archived_at, "message_count": s.message_count}


def message_json(m):
    return {"seq": m.seq, "role": m.role, "content": m.content, "thinking": m.thinking,
            "status": m.status, "model": m.model, "skills": m.skills,
            "prompt_tokens": m.prompt_tokens, "eval_tokens": m.eval_tokens,
            "duration_ms": m.duration_ms, "created_at": m.created_at,
            "attachments": [{"id": a.id, "path": a.path, "kind": a.kind, "note": a.note,
                             "size": a.size}
                            for a in m.attachments]}


class BadRequest(Exception):
    pass


def usage(client, session, messages):
    """Context in use as the latest reply left it: {"used": tokens, "context": the model's
    window, "exact": whether that is Ollama's figure for the loaded model or a best guess}."""
    context, exact = client.context_window(session.model, session.options)
    for m in reversed(messages):
        if m.role == "assistant" and m.prompt_tokens is not None:
            return {"used": m.prompt_tokens + (m.eval_tokens or 0), "context": context, "exact": exact}
    return {"used": None, "context": context, "exact": exact}


def read_files(sent):
    """Attachments for the files the page sent with a message as {"name", "data" (base64)},
    plus lines the user should see. Each is read once from a temporary folder, as the same
    file on disk would be, so a PDF gives its text (or page images) and an image goes to the
    model as an image; it is known by its own name, never by a path."""
    if not isinstance(sent, list):
        raise BadRequest("files must be a list")
    attachments, notes = [], []
    with tempfile.TemporaryDirectory(prefix="acc-upload-") as tmp:
        for i, upload in enumerate(sent):
            if not isinstance(upload, dict):
                raise BadRequest("each file needs a name and data")
            name = Path(str(upload.get("name") or "")).name
            if name in ("", ".", ".."):
                name = f"file-{i + 1}"
            try:
                data = base64.b64decode(str(upload.get("data") or ""), validate=True)
            except (binascii.Error, ValueError):
                raise BadRequest(f"{name} didn't arrive intact") from None
            folder = Path(tmp) / str(i)         # its own folder: two files may share a name
            folder.mkdir()
            path = folder / name
            path.write_bytes(data)
            try:
                got, said = files.read_all(path)
            except files.FileError as e:
                notes.append(str(e))
                continue
            for a in got:
                a.path = a.path.replace(str(folder) + "/", "")
            attachments += got
            notes += said
    return attachments, notes


def skills_json(user):
    """What the page needs about skills: those a session can attach, and the user's own
    library, which is what the page can change (AC_SKILLS_PATH skills are the owner's, read
    from disk only)."""
    def listed(found):
        return [{"name": k.name, "description": k.description} for _, k in sorted(found.items())]
    return {"skills": listed(skills.discover(config.skills_dirs(user))),
            "library": listed(skills.discover([skills.library(user)]))}


def skill_json(user, name):
    """One skill of the user's library, for editing: {"name", "description", "body"}."""
    path = skills.library(user) / name / skills.FILENAME
    if not skills.NAME.fullmatch(name) or not path.is_file():
        raise NotFound(f"no skill named '{name}'")
    meta, body = skills.parse(path.read_text(encoding="utf-8"))
    return {"name": name, "description": meta.get("description", ""), "body": body}


def save_skill(user, request):
    """Create or change a skill from {"name", "description", "body", "was"?: NAME}; see
    skills.save."""
    was = request.get("was")
    if was is not None and not isinstance(was, str):
        raise BadRequest("was must be the skill's name until now")
    try:
        skills.save(user, request.get("name"), request.get("description"), request.get("body"), was)
    except skills.SkillError as e:
        raise BadRequest(str(e)) from None
    return skill_json(user, str(request["name"]).strip())


def update_session(store, client, session, request):
    """Change a session's title, model or skills from {"title": ..., "model": NAME,
    "skills": [NAME, ...]}. A title given here is the user's, never replaced by the model's.
    ({"archived": true|false} is handled by the caller, once these have been saved.)"""
    if "title" in request:
        title = " ".join(str(request["title"] or "").split())
        if not title:
            raise BadRequest("a title can't be empty")
        session.title, session.title_source = title, "user"
    if request.get("model"):
        session.model = resolve_model(client, str(request["model"]))
    if "skills" in request:
        if not isinstance(request["skills"], list):
            raise BadRequest("skills must be a list")
        try:
            dirs = config.skills_dirs(store.user)
            session.skills = list(dict.fromkeys(skills.normalize_ref(str(r), dirs)
                                                for r in request["skills"]))
        except skills.SkillError as e:
            raise BadRequest(str(e)) from None
    return session


def chat(store, client, request):
    """One turn, as events for the page: the session, the user's message, the reply as it
    streams, then the saved reply. {"session": ID?, "text": ..., "model": ...?, "files": [...]?}
    sends a message, starting a new session when no id is given, with the files the page sent
    for it (see read_files); {"session": ID, "retry": true} asks again for the reply to the
    last message.

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
        update_session(store, client, session, {"skills": request.get("skills") or []})
    # Read before anything is saved, so a broken file is refused with nothing to undo.
    attachments, notes = read_files(request.get("files") or []) if not retry else ([], [])

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
        if session.archived_at:             # a new message brings it back, as in a mail inbox
            store.set_archived(session.id, False)
        yield {"type": "session", "session": session_json(store.get(session.id))}
        for line in notes + files.announce(attachments):
            yield {"type": "note", "text": line}
        sent = store.add_message(session.id, "user", text, attachments=attachments)
        yield {"type": "message", "message": message_json(sent)}

    system, active, missing = skills.compose_system(session.system, session.skills,
                                                    config.skills_dirs(store.user))
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
        eval_ns, eval_count = stats.get("eval_duration"), stats.get("eval_count")
        context, exact = client.context_window(session.model, session.options)
        yield {"type": "done", "used": used, "context": context, "exact": exact,
               "speed": round(eval_count / (eval_ns / 1e9), 1) if eval_ns and eval_count else None}


class Reply:
    """A reply being written, apart from any page: chat() runs in a thread of its own, so the
    reply goes on when the page that asked for it closes, and a page that opens the session
    later can follow the rest. Pages read its events; stop() ends it the way Stop should."""

    KEEP = 60           # seconds a finished reply stays followable, for a page that just missed it

    def __init__(self):
        self.events = []
        self.session_id = None
        self.reply_from = 0     # where the reply's own events start, for a page that follows later
        self.failure = None     # what went wrong before the first event, as an HTTP error
        self.done = False
        self.finished_at = None
        self.stopping = False
        self.cond = threading.Condition()

    def add(self, event):
        with self.cond:
            self.events.append(event)
            if event["type"] == "message" and event["message"]["role"] == "user":
                self.reply_from = len(self.events)
            self.cond.notify_all()

    def finish(self, failure=None):
        with self.cond:
            self.failure = failure if not self.events else None
            self.done, self.finished_at = True, time.monotonic()
            self.cond.notify_all()

    def stop(self):
        self.stopping = True

    def follow(self, start=0):
        """The events from `start` on, waiting for each, until the reply is done."""
        i = start
        while True:
            with self.cond:
                while i >= len(self.events) and not self.done:
                    self.cond.wait()
                batch, done = self.events[i:], self.done
                i += len(batch)
            yield from batch
            if done:
                return


_replies = {}                   # (user, session id) -> the Reply being written there
_replies_lock = threading.Lock()


def reply_for(user, session_id):
    """The reply being written in a session, or one that finished a moment ago."""
    with _replies_lock:
        now = time.monotonic()
        for key, reply in list(_replies.items()):
            if reply.done and now - reply.finished_at > Reply.KEEP:
                del _replies[key]
        return _replies.get((user, session_id))


def replying(user, session_id):
    reply = reply_for(user, session_id)
    return bool(reply and not reply.done)


def claim(user, session_id, reply):
    """Make `reply` the one being written in a session, unless another still is: checked and
    taken in one step, so two messages sent at once can't both start a reply."""
    with _replies_lock:
        current = _replies.get((user, session_id))
        if current is not None and not current.done:
            return False
        reply.session_id = session_id
        _replies[(user, session_id)] = reply
        return True


def write_reply(db_path, user, client, request, reply):
    """Run chat() to the end into `reply`, whoever is following it. Stopping keeps what came,
    marked interrupted, as leaving the generator does."""
    store = Store(db_path, user)
    failure = None
    try:
        events = chat(store, client, request)
        try:
            for event in events:
                if event["type"] == "session" and reply.session_id is None:  # a new session
                    claim(user, event["session"]["id"], reply)
                if reply.stopping:
                    break
                reply.add(event)
        finally:
            events.close()
    except Exception as e:      # noqa: BLE001 - whatever it was, the page must hear of it
        if reply.events:
            reply.add({"type": "error", "text": str(e) or type(e).__name__})
        failure = e
    finally:
        store.close()
        reply.finish(failure)


def export(store, client, session, request):
    """A session as markdown, the way `/export` makes it, for the page to offer as a download:
    a session still titled with its first message is first named by the model."""
    if not session.message_count:
        raise BadRequest("nothing to export: this session has no messages yet")
    notes = []
    titles.ensure(store, client, session, notes.append, notes.append)
    notes = [n for n in notes if not n.endswith("…")]  # progress the page already showed
    session = store.get(session.id)
    messages = store.messages(session.id)
    text = render.to_markdown(session, messages, thinking=bool(request.get("thinking")))
    return {"session": session_json(session), "notes": notes,
            "filename": render.export_name(session), "text": text}


class Handler(BaseHTTPRequestHandler):
    db_path = None      # set by make_server
    client = None
    allowed_hosts = ()
    quiet = True
    timeout = 60        # seconds a connection may sit silent: a request never finished lets go

    def log_message(self, format, *args):
        if not self.quiet:
            super().log_message(format, *args)

    def log_request(self, code="-", size="-"):
        """Only what went wrong: every page load and API call would fill the log for nothing,
        and behind a proxy the proxy keeps the access log."""
        if isinstance(code, int) and code < 400:
            return
        super().log_request(code, size)

    def do_GET(self):
        if not self._host_ok() or self._user() is None:
            return self._error(403, "forbidden")
        url = urlsplit(self.path)
        path = url.path
        if path == "/sw.js":        # served from the root so it may control the whole app
            return self._static("sw.js")
        if path == "/" or path.startswith("/static/"):
            return self._static("index.html" if path == "/" else path[len("/static/"):])
        if not path.startswith("/api/"):
            return self._error(404, "not found")
        store = self._store()
        try:
            return self._api(store, path[len("/api"):], parse_qs(url.query))
        except (NotFound, Ambiguous) as e:
            return self._error(404, str(e))
        except BadRequest as e:
            return self._error(400, str(e))
        finally:
            store.close()

    def do_POST(self):
        if not self._host_ok() or not self._same_origin() or self._user() is None:
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
        path = urlsplit(self.path).path
        store = self._store()
        try:
            if path == "/api/chat":
                return self._chat(store, request)
            if path == "/api/skills":
                try:
                    return self._json({"skill": save_skill(store.user, request),
                                       **skills_json(store.user)})
                except BadRequest as e:
                    return self._error(400, str(e))
            if path.startswith("/api/sessions/") and path.endswith("/stop"):
                return self._stop(store, unquote(path[len("/api/sessions/"):-len("/stop")]))
            if path.startswith("/api/sessions/") and path.endswith("/export"):
                return self._export(store, unquote(path[len("/api/sessions/"):-len("/export")]),
                                    request)
            if path.startswith("/api/sessions/"):
                return self._update(store, unquote(path[len("/api/sessions/"):]), request)
            return self._error(404, "not found")
        finally:
            store.close()

    def do_DELETE(self):
        if not self._host_ok() or not self._same_origin() or self._user() is None:
            return self._error(403, "forbidden")
        path = urlsplit(self.path).path
        if path.startswith("/api/attachments/"):
            return self._delete_attachment(path[len("/api/attachments/"):])
        if path.startswith("/api/skills/"):
            return self._delete_skill(unquote(path[len("/api/skills/"):]))
        if not path.startswith("/api/sessions/"):
            return self._error(404, "not found")
        ref, _, rest = path[len("/api/sessions/"):].partition("/messages/")
        if rest:
            return self._delete_message(unquote(ref), rest)
        store = self._store()
        try:
            session = store.get(unquote(path[len("/api/sessions/"):]))
            store.delete(session.id)
            return self._json({"deleted": session.id})
        except (NotFound, Ambiguous) as e:
            return self._error(404, str(e))
        finally:
            store.close()

    def _delete_skill(self, name):
        """Delete one of the user's skills. Sessions that attached it are told it's gone."""
        user = self._user()
        try:
            skills.remove(user, name)
        except skills.SkillError as e:
            return self._error(404, str(e))
        return self._json({"deleted": name, **skills_json(user)})

    def _delete_message(self, ref, seq):
        """Take one message out of a conversation, with the files that came with it."""
        store = self._store()
        try:
            session = store.get(ref)
            store.delete_message(session.id, int(seq))
            return self._json({"deleted": int(seq), "session": session_json(store.get(session.id))})
        except ValueError:
            return self._error(404, "no such message")
        except (NotFound, Ambiguous) as e:
            return self._error(404, str(e))
        finally:
            store.close()

    def _delete_attachment(self, ref):
        """Take one file out of a conversation: the message it came with stays, and the model
        doesn't see it again."""
        store = self._store()
        try:
            try:
                attachment = store.attachment(int(ref))
            except (ValueError, NotFound):
                return self._error(404, "no such attachment")
            store.delete_attachment(attachment.id)
            return self._json({"deleted": attachment.id})
        finally:
            store.close()

    def _export(self, store, ref, request):
        try:
            return self._json(export(store, self.client, store.get(ref), request))
        except (NotFound, Ambiguous) as e:
            return self._error(404, str(e))
        except BadRequest as e:
            return self._error(400, str(e))

    def _update(self, store, ref, request):
        try:
            session = update_session(store, self.client, store.get(ref), request)
        except (NotFound, Ambiguous) as e:
            return self._error(404, str(e))
        except BadRequest as e:
            return self._error(400, str(e))
        except OllamaError as e:
            return self._error(400, str(e))
        if request.keys() & {"title", "model", "skills"}:
            store.save(session)             # archiving alone leaves its place in the list
        if "archived" in request:
            store.set_archived(session.id, bool(request["archived"]))
        return self._json({"session": session_json(store.get(session.id))})

    def _chat(self, store, request):
        """Start a reply in its own thread and follow it. The page going away leaves it running."""
        reply = Reply()
        try:
            if request.get("session"):
                session = store.get(str(request["session"]))
                if not claim(store.user, session.id, reply):
                    return self._error(409, "a reply is still being written in this session")
        except (NotFound, Ambiguous) as e:
            return self._error(404, str(e))
        threading.Thread(target=write_reply, daemon=True,
                         args=(self.db_path, store.user, self.client, request, reply)).start()
        events = reply.follow()
        first = next(events, None)
        if first is None:
            e = reply.failure
            if isinstance(e, BadRequest):
                return self._error(400, str(e))
            if isinstance(e, (NotFound, Ambiguous)):
                return self._error(404, str(e))
            if isinstance(e, OllamaError):
                return self._error(502, str(e))
            return self._error(400 if e else 500, str(e) if e else "no reply")
        return self._stream(events, first)

    def _stream(self, events, first=None):
        """Events as JSON lines while they come. A page that goes away just stops reading."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self._protect()
        self.end_headers()
        self.close_connection = True
        try:
            if first is not None:
                self._event(first)
            for event in events:
                self._event(event)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stop(self, store, ref):
        try:
            session = store.get(ref)
        except (NotFound, Ambiguous) as e:
            return self._error(404, str(e))
        reply = reply_for(store.user, session.id)
        if reply and not reply.done:
            reply.stop()
        return self._json({"stopping": bool(reply and not reply.done)})

    def _event(self, event):
        if event["type"] == "error":
            self.log_message("reply failed: %s", event["text"])
        self.wfile.write(json.dumps(event).encode() + b"\n")
        self.wfile.flush()

    def _api(self, store, path, query):
        if path == "/sessions":
            search = (query.get("search") or [""])[0].strip() or None
            archived = (query.get("archived") or [""])[0] in ("1", "true")
            return self._json({
                "sessions": [session_json(s) for s in store.list(search=search, archived=archived)],
                "archived_count": store.archived_count()})
        if path.startswith("/sessions/") and path.endswith("/reply"):
            session = store.get(unquote(path[len("/sessions/"):-len("/reply")]))
            reply = reply_for(store.user, session.id)
            if reply is None:
                raise NotFound("no reply is being written in this session")
            return self._stream(reply.follow(reply.reply_from))
        if path.startswith("/sessions/"):
            session = store.get(unquote(path[len("/sessions/"):]))
            messages = store.messages(session.id)
            return self._json({"session": session_json(session),
                               "messages": [message_json(m) for m in messages],
                               "usage": usage(self.client, session, messages),
                               "replying": replying(store.user, session.id)})
        if path == "/config":
            return self._json({"names": config.speaker_names(store.user), "user": store.user})
        if path == "/models":
            try:
                models = self.client.list_models()
            except OllamaError as e:
                return self._error(502, str(e))
            try:
                default = pick_model(self.client)
            except OllamaError:
                default = None
            return self._json({"default": default, "models": [
                {"name": m["name"], "size": m.get("size"),
                 "parameter_size": (m.get("details") or {}).get("parameter_size")}
                for m in models]})
        if path == "/skills":
            return self._json(skills_json(store.user))
        if path.startswith("/skills/"):
            return self._json(skill_json(store.user, unquote(path[len("/skills/"):])))
        if path.startswith("/attachments/"):
            try:
                a = store.attachment(int(path[len("/attachments/"):]))
            except ValueError:
                return self._error(404, "not found")
            if a.kind != "image" or a.data is None:
                return self._error(404, "not an image")
            kind = next((t for magic, t in IMAGE_TYPES if a.data.startswith(magic)),
                        "application/octet-stream")
            return self._send(200, a.data, kind)
        return self._error(404, "not found")

    LOCAL_HOSTS = ("127.0.0.1", "localhost")

    def _host(self):
        return (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()

    def _host_ok(self):
        """Only requests addressed to this machine by name: a page elsewhere can't reach the
        server through a hostname that it has pointed at 127.0.0.1. A proxy in front of it
        passes its own name, which --allow-host adds."""
        return self._host() in (*self.LOCAL_HOSTS, *self.allowed_hosts)

    def _user(self):
        """Who is asking: the user the proxy named, or the owner for a request that reached
        this machine directly and names no one. None for a name config.toml doesn't know, and
        for a request through the proxy that names no one: the proxy must always say, so that
        losing that (a dropped header line, a certificate without a user) refuses the request
        rather than making it the owner's."""
        name = (self.headers.get(USER_HEADER) or "").strip().lower()
        known = config.users()
        if not name:
            return known[0] if self._host() in self.LOCAL_HOSTS else None
        return name if name in known else None

    def _store(self):
        return Store(self.db_path, self._user())

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
        self._send(200, body, kind + ("; charset=utf-8" if kind.startswith("text/") else ""),
                   csp=csp_for(body) if name == "index.html" else None)

    def _json(self, data, status=200):
        self._send(status, json.dumps(data).encode(), "application/json")

    def _error(self, status, message):
        self._json({"error": message}, status)

    def _protect(self):
        """Headers every answer carries: nothing is cached, sniffed or sent on as a referrer."""
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send(self, status, body, kind, csp=None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self._protect()
        if csp:
            self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                            # the page went away before it read the answer


def make_server(port=DEFAULT_PORT, db_path=None, client=None, quiet=True, allowed_hosts=()):
    handler = type("BoundHandler", (Handler,),
                   {"db_path": str(db_path or config.db_path()), "client": client or Client(),
                    "quiet": quiet, "allowed_hosts": tuple(h.lower() for h in allowed_hosts)})
    Store(handler.db_path).close()      # create or upgrade the database before any request
    return ThreadingHTTPServer((HOST, port), handler)


def serve(port=DEFAULT_PORT, open_browser=True, say=print, allowed_hosts=()):
    server = make_server(port, quiet=False, allowed_hosts=allowed_hosts)
    url = f"http://{HOST}:{server.server_address[1]}/"
    say(f"{config.COMMAND} is serving {url}  (Ctrl-C stops it)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
