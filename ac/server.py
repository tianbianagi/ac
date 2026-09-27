"""A local web server for chatting in the browser: `acc serve`.

It listens on 127.0.0.1 only and answers only requests addressed to it by that name (or by a
name passed with --allow-host, for a proxy in front of it), since it can read every session and
the files a message names. Each request opens its own connection
to the database, so the server can answer several at once.

It serves the users config.toml names, each with their own sessions and skills. The
proxy says who is asking in the X-Acc-User header, from the client certificate it checked; a
request without one (as from a browser on this machine) is the owner's.
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
from .chat import RESERVED_OPTIONS, build_messages
from .store import Ambiguous, NotFound, Store, StoreError, first_message_title

mimetypes.add_type("application/manifest+json", ".webmanifest")

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY = 64 << 20     # a message with its uploads, base64 and all
USER_HEADER = "X-Acc-User"
IMAGE_TYPES = [(b"\x89PNG", "image/png"), (b"\xff\xd8", "image/jpeg"), (b"GIF8", "image/gif"),
               (b"RIFF", "image/webp")]


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
                             "size": a.size,
                             "real": os.path.realpath(a.path) if a.path.startswith("/") else None}
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


def read_uploads_queued(queue):
    """What the page queued, in its order, with each upload already read, so a broken one is
    refused before anything is saved: entries {"upload": {"name", "data"}} (see read_uploads)
    become (attachments, notes); entries {"path": ...} stay as the path, for read_queue."""
    if not isinstance(queue, list):
        raise BadRequest("queue must be a list")
    read = []
    for item in queue:
        if isinstance(item, dict) and "upload" in item:
            read.append(read_uploads([item["upload"]]))
        elif isinstance(item, dict) and "path" in item:
            read.append(str(item["path"]))
        else:
            raise BadRequest("each queued entry is an upload or a path")
    return read


def read_queue(read, names):
    """Attachments for what the page queued, in the order it was queued, plus lines the user
    should see: read_uploads_queued's entries, with each path read as path_refs finds it."""
    attachments, notes = [], []
    for item in read:
        if isinstance(item, str):
            refs, problems = path_refs([item], names)
            got, said = files.read_refs(refs, already=attachments)
            said = problems + said
        else:
            got, said = item
        attachments += got
        notes += said
    return attachments, notes


def browse(folder):
    """What a folder on this machine holds, for the page's picker: folders first, then files,
    leaving out hidden, ignored and dependency files. Paths stay as they were reached, so a folder entered
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


def keep_upload(name, data, user=None):
    """Save an uploaded file's bytes into the user's uploads folder under its own name, or beside
    a different file of that name as "name 2.ext"; the same bytes again reuse the first copy."""
    folder = config.uploads_dir(user)
    folder.mkdir(parents=True, exist_ok=True)
    name = Path(name).name or "upload"
    stem, suffix = Path(name).stem, Path(name).suffix
    for n in range(1, 1000):
        path = folder / (name if n == 1 else f"{stem} {n}{suffix}")
        if not path.exists():
            path.write_bytes(data)
            return path
        if path.read_bytes() == data:
            return path
    raise BadRequest(f"too many uploads named {name}")


def in_uploads(path):
    """Whether a path is somewhere in the uploads folder, however either is reached."""
    root = os.path.realpath(config.uploads_dir())
    return os.path.realpath(path).startswith(root + os.sep)


def tagged_path(store, request):
    """The file a tag request is about: {"path"} on this machine, {"upload": {"name", "data"}}
    not sent yet, or {"attachment": ID} already in a conversation. An upload is kept in the
    uploads folder first; one already sent is kept as the model saw it (a PDF as its text), and
    its conversation learns where it now lives. If that kept copy has since been cleaned up
    (`acc uploads --clean`), it is kept again from what the conversation holds."""
    if "upload" in request:
        upload = request["upload"] if isinstance(request["upload"], dict) else {}
        try:
            data = base64.b64decode(str(upload.get("data") or ""), validate=True)
        except (binascii.Error, ValueError):
            raise BadRequest("the file didn't arrive intact") from None
        return str(keep_upload(str(upload.get("name") or ""), data, store.user))
    if "attachment" in request:
        try:
            a = store.attachment(int(request["attachment"]))
        except (TypeError, ValueError, NotFound):
            raise BadRequest("no such attachment") from None
        kept_before = Path(a.path).is_absolute()
        if kept_before and (Path(a.path).exists() or not in_uploads(a.path)):
            return a.path
        name = Path(a.path).name if kept_before else a.path
        if a.kind == "image":
            data = a.data or b""
        else:
            data = (a.content or "").encode()
            if name.lower().endswith(".pdf"):
                name += ".txt"              # a PDF's text is not a PDF
        path = str(keep_upload(name, data, store.user))
        store.move_attachment(a.id, path)
        return path
    return str(request.get("path") or "").strip()


def check_tag(tag):
    tag = str(tag or "").strip().lstrip("@").lower()
    if not files.NAME.fullmatch(tag):
        raise BadRequest("a tag is letters, digits, - and _, starting with a letter or digit")
    return tag


def draft_tags(raw):
    """The tags a new chat's page holds until its first message makes the session, as
    {TAG: [path, ...]}: sent with that message, and with a picker lookup before it."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise BadRequest("bad tags") from None
    if not isinstance(raw, dict):
        raise BadRequest("tags must map each tag to its paths")
    tags = {}
    for tag, paths in raw.items():
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise BadRequest("tags must map each tag to its paths")
        kept = [files.portable(p) for p in paths if p.startswith(("/", "~"))]
        if kept:
            tags[check_tag(tag)] = list(dict.fromkeys(kept))
    return tags


