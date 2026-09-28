"""SQLite persistence for sessions and messages."""

import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE sessions (
    id            TEXT PRIMARY KEY,
    title         TEXT,
    model         TEXT NOT NULL,
    system        TEXT,
    options       TEXT NOT NULL DEFAULT '{}',
    parent_id     TEXT,
    forked_at_seq INTEGER,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE session_skills (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ref        TEXT NOT NULL,
    position   INTEGER NOT NULL,
    PRIMARY KEY (session_id, ref)
);
CREATE TABLE messages (
    id            INTEGER PRIMARY KEY,
    session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    role          TEXT NOT NULL,
    content       TEXT NOT NULL,
    thinking      TEXT,
    status        TEXT NOT NULL DEFAULT 'complete',
    model         TEXT,
    skills        TEXT,
    prompt_tokens INTEGER,
    eval_tokens   INTEGER,
    duration_ms   INTEGER,
    created_at    TEXT NOT NULL,
    UNIQUE (session_id, seq)
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE messages_fts USING fts5(content, content='messages', content_rowid='id');
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER messages_au AFTER UPDATE OF content ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

ATTACHMENTS_SCHEMA = """
CREATE TABLE attachments (
    id         INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    position   INTEGER NOT NULL,
    path       TEXT NOT NULL,
    kind       TEXT NOT NULL,   -- text | image | directory
    content    TEXT,            -- text and directory listings
    data       BLOB,            -- image bytes
    note       TEXT             -- e.g. how much of a large file was kept
);
CREATE INDEX attachments_message ON attachments(message_id);
"""

# Who wrote the title: "auto" (the first message), "model" or "user".
TITLE_SOURCE_SCHEMA = "ALTER TABLE sessions ADD COLUMN title_source TEXT;"

# History: names (later tags) the user gave to paths on this machine. Gone since files only
# come with a message; the tables are dropped by DROP_RESOURCES_SCHEMA below.
RESOURCES_SCHEMA = """
CREATE TABLE resources (
    name       TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# When a session was archived: out of the lists, kept whole, and back with a new message.
ARCHIVED_SCHEMA = "ALTER TABLE sessions ADD COLUMN archived_at TEXT;"

# Whose each session is: acc can serve several people, who never see each other's.
# What was there before belongs to the owner (filled in by _backfill_owner).
OWNER_SCHEMA = """
ALTER TABLE sessions ADD COLUMN owner TEXT NOT NULL DEFAULT '';
CREATE INDEX sessions_owner ON sessions(owner, updated_at);
CREATE TABLE owned_resources (
    owner      TEXT NOT NULL,
    name       TEXT NOT NULL,
    path       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (owner, name)
);
INSERT INTO owned_resources SELECT '', name, path, created_at FROM resources;
DROP TABLE resources;
ALTER TABLE owned_resources RENAME TO resources;
"""

SESSION_RESOURCES_SCHEMA = """
CREATE TABLE session_resources (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    path       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, name)
);
INSERT INTO session_resources
    SELECT s.id, r.name, r.path, r.created_at FROM resources r JOIN sessions s ON s.owner = r.owner;
DROP TABLE resources;
ALTER TABLE session_resources RENAME TO resources;
"""

RELEASED_SCHEMA = """
CREATE TABLE released (
    path TEXT PRIMARY KEY,
    at   TEXT NOT NULL
);
"""

# Files come only with a message now, so nothing on this machine is named or kept for later.
DROP_RESOURCES_SCHEMA = """
DROP TABLE resources;
DROP TABLE released;
"""

# MIGRATIONS[n] upgrades a database from user_version n to n + 1.
MIGRATIONS = [SCHEMA, ATTACHMENTS_SCHEMA, TITLE_SOURCE_SCHEMA, RESOURCES_SCHEMA, ARCHIVED_SCHEMA,
              OWNER_SCHEMA, SESSION_RESOURCES_SCHEMA, RELEASED_SCHEMA, DROP_RESOURCES_SCHEMA]


class StoreError(Exception):
    pass


class NotFound(StoreError):
    pass


class Ambiguous(StoreError):
    pass


@dataclass
class Session:
    id: str
    model: str
    owner: str | None = None
    title: str | None = None
    title_source: str | None = None
    system: str | None = None
    options: dict = field(default_factory=dict)
    skills: list = field(default_factory=list)
    parent_id: str | None = None
    forked_at_seq: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    archived_at: str | None = None
    message_count: int = 0
    persisted: bool = False


@dataclass
class Attachment:
    """A file's contents as they were when the message was sent."""
    path: str
    kind: str
    content: str | None = None
    data: bytes | None = None
    note: str | None = None
    id: int | None = field(default=None, compare=False)  # its row, once it has been saved

    @property
    def size(self):
        return len(self.data) if self.data is not None else len((self.content or "").encode())


@dataclass
class Message:
    id: int
    session_id: str
    seq: int
    role: str
    content: str
    thinking: str | None = None
    status: str = "complete"
    model: str | None = None
    skills: list | None = None
    prompt_tokens: int | None = None
    eval_tokens: int | None = None
    duration_ms: int | None = None
    created_at: str | None = None
    attachments: list = field(default_factory=list)


def plain_text(markdown):
    """Markdown as the words alone: no fences, markers, emphasis or link targets."""
    lines = []
    for line in markdown.replace("\r", "").split("\n"):
        line = line.strip()
        if re.fullmatch(r"(```|~~~).*|([-*_])(\s*\2){2,}|\|?[\s:|-]+\|?", line):
            continue                                 # a fence, a rule or a table's divider
        line = re.sub(r"^((#{1,6}|>|[-*+]|\d+[.)])\s+)+", "", line)   # headings, quotes, items
        line = re.sub(r"^\||\|$", "", line).replace("|", " ")          # table cells
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)           # a link's text
        line = re.sub(r"(\*\*|__|~~|`)(.+?)\1", r"\2", line)           # bold, strike, code
        line = re.sub(r"(?<![\w*])[*_](?=\S)(.+?)(?<=\S)[*_](?![\w*])", r"\1", line)  # italics
        lines.append(line)
    return " ".join(" ".join(lines).split())


