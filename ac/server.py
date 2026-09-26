"""A local web server for chatting in the browser: `acc serve`.

It listens on 127.0.0.1 only and answers only requests addressed to it by that name, since it
can read every session and the files a message names. Each request opens its own connection
to the database, so the server can answer several at once.
"""

import base64
import binascii
import json
import mimetypes
import os
import tempfile
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from importlib import resources
from urllib.parse import parse_qs, unquote, urlsplit

from . import config, files, render, skills, titles
from .ollama import Client, OllamaError, pick_model, resolve_model
from .repl import RESERVED_OPTIONS, build_messages
from .store import Ambiguous, NotFound, Store, StoreError, first_message_title

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY = 64 << 20     # a message with its uploads, base64 and all
IMAGE_TYPES = [(b"\x89PNG", "image/png"), (b"\xff\xd8", "image/jpeg"), (b"GIF8", "image/gif"),
               (b"RIFF", "image/webp")]


def session_json(s):
    return {"id": s.id, "title": s.title, "title_source": s.title_source, "model": s.model,
            "skills": s.skills,
            "system": s.system, "options": s.options, "parent_id": s.parent_id,
            "created_at": s.created_at, "updated_at": s.updated_at,
            "message_count": s.message_count}


def message_json(m):
    return {"seq": m.seq, "role": m.role, "content": m.content, "thinking": m.thinking,
            "status": m.status, "model": m.model, "skills": m.skills,
            "prompt_tokens": m.prompt_tokens, "eval_tokens": m.eval_tokens,
            "duration_ms": m.duration_ms, "created_at": m.created_at,
            "attachments": [{"id": a.id, "path": a.path, "kind": a.kind, "note": a.note,
                             "size": a.size} for a in m.attachments]}


class BadRequest(Exception):
    pass


def read_uploads(uploads):
    """Attachments for files the page sent as {"name", "data" (base64)}, plus lines the user
    should see. Each is read as the same file on disk would be, so a PDF gives its text (or
    page images) and an image goes to the model as an image; it is known by its own name."""
    if not isinstance(uploads, list):
        raise BadRequest("files must be a list")
    attachments, notes = [], []
    with tempfile.TemporaryDirectory(prefix="acc-upload-") as tmp:
        for i, upload in enumerate(uploads):
            if not isinstance(upload, dict):
                raise BadRequest("each file needs a name and data")
            name = Path(str(upload.get("name") or f"file-{i + 1}")).name or f"file-{i + 1}"
            try:
                data = base64.b64decode(str(upload.get("data") or ""), validate=True)
            except (binascii.Error, ValueError):
                raise BadRequest(f"{name} didn't arrive intact") from None
            folder = Path(tmp) / str(i)         # its own folder: two uploads may share a name
            folder.mkdir()
            path = folder / name
            path.write_bytes(data)
            try:
                got, said = files.read_all(path)
            except files.FileError as e:
                notes.append(str(e).replace(str(folder) + "/", ""))
                continue
            for a in got:
                a.path = a.path.replace(str(folder) + "/", "")
            attachments += got
            notes += [line.replace(str(folder) + "/", "") for line in said]
    return attachments, notes


def path_refs(paths, names):
    """Refs for what the page's server-file picker queued, plus lines the user should see.

    Each entry is a path on this machine as the picker gives it: a file is attached as it is,
    a folder with everything in it (the way folder/** would be, so hidden, ignored and binary
    files stay out), a pattern with what it matches, and @NAME with what the name stands for.
    """
    if not isinstance(paths, list):
        raise BadRequest("paths must be a list")
    refs, problems = [], []
    for raw in paths:
        word = str(raw).strip()
        if not word:
            continue
        if word.startswith("@") and word[1:].lower() in names:
            refs += files.named_refs(word[1:].lower(), names, problems)
            continue
        folder = Path(word).expanduser()
        if "*" not in word and folder.is_dir():
            ref = files.resolve(os.path.join(folder, "**"), explicit=True)
            if ref is None:
                problems.append(f"nothing in {files.display_path(folder)}/ to attach")
            else:
                refs.append(ref._replace(label=files.display_path(folder.resolve()) + "/"))
            continue
        ref = files.resolve(word, explicit=True)
        if ref is None:
            problems.append(f"nothing matches {word}")
        else:
            refs.append(ref)
    return refs, problems


def browse(folder):
    """What a folder on this machine holds, for the page's picker: folders first, then files,
    leaving out what /files leaves out. Paths stay as they were reached, so a folder entered
    through a symlink (~/accspace) keeps that name rather than where the link points."""
    path = Path(os.path.abspath(Path(folder or "~").expanduser()))
    if not path.is_dir():
        raise NotFound(f"{folder} isn't a folder here")
    entries = []
    for found in files.here(path):
        item = path / found.name
        try:
            is_dir = item.is_dir()
            entries.append({"name": item.name, "path": str(item), "real": os.path.realpath(item),
                            "dir": is_dir,
                            "size": None if is_dir else item.stat().st_size})
        except OSError:
            continue
    return {"dir": str(path), "display": files.display_path(path),
            "parent": str(path.parent) if path.parent != path else None,
            "home": os.path.abspath(Path.home()), "entries": entries}


MAX_COVERS = 5000