def tag_file(store, request):
    """Tag a file, or untag it, from {"session": ID, "path" | "upload" | "attachment" (see
    tagged_path), "tag": TAG, "remove": bool}. A tag is an @name of that session: @TAG in one of
    its messages attaches every file tagged with it, as they are then. A tag left with no files
    is forgotten; files are never deleted. Without a session (a new chat) nothing is saved: the
    answer's "portable" path is for the page to keep until the first message."""
    tag = check_tag(request.get("tag"))
    session = store.get(str(request["session"])) if request.get("session") else None
    word = tagged_path(store, request)
    if not word.startswith(("/", "~")):
        raise BadRequest("only a file on this machine can be tagged")
    path = files.portable(word)
    if session is None:
        if not request.get("remove") and files.resolve(word, explicit=True) is None:
            raise BadRequest(f"nothing matches {word}")
        return {"tag": tag, "path": os.path.realpath(path), "portable": path}
    paths = store.resources(session.id).get(tag, [])
    if request.get("remove"):             # however it was reached: through a link or not
        paths = [p for p in paths if os.path.realpath(p) != os.path.realpath(path)]
    else:
        if files.resolve(word, explicit=True) is None:
            raise BadRequest(f"nothing matches {word}")
        paths = list(dict.fromkeys(paths + [path]))
    if paths:
        store.set_resource(session.id, tag, paths)
    else:
        store.delete_resource(session.id, tag)
    return {"tag": tag, "paths": paths, "path": os.path.realpath(path)}


def skills_json(user):
    """What the page needs about skills: those a session can attach (a user's own winning over a
    shared one of the same name), and each library's own, for managing them."""
    def listed(found, scope):
        return [{"name": k.name, "description": k.description, "scope": scope}
                for _, k in sorted(found.items())]
    scope_of = {config.personal_skills_dir(user): "personal", config.shared_skills_dir(): "shared"}
    usable = []
    for name, k in sorted(skills.discover(config.skills_dirs(user)).items()):
        usable.append({"name": name, "description": k.description,
                       "scope": scope_of.get(k.path.parent.parent, "other")})
    return {"skills": usable,
            "library": {scope: listed(skills.discover([skills.library(scope, user)]), scope)
                        for scope in skills.SCOPES}}


def skill_json(user, scope, name):
    """One skill of a library, for editing: {"scope", "name", "description", "body"}."""
    path = skills.library(scope, user) / name / skills.FILENAME
    if not skills.NAME.fullmatch(name) or not path.is_file():
        raise NotFound(f"no {scope} skill named '{name}'")
    meta, body = skills.parse(path.read_text(encoding="utf-8"))
    return {"scope": scope, "name": name, "description": meta.get("description", ""), "body": body}


def save_skill(user, request):
    """Create or change a skill from {"scope", "name", "description", "body", "was"?: {"scope",
    "name"}}; see skills.save."""
    was = request.get("was")
    if was is not None and not (isinstance(was, dict) and was.get("scope") and was.get("name")):
        raise BadRequest("was must say the skill's scope and name")
    try:
        skills.save(user, str(request.get("scope") or ""), request.get("name"),
                    request.get("description"), request.get("body"), was)
    except skills.SkillError as e:
        raise BadRequest(str(e)) from None
    return skill_json(user, str(request["scope"]), str(request["name"]).strip())