def first_message_title(text, width=60):
    """The automatic title a session gets from its first message: its words, without markdown."""
    text = plain_text(text)
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


def _raw_title(text, width=60):
    """The automatic title as it was made before markdown was stripped, so the migration
    still knows an old session's title for automatic."""
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _fts_query(text):
    """Quote every token so user input can't be parsed as FTS5 syntax."""
    return " ".join('"' + tok.replace('"', '""') + '"' for tok in text.split())


class Store:
    """One user's view of the database: their sessions, never anyone else's.
    The owner's unless another user is given."""

    def __init__(self, path, user=None):
        self.user = user or config.owner()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self._migrate()
        self.fts = self._has_table("messages_fts")

    def close(self):
        self.db.close()

    def _has_table(self, name):
        row = self.db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone()
        return row is not None

    def _migrate(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        for step in range(version, len(MIGRATIONS)):
            self.db.executescript(MIGRATIONS[step])
            if step == 0:
                try:
                    self.db.executescript(FTS_SCHEMA)
                except sqlite3.OperationalError:
                    pass  # SQLite built without FTS5: search falls back to LIKE
            if MIGRATIONS[step] is TITLE_SOURCE_SCHEMA:
                self._backfill_title_source()
            if MIGRATIONS[step] is OWNER_SCHEMA:
                self._backfill_owner()
            self.db.execute(f"PRAGMA user_version = {step + 1}")
            self.db.commit()

    def _backfill_title_source(self):
        """Older sessions didn't record who titled them. A title that isn't simply the first
        message must have been chosen by the user, and so must never be replaced."""
        rows = self.db.execute(
            """SELECT s.id, s.title, (SELECT content FROM messages m WHERE m.session_id = s.id
                                      AND m.role = 'user' ORDER BY seq LIMIT 1) AS first
               FROM sessions s WHERE s.title IS NOT NULL""").fetchall()
        for row in rows:
            # Older versions titled with the raw first message, markdown and all.
            automatic = row["first"] is not None and row["title"] in (
                first_message_title(row["first"]), _raw_title(row["first"]))
            self.db.execute("UPDATE sessions SET title_source = ? WHERE id = ?",
                            ("auto" if automatic else "user", row["id"]))

    def _backfill_owner(self):
        """Everything from before acc knew about users is the owner's."""
        for table in ("sessions", "resources"):
            self.db.execute(f"UPDATE {table} SET owner = ? WHERE owner = ''", (config.owner(),))

    # -- sessions ---------------------------------------------------------

    def draft(self, model, *, title=None, system=None, options=None, skills=()):
        """A new session that is not written until save(), so abandoned launches leave nothing."""
        return Session(id=secrets.token_hex(4), model=model, owner=self.user, title=title,
                       system=system,
                       title_source="user" if title else None,
                       options=dict(options or {}), skills=list(skills))

    def save(self, session):
        now = _now()
        session.created_at = session.created_at or now
        session.updated_at = now
        with self.db:
            saved = self.db.execute(
                """INSERT INTO sessions (id, owner, title, title_source, model, system, options,
                                         parent_id, forked_at_seq, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       title = excluded.title, title_source = excluded.title_source,
                       model = excluded.model, system = excluded.system,
                       options = excluded.options, updated_at = excluded.updated_at
                   WHERE sessions.owner = excluded.owner""",
                (session.id, self.user, session.title, session.title_source, session.model,
                 session.system, json.dumps(session.options), session.parent_id,
                 session.forked_at_seq, session.created_at, session.updated_at)).rowcount
            if not saved:
                raise NotFound(f"no session matches '{session.id}'")
            self.db.execute("DELETE FROM session_skills WHERE session_id = ?", (session.id,))
            self.db.executemany(
                "INSERT INTO session_skills (session_id, ref, position) VALUES (?, ?, ?)",
                [(session.id, ref, i) for i, ref in enumerate(session.skills)])
        session.persisted = True
        return session

    def _session(self, row):
        skills = [r["ref"] for r in self.db.execute(
            "SELECT ref FROM session_skills WHERE session_id = ? ORDER BY position", (row["id"],))]
        keys = row.keys()
        return Session(
            id=row["id"], model=row["model"], owner=row["owner"], title=row["title"],
            title_source=row["title_source"], system=row["system"],
            options=json.loads(row["options"]), skills=skills, parent_id=row["parent_id"],
            forked_at_seq=row["forked_at_seq"], created_at=row["created_at"],
            updated_at=row["updated_at"], archived_at=row["archived_at"], persisted=True,
            message_count=row["message_count"] if "message_count" in keys else 0)

    _SELECT = """SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id)
                 AS message_count FROM sessions s"""

    def get(self, ref):
        """Look a session up by id, unique id prefix, or exact title."""
        ref = ref.strip()
        if not ref:
            raise NotFound("no session given")
        rows = self.db.execute(
            self._SELECT + " WHERE s.owner = ? AND substr(s.id, 1, ?) = ?",
            (self.user, len(ref), ref.lower())).fetchall()
        if not rows:
            rows = self.db.execute(
                self._SELECT + " WHERE s.owner = ? AND s.title = ? COLLATE NOCASE",
                (self.user, ref)).fetchall()
        if not rows:
            raise NotFound(f"no session matches '{ref}'")
        if len(rows) > 1:
            ids = ", ".join(r["id"] for r in rows)
            raise Ambiguous(f"'{ref}' matches several sessions: {ids}")
        return self._session(rows[0])

    def latest(self):
        """The session most recently changed, leaving archived ones out."""
        row = self.db.execute(
            self._SELECT + " WHERE s.owner = ? AND s.archived_at IS NULL"
            " ORDER BY s.updated_at DESC, s.rowid DESC LIMIT 1", (self.user,)).fetchone()
        return self._session(row) if row else None

    def list(self, search=None, archived=False):
        """Sessions, most recent first: those not archived, or with archived=True only those
        that are. A search matches titles and message text."""
        sql = self._SELECT + (" WHERE s.owner = ? AND s.archived_at IS NOT NULL" if archived
                              else " WHERE s.owner = ? AND s.archived_at IS NULL")
        params = [self.user]
        if search:
            if self.fts:
                sql += """ AND (s.title LIKE ? OR s.id IN (
                               SELECT m.session_id FROM messages_fts f
                               JOIN messages m ON m.id = f.rowid WHERE messages_fts MATCH ?))"""
                params += [f"%{search}%", _fts_query(search)]
            else:
                sql += """ AND (s.title LIKE ? OR s.id IN (
                               SELECT session_id FROM messages WHERE content LIKE ?))"""
                params += [f"%{search}%", f"%{search}%"]
        sql += " ORDER BY s.updated_at DESC, s.rowid DESC"
        return [self._session(r) for r in self.db.execute(sql, params)]

    def archived_count(self):
        return self.db.execute(
            "SELECT COUNT(*) FROM sessions WHERE owner = ? AND archived_at IS NOT NULL",
            (self.user,)).fetchone()[0]

    def set_archived(self, session_id, archived):
        """Archive a session or bring it back. Its place in the list (updated_at) is kept."""
        with self.db:
            self.db.execute("UPDATE sessions SET archived_at = ? WHERE id = ? AND owner = ?",
                            (_now() if archived else None, session_id, self.user))

    def delete(self, session_id):
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE id = ? AND owner = ?",
                            (session_id, self.user))

    def fork(self, session_id, at_seq=None, title=None):
        """Copy a session, and its messages up to at_seq (all of them by default), into a new one."""
        src = self.get(session_id)
        if at_seq is None:
            at_seq = self.db.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE session_id = ?",
                (src.id,)).fetchone()[0]
        new = self.draft(src.model, title=title or f"{src.title or src.id} (fork)",
                         system=src.system, options=src.options, skills=src.skills)
        if not title:
            new.title_source = src.title_source  # a copy of a first-message title still is one
        new.parent_id, new.forked_at_seq = src.id, at_seq
        self.save(new)
        with self.db:
            self.db.execute(
                """INSERT INTO messages (session_id, seq, role, content, thinking, status, model,
                                         skills, prompt_tokens, eval_tokens, duration_ms, created_at)
                   SELECT ?, seq, role, content, thinking, status, model, skills, prompt_tokens,
                          eval_tokens, duration_ms, created_at
                   FROM messages WHERE session_id = ? AND seq <= ? ORDER BY seq""",
                (new.id, src.id, at_seq))
            self.db.execute(
                """INSERT INTO attachments (message_id, position, path, kind, content, data, note)
                   SELECT copy.id, a.position, a.path, a.kind, a.content, a.data, a.note
                   FROM attachments a
                   JOIN messages orig ON orig.id = a.message_id
                   JOIN messages copy ON copy.session_id = ? AND copy.seq = orig.seq
                   WHERE orig.session_id = ? AND orig.seq <= ?""",
                (new.id, src.id, at_seq))
        return self.get(new.id)

    # -- messages ---------------------------------------------------------

    def add_message(self, session_id, role, content, *, thinking=None, status="complete",
                    model=None, skills=None, prompt_tokens=None, eval_tokens=None,
                    duration_ms=None, attachments=()):
        now = _now()
        with self.db:
            seq = self.db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?",
                (session_id,)).fetchone()[0]
            cur = self.db.execute(
                """INSERT INTO messages (session_id, seq, role, content, thinking, status, model,
                                         skills, prompt_tokens, eval_tokens, duration_ms, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, seq, role, content, thinking, status, model,
                 json.dumps(skills) if skills is not None else None,
                 prompt_tokens, eval_tokens, duration_ms, now))
            message_id = cur.lastrowid
            for i, a in enumerate(attachments):
                a.id = self.db.execute(
                    """INSERT INTO attachments (message_id, position, path, kind, content, data, note)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (message_id, i, a.path, a.kind, a.content, a.data, a.note)).lastrowid
            self.db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        return Message(id=message_id, session_id=session_id, seq=seq, role=role,
                       content=content, thinking=thinking, status=status, model=model,
                       skills=skills, prompt_tokens=prompt_tokens, eval_tokens=eval_tokens,
                       duration_ms=duration_ms, created_at=now, attachments=list(attachments))

    def messages(self, session_id):
        attached = {}
        for a in self.db.execute(
                """SELECT a.* FROM attachments a JOIN messages m ON m.id = a.message_id
                   WHERE m.session_id = ? ORDER BY a.message_id, a.position""", (session_id,)):
            attached.setdefault(a["message_id"], []).append(Attachment(
                path=a["path"], kind=a["kind"], content=a["content"], data=a["data"],
                note=a["note"], id=a["id"]))
        rows = self.db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY seq", (session_id,))
        return [Message(**{**dict(r), "skills": json.loads(r["skills"]) if r["skills"] else None,
                           "attachments": attached.get(r["id"], [])})
                for r in rows]

    def attachment(self, attachment_id):
        """A file attached in one of this user's sessions."""
        row = self.db.execute(
            """SELECT a.* FROM attachments a JOIN messages m ON m.id = a.message_id
               JOIN sessions s ON s.id = m.session_id WHERE a.id = ? AND s.owner = ?""",
            (attachment_id, self.user)).fetchone()
        if row is None:
            raise NotFound(f"no attachment {attachment_id}")
        return Attachment(path=row["path"], kind=row["kind"], content=row["content"],
                          data=row["data"], note=row["note"], id=row["id"])

    def delete_attachment(self, attachment_id):
        """Take one file out of a conversation. The message it came with stays."""
        with self.db:
            self.db.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))

    def delete_message(self, session_id, seq):
        """Drop one message, and the files that came with it; the rest of the session stays."""
        with self.db:
            gone = self.db.execute(
                "DELETE FROM messages WHERE session_id = ? AND seq = ?", (session_id, seq)).rowcount
            if gone:
                self.db.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id))
        if not gone:
            raise NotFound(f"no message {seq} in {session_id}")

    def delete_messages_from(self, session_id, seq):
        """Drop message seq and everything after it."""
        with self.db:
            self.db.execute(
                "DELETE FROM messages WHERE session_id = ? AND seq >= ?", (session_id, seq))
            self.db.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id))
