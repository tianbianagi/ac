"""Terminal output for the commands, and markdown export."""

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config, files, skills


def use_color(stream):
    return (hasattr(stream, "isatty") and stream.isatty()
            and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb")


class Style:
    DIM, BOLD, CYAN, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[0m"

    def __init__(self, enabled):
        self.enabled = enabled

    def _wrap(self, code, text):
        return f"{code}{text}{self.RESET}" if self.enabled else text

    def dim(self, text):
        return self._wrap(self.DIM, text)

    def bold(self, text):
        return self._wrap(self.BOLD, text)

    def cyan(self, text):
        return self._wrap(self.CYAN, text)


def ago(iso):
    if not iso:
        return ""
    seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    for limit, unit, size in ((60, "s", 1), (3600, "m", 60), (86400, "h", 3600)):
        if seconds < limit:
            return f"{max(int(seconds // size), 0)}{unit} ago"
    days = int(seconds // 86400)
    return f"{days}d ago" if days < 60 else datetime.fromisoformat(iso).strftime("%Y-%m-%d")


def skills_label(refs):
    return ", ".join(skills.label(r) for r in refs) or "none"


def _clip(text, width):
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[:width - 1] + "…"


def _session_title(session):
    """A session is only saved with its first message: until then it is "new", not untitled."""
    return session.title or ("(untitled)" if session.persisted else "(new session)")


def format_sessions(sessions, style):
    if not sessions:
        return "no sessions"
    rows = [("ID", "TITLE", "MODEL", "SKILLS", "MSGS", "UPDATED")]
    for s in sessions:
        rows.append((s.id, _clip(_session_title(s), 44), _clip(s.model, 28),
                     _clip(skills_label(s.skills), 24), str(s.message_count), ago(s.updated_at)))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip() for row in rows]
    return "\n".join([style.dim(lines[0])] + lines[1:])


def format_message(msg, style, thinking=False):
    if msg.role == "user":
        lines = [style.bold(style.cyan(">>> ")) + msg.content]
        lines += [style.dim(f"    attached {line}") for line in files.summarize(msg.attachments)]
        return "\n".join(lines)
    parts = []
    if thinking and msg.thinking:
        parts.append(style.dim(msg.thinking.strip()))
    parts.append(msg.content)
    if msg.status != "complete":
        parts.append(style.dim(f"[{msg.status}]"))
    return "\n".join(p for p in parts if p)


def safe_filename(title, width=80):
    """A title as a filename: nothing a filesystem, a shell or a sync service will choke on."""
    name = re.sub(r'[/\\:*?"<>|\x00-\x1f]+', " ", title or "")
    name = " ".join(name.split()).strip(". ")
    if len(name) > width:
        name = name[:width].rsplit(" ", 1)[0].strip(". ")
    return name


def _is_export_of(path, session):
    """Whether an existing file is an earlier export of this same session."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return session.id in f.read(1000)  # an export states the id near the top
    except OSError:
        return False


def export_path(session):
    """Default file for an export: "<date the session began> <title>.md" in the export folder.

    The session's own date, not today's, so exporting again updates the same file. If another
    session already owns that name, this one's id is added rather than overwriting it.
    """
    day = datetime.fromisoformat(session.created_at).astimezone().date().isoformat()
    folder = config.export_dir() or Path.cwd()
    name = safe_filename(session.title) or f"ac-{session.id}"
    path = folder / f"{day} {name}.md"
    if path.exists() and not _is_export_of(path, session):
        path = folder / f"{day} {name} ({session.id}).md"
    return path


def write_export(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def to_markdown(session, messages, thinking=False):
    lines = [f"# {session.title or session.id}", "",
             f"- session: `{session.id}`", f"- model: `{session.model}`",
             f"- skills: {skills_label(session.skills)}", f"- created: {session.created_at}"]
    if session.system:
        lines += ["", "## System", "", session.system]
    names = config.speaker_names()
    for m in messages:
        lines += ["", f"## {names.get(m.role, m.role.capitalize())}" + ("" if m.status == "complete"
                                                     else f" ({m.status})"), ""]
        if thinking and m.thinking:
            lines += ["<details><summary>thinking</summary>", "", m.thinking.strip(), "",
                      "</details>", ""]
        lines.append(m.content)
        # An export is the conversation. Attached files are named, never reproduced: they can
        # be large, and they are the user's documents rather than something that was said.
        if m.attachments:
            lines.append("")
            lines += [f"*attached: {line}*  " for line in files.summarize(m.attachments)]
    return "\n".join(lines) + "\n"