def update_session(store, client, session, request):
    """Change a session's title, model or skills from {"title": ..., "model": NAME,
    "skills": [REF, ...]}. A title given here is the user's, never replaced by the model's.
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
    streams, then the saved reply. {"session": ID?, "text": ..., "model": ...?, "queue": [...]?}
    sends a message, starting a new session when no id is given, with the files queued for it
    (see read_queue; "files" and "paths" say the same as uploads then paths); {"session": ID,
    "retry": true} asks again for the reply to the last message.

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
    tags = draft_tags(request.get("tags") or {}) if not session.persisted else {}
    queue = request.get("queue")
    if queue is None:
        uploads, paths = request.get("files") or [], request.get("paths") or []
        if not isinstance(uploads, list) or not isinstance(paths, list):
            raise BadRequest("files and paths must be lists")
        queue = [{"upload": f} for f in uploads] + [{"path": p} for p in paths]
    queue = read_uploads_queued(queue) if not retry else []

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
            for tag, paths in tags.items():     # what the new chat's page tagged before this
                store.set_resource(session.id, tag, paths)
        if session.archived_at:             # a new message brings it back, as in a mail inbox
            store.set_archived(session.id, False)
        names = store.resources(session.id)
        queued, queue_notes = read_queue(queue, names)
        found, problems = files.collect(text, names, already=queued)
        attachments = queued + found
        yield {"type": "session", "session": session_json(store.get(session.id))}
        for line in queue_notes + problems + files.announce(attachments):
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


def export(store, client, session, request):
    """A session as markdown, the way `/export` makes it: a session still titled with its first
    message is first named by the model. {"save": true} writes the file into the export
    folder; otherwise the text comes back for the page to offer as a download."""
    if not session.message_count:
        raise BadRequest("nothing to export: this session has no messages yet")
    notes = []
    titles.ensure(store, client, session, notes.append, notes.append)
    notes = [n for n in notes if not n.endswith("…")]  # progress the page already showed
    session = store.get(session.id)
    messages = store.messages(session.id)
    text = render.to_markdown(session, messages, thinking=bool(request.get("thinking")))
    path = render.export_path(session)
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
    allowed_hosts = ()
    quiet = True

    def log_message(self, format, *args):
        if not self.quiet:
            super().log_message(format, *args)

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
            if path == "/api/tags":
                try:
                    return self._json(tag_file(store, request))
                except BadRequest as e:
                    return self._error(400, str(e))
                except (NotFound, Ambiguous) as e:
                    return self._error(404, str(e))
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
            return self._delete_skill(*map(unquote, path[len("/api/skills/"):].partition("/")[::2]))
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

    def _delete_skill(self, scope, name):
        """Delete one of a library's skills. Sessions that attached it are told it's gone."""
        user = self._user()
        try:
            skills.remove(user, scope, name)
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
        """Take one file out of a conversation: the message it came
        with stays, the file on disk is never touched, and the model doesn't see it again."""
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
            archived = (query.get("archived") or [""])[0] in ("1", "true")
            return self._json({
                "sessions": [session_json(s) for s in store.list(search=search, archived=archived)],
                "archived_count": store.archived_count()})
        if path.startswith("/sessions/"):
            session = store.get(unquote(path[len("/sessions/"):]))
            messages = store.messages(session.id)
            return self._json({"session": session_json(session),
                               "messages": [message_json(m) for m in messages],
                               "usage": usage(self.client, session, messages)})
        if path == "/config":
            return self._json({"names": config.speaker_names(store.user), "user": store.user})
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
            return self._json(match((query.get("path") or [""])[0], self._names(store, query)))
        if path == "/names":
            return self._json({"names": [
                {"name": name, "paths": [files.display_path(p) for p in paths],
                 "real": [os.path.realpath(p) for p in paths]}
                for name, paths in self._names(store, query).items()]})
        if path == "/skills":
            return self._json(skills_json(store.user))
        if path.startswith("/skills/"):
            scope, _, name = path[len("/skills/"):].partition("/")
            try:
                return self._json(skill_json(store.user, unquote(scope), unquote(name)))
            except skills.SkillError as e:
                return self._error(404, str(e))
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

    def _names(self, store, query):
        """The @names a lookup may use: those of ?session=ID, or a new chat's ?tags=JSON."""
        ref = (query.get("session") or [""])[0]
        if ref:
            return store.resources(store.get(ref).id)
        return draft_tags((query.get("tags") or ["{}"])[0])

    def _host_ok(self):
        """Only requests addressed to this machine by name: a page elsewhere can't reach the
        server through a hostname that it has pointed at 127.0.0.1. A proxy in front of it
        passes its own name, which --allow-host adds."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        return host in ("127.0.0.1", "localhost", *self.allowed_hosts)

    def _user(self):
        """Who is asking: the user the proxy named, or the owner when it named no one. None
        for a name config.toml doesn't know."""
        name = (self.headers.get(USER_HEADER) or "").strip().lower()
        known = config.users()
        if not name:
            return known[0]
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