def match(word, names):
    """What one picker entry covers, before it is queued, so the page can show it however it
    was chosen, ticked in a folder or typed: {"kind", "count", "size", "path" (the entry as
    the page should send it), "real" (where a file or folder really is), "covers" (the files
    it brings, by where they really are, up to MAX_COVERS)}."""
    word = word.strip()
    refs, problems = path_refs([word], names)
    if not refs:
        raise NotFound(problems[0] if problems else f"nothing matches {word}")
    kind = ("name" if word.startswith("@") else "pattern" if "*" in word
            else "folder" if Path(word).expanduser().is_dir() else "file")
    path = word if kind == "name" else os.path.abspath(os.path.expanduser(word))
    paths = [p for r in refs for p in (r.matches if r.matches is not None else [r.path])]
    size = 0
    for p in paths:
        try:
            size += p.stat().st_size
        except OSError:
            pass
    return {"kind": kind, "count": len(paths), "size": size, "path": path,
            "real": os.path.realpath(path) if kind in ("file", "folder") else None,
            "covers": [os.path.realpath(p) for p in paths[:MAX_COVERS]]}


def update_session(store, client, session, request):
    """Change a session's title, model or skills from {"title": ..., "model": NAME,
    "skills": [REF, ...]}. A title given here is the user's, never replaced by the model's."""
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
            session.skills = list(dict.fromkeys(skills.normalize_ref(str(r))
                                                for r in request["skills"]))
        except skills.SkillError as e:
            raise BadRequest(str(e)) from None
    return session


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
        update_session(store, client, session, {"skills": request.get("skills") or []})
    uploaded, upload_notes = read_uploads(request.get("files") or []) if not retry else ([], [])

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
        names = store.resources()
        refs, picked_problems = path_refs(request.get("paths") or [], names)
        picked, picked_notes = files.read_refs(refs, already=uploaded)
        found, problems = files.collect(text, names, already=uploaded + picked)
        attachments = uploaded + picked + found
        problems = picked_problems + picked_notes + problems
        yield {"type": "session", "session": session_json(store.get(session.id))}
        for line in upload_notes + problems + files.announce(attachments):
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


def export(store, client, session, request):
    """A session as markdown or JSON, the way `/export` makes it: a session still titled with
    its first message is first named by the model. {"save": true} writes the file into the
    export folder; otherwise the text comes back for the page to offer as a download."""
    fmt = request.get("format") or "md"
    if fmt not in ("md", "json"):
        raise BadRequest("format is md or json")
    if not session.message_count:
        raise BadRequest("nothing to export: this session has no messages yet")
    notes = []
    titles.ensure(store, client, session, notes.append, notes.append)
    notes = [n for n in notes if not n.endswith("…")]  # progress the page already showed
    session = store.get(session.id)
    messages = store.messages(session.id)
    text = (render.to_json(session, messages) if fmt == "json"
            else render.to_markdown(session, messages, thinking=bool(request.get("thinking"))))
    path = render.export_path(session, fmt)
    result = {"session": session_json(session), "notes": notes, "filename": path.name}
    if request.get("save"):
        try:
            render.write_export(path, text)
        except OSError as e:
            raise BadRequest(f"can't write {files.display_path(path)}: {e.strerror or e}") from None
        result["path"] = files.display_path(path)
    else:
        result["text"] = text
    return result


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
        path = urlsplit(self.path).path
        store = Store(self.db_path)
        try:
            if path == "/api/chat":
                return self._chat(store, request)
            if path.startswith("/api/sessions/") and path.endswith("/export"):
                return self._export(store, unquote(path[len("/api/sessions/"):-len("/export")]),
                                    request)
            if path.startswith("/api/sessions/"):
                return self._update(store, unquote(path[len("/api/sessions/"):]), request)
            return self._error(404, "not found")
        finally:
            store.close()

    def do_DELETE(self):
        if not self._host_ok() or not self._same_origin():
            return self._error(403, "forbidden")
        path = urlsplit(self.path).path
        if path.startswith("/api/attachments/"):
            return self._delete_attachment(path[len("/api/attachments/"):])
        if not path.startswith("/api/sessions/"):
            return self._error(404, "not found")
        store = Store(self.db_path)
        try:
            session = store.get(unquote(path[len("/api/sessions/"):]))
            store.delete(session.id)
            return self._json({"deleted": session.id})
        except (NotFound, Ambiguous) as e:
            return self._error(404, str(e))
        finally:
            store.close()

    def _delete_attachment(self, ref):
        """Take one file out of a conversation, as Ctrl-D does in /files: the message it came
        with stays, the file on disk is never touched, and the model doesn't see it again."""
        store = Store(self.db_path)
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
        store.save(session)
        return self._json({"session": session_json(store.get(session.id))})

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
        except (StoreError, OllamaError, skills.SkillError) as e:
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
        if event["type"] == "error":
            self.log_message("reply failed: %s", event["text"])
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
        if path == "/models":
            try:
                models = self.client.list_models()
            except OllamaError as e:
                return self._error(502, str(e))
            return self._json({"models": [
                {"name": m["name"], "size": m.get("size"),
                 "parameter_size": (m.get("details") or {}).get("parameter_size")}
                for m in models]})
        if path == "/browse":
            return self._json(browse((query.get("dir") or [""])[0]))
        if path == "/match":
            return self._json(match((query.get("path") or [""])[0], store.resources()))
        if path == "/names":
            return self._json({"names": [
                {"name": name, "paths": [files.display_path(p) for p in paths]}
                for name, paths in store.resources().items()]})
        if path == "/skills":
            return self._json({"skills": [{"name": s.name, "description": s.description}
                                          for _, s in sorted(skills.discover().items())]})
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
