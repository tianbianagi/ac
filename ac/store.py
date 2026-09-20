"""SQLite persistence for sessions and messages."""

import json
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

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

SCHEMA_VERSION = 1


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
    title: str | None = None
    system: str | None = None
    options: dict = field(default_factory=dict)
    skills: list = field(default_factory=list)
    parent_id: str | None = None
    forked_at_seq: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    message_count: int = 0
    persisted: bool = False


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


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _fts_query(text):
    """Quote every token so user input can't be parsed as FTS5 syntax."""
    return " ".join('"' + tok.replace('"', '""') + '"' for tok in text.split())


class Store:
    def __init__(self, path):
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
        if version >= SCHEMA_VERSION:
            return
        self.db.executescript(SCHEMA)
        try:
            self.db.executescript(FTS_SCHEMA)
        except sqlite3.OperationalError:
            pass  # SQLite built without FTS5: search falls back to LIKE
        self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.db.commit()

    # -- sessions ---------------------------------------------------------

    def draft(self, model, *, title=None, system=None, options=None, skills=()):
        """A new session that is not written until save(), so abandoned launches leave nothing."""
        return Session(id=secrets.token_hex(4), model=model, title=title, system=system,
                       options=dict(options or {}), skills=list(skills))

    def save(self, session):
        now = _now()
        session.created_at = session.created_at or now
        session.updated_at = now
        with self.db:
            self.db.execute(
                """INSERT INTO sessions (id, title, model, system, options, parent_id,
                                         forked_at_seq, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       title = excluded.title, model = excluded.model, system = excluded.system,
                       options = excluded.options, updated_at = excluded.updated_at""",
                (session.id, session.title, session.model, session.system,
                 json.dumps(session.options), session.parent_id, session.forked_at_seq,
                 session.created_at, session.updated_at))
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
            id=row["id"], model=row["model"], title=row["title"], system=row["system"],
            options=json.loads(row["options"]), skills=skills, parent_id=row["parent_id"],
            forked_at_seq=row["forked_at_seq"], created_at=row["created_at"],
            updated_at=row["updated_at"], persisted=True,
            message_count=row["message_count"] if "message_count" in keys else 0)

    _SELECT = """SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id)
                 AS message_count FROM sessions s"""

    def get(self, ref):
        """Look a session up by id, unique id prefix, or exact title."""
        ref = ref.strip()
        if not ref:
            raise NotFound("no session given")
        rows = self.db.execute(
            self._SELECT + " WHERE substr(s.id, 1, ?) = ?", (len(ref), ref.lower())).fetchall()
        if not rows:
            rows = self.db.execute(
                self._SELECT + " WHERE s.title = ? COLLATE NOCASE", (ref,)).fetchall()
        if not rows:
            raise NotFound(f"no session matches '{ref}'")
        if len(rows) > 1:
            ids = ", ".join(r["id"] for r in rows)
            raise Ambiguous(f"'{ref}' matches several sessions: {ids}")
        return self._session(rows[0])

    def latest(self):
        row = self.db.execute(
            self._SELECT + " ORDER BY s.updated_at DESC, s.rowid DESC LIMIT 1").fetchone()
        return self._session(row) if row else None

    def list(self, search=None):
        sql, params = self._SELECT, []
        if search:
            if self.fts:
                sql += """ WHERE s.title LIKE ? OR s.id IN (
                               SELECT m.session_id FROM messages_fts f
                               JOIN messages m ON m.id = f.rowid WHERE messages_fts MATCH ?)"""
                params = [f"%{search}%", _fts_query(search)]
            else:
                sql += """ WHERE s.title LIKE ? OR s.id IN (
                               SELECT session_id FROM messages WHERE content LIKE ?)"""
                params = [f"%{search}%", f"%{search}%"]
        sql += " ORDER BY s.updated_at DESC, s.rowid DESC"
        return [self._session(r) for r in self.db.execute(sql, params)]

    def delete(self, session_id):
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def fork(self, session_id, at_seq=None, title=None):
        """Copy a session, and its messages up to at_seq (all of them by default), into a new one."""
        src = self.get(session_id)
        if at_seq is None:
            at_seq = self.db.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE session_id = ?",
                (src.id,)).fetchone()[0]
        new = self.draft(src.model, title=title or f"{src.title or src.id} (fork)",
                         system=src.system, options=src.options, skills=src.skills)
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
        return self.get(new.id)

    # -- messages ---------------------------------------------------------

    def add_message(self, session_id, role, content, *, thinking=None, status="complete",
                    model=None, skills=None, prompt_tokens=None, eval_tokens=None,
                    duration_ms=None):
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
            self.db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        return Message(id=cur.lastrowid, session_id=session_id, seq=seq, role=role,
                       content=content, thinking=thinking, status=status, model=model,
                       skills=skills, prompt_tokens=prompt_tokens, eval_tokens=eval_tokens,
                       duration_ms=duration_ms, created_at=now)

    def messages(self, session_id):
        rows = self.db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY seq", (session_id,))
        return [Message(**{**dict(r), "skills": json.loads(r["skills"]) if r["skills"] else None})
                for r in rows]

    def delete_messages_from(self, session_id, seq):
        """Drop message seq and everything after it."""
        with self.db:
            self.db.execute(
                "DELETE FROM messages WHERE session_id = ? AND seq >= ?", (session_id, seq))
            self.db.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id))
