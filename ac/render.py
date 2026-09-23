"""Terminal output: styling, streamed replies, session tables, transcripts."""

import json
import os
import re
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

from . import config, files, skills
from .markdown import MarkdownStream, _width
from .markdown import render as render_markdown
from .picker import _clip as _clip_cells


def use_color(stream):
    return (hasattr(stream, "isatty") and stream.isatty()
            and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb")


class Style:
    DIM, BOLD, RED, YELLOW, CYAN, RESET = "\033[2m", "\033[1m", "\033[31m", "\033[33m", "\033[36m", "\033[0m"
    GREEN, ORANGE = "\033[32m", "\033[38;5;208m"  # orange is a 256-colour shade

    def __init__(self, enabled):
        self.enabled = enabled

    def code(self, code):
        return code if self.enabled else ""

    def _wrap(self, code, text):
        return f"{code}{text}{self.RESET}" if self.enabled else text

    def dim(self, text):
        return self._wrap(self.DIM, text)

    def bold(self, text):
        return self._wrap(self.BOLD, text)

    def red(self, text):
        return self._wrap(self.RED, text)

    def yellow(self, text):
        return self._wrap(self.YELLOW, text)

    def cyan(self, text):
        return self._wrap(self.CYAN, text)

    def green(self, text):
        return self._wrap(self.GREEN, text)

    def orange(self, text):
        return self._wrap(self.ORANGE, text)


class StreamRenderer:
    """Prints one streamed reply: dimmed thinking first, then the content.

    thinking: "show" prints the reasoning, "hide" prints only a marker, "silent" prints nothing.
    markdown: render the content for a terminal. Only ever on when styling is, so that piped
    and redirected output stays exactly what the model wrote.
    """

    def __init__(self, out, style, thinking="show", markdown=False):
        self.out, self.style, self.thinking = out, style, thinking
        self.markdown = MarkdownStream(out) if markdown and style.enabled else None
        self.state = None
        self.last = "\n"

    def _write(self, text):
        if text:
            self.out.write(text)
            self.last = text[-1]

    def feed(self, kind, text):
        if kind == "thinking":
            if self.thinking == "silent":
                return
            if self.state is None:
                self.state = "thinking"
                self._write(self.style.code(Style.DIM) + "thinking…\n")
            if self.thinking == "show":
                self._write(text)
        else:
            if self.state != "content":
                if self.state == "thinking":
                    self._end_thinking()
                    self._write("\n")
                self.state = "content"
                text = text.lstrip()
            if self.markdown:
                self.markdown.feed(text)
            else:
                self._write(text)
        self.out.flush()

    def _end_thinking(self):
        if self.last != "\n":
            self._write("\n")
        self.out.write(self.style.code(Style.RESET))

    def finish(self):
        if self.state == "thinking":
            self._end_thinking()
        elif self.state == "content" and self.markdown:
            self.markdown.finish()
        elif self.state == "content" and self.last != "\n":
            self._write("\n")
        self.out.flush()


def fmt_tokens(n):
    if n is None:
        return "?"
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f}k"
    return f"{round(n / 1000)}k"


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


def status_line(model, used, context, stats, skill_names):
    parts = [model]
    if used is not None:
        parts.append(f"{fmt_tokens(used)}/{fmt_tokens(context)} ctx" if context
                     else f"{fmt_tokens(used)} ctx")
    eval_ns, eval_count = stats.get("eval_duration"), stats.get("eval_count")
    if eval_ns and eval_count:
        parts.append(f"{eval_count / (eval_ns / 1e9):.0f} tok/s")
    parts.append(f"skills: {', '.join(skill_names) or 'none'}")
    return " · ".join(parts)


# What the status bar shows. session: the Session in use. skills: the labels of the attached
# skills. queued: files waiting for the next message. usage: (tokens used, context length),
# or None before the first reply. speed: tok/s of the latest reply. note: what a reply under
# way is doing, shown next to the numbers in place of the speed.
Status = namedtuple("Status", "session model skills queued usage speed note",
                    defaults=((), 0, None, None, None))

METER = 8  # cells in the context meter


def usage_level(usage):
    """None, "warn" at 80% of the context window and "alert" at 95%: the /compact thresholds."""
    used, context = usage or (None, None)
    if not used or not context:
        return None
    return "alert" if used >= 0.95 * context else "warn" if used >= 0.8 * context else None


def _status_left(status, cut):
    """Where you are, as (text, colour) segments: session, model, skills, queued files. cut
    trims it, one step at a time."""
    s = status.session
    title = " ".join((s.title or "").split()) if s.persisted else "new session"
    parts = [(title, "orange")] * (cut < 2 and bool(title)) + [(status.model, "cyan")]
    if status.skills:
        names = ", ".join(status.skills)
        parts.append((names if cut < 3 else f"{len(status.skills)} skill{'s' * (len(status.skills) != 1)}",
                      "green"))
    else:
        parts.append(("no skills", "dim"))
    if s.system:
        parts[-1] = (parts[-1][0] + " +sys", "green")
    if status.queued:
        parts.append((f"{status.queued} file{'s' * (status.queued != 1)} queued", "yellow"))
    return parts


def _status_right(status, cut):
    """The numbers, as segments: context used, with a meter, and the speed of the last reply."""
    used, context = status.usage or (None, None)
    colour = {"warn": "yellow", "alert": "red"}.get(usage_level(status.usage))
    parts = []
    if used is not None and context:
        share = min(used / context, 1)
        text = f"{fmt_tokens(used)}/{fmt_tokens(context)}"
        filled = max(round(share * METER), 1)       # anything used shows: never an empty meter
        text += " " + "▮" * filled + "▯" * (METER - filled)
        parts.append((f"{text} {share:.0%}", colour))
        if colour == "red":
            parts.append(("nearly full · /compact", colour))
    elif used is not None:
        parts.append((f"{fmt_tokens(used)} ctx", None))
    if status.note:                 # a reply under way: what it is doing, not the last one's speed
        parts.append((status.note, "dim"))
    elif status.speed and cut < 1:
        parts.append((f"{status.speed:.0f} tok/s", "dim"))
    return parts


def _segments_text(segments):
    return " · ".join(text for text, _ in segments)


def _segments_paint(segments, style):
    return style.dim(" · ").join(getattr(style, colour)(text) if colour else text
                                 for text, colour in segments)


def _segments_clip(segments, width):
    """The segments that fit in width cells, the last one cut short if need be."""
    kept = []
    for text, colour in segments:
        room = width - _width(_segments_text(kept)) - (3 if kept else 0)
        if room <= 0:
            break
        kept.append((_clip_cells(text, room), colour))
        if _width(text) > room:
            break
    return kept


def status_bar(status, width, style):
    """One line of exactly `width` cells for the bottom of the terminal: where you are on the
    left, the numbers on the right, each part in its own colour. When there isn't room for
    everything, the least useful parts go first: the speed, the session's title, then the
    skill names. The context meter always stays."""
    for cut in range(4):
        left, right = _status_left(status, cut), _status_right(status, cut)
        between = 2 if right else 0
        gap = width - 2 - _width(_segments_text(left)) - _width(_segments_text(right)) - between
        if gap >= 0:
            break
    else:
        left = _segments_clip(left, max(width - 2 - _width(_segments_text(right)) - between, 0))
        if not left:                    # not even room for that: the numbers alone, then
            right, between = _segments_clip(right, width - 2), 0
        gap = max(width - 2 - _width(_segments_text(left)) - _width(_segments_text(right))
                  - between, 0)
    pad = " " * (gap + between)
    return f" {_segments_paint(left, style)}{pad}{_segments_paint(right, style)} "


def _clip(text, width):
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[:width - 1] + "…"


def _session_title(session):
    """A session is only saved with its first message: until then it is "new", not untitled."""
    return session.title or ("(untitled)" if session.persisted else "(new session)")


def session_label(session, current_id=None):
    """(title, details) for one row of the session picker."""
    here = " · current" if session.id == current_id else ""
    return (" ".join(_session_title(session).split()),
            f"{session.id} · {ago(session.updated_at) or 'new'} · {session.message_count} msgs{here}")


def model_label(model, current=None):
    """(name, details) for one row of the model picker."""
    details = model.get("details") or {}
    parts = [f"{model.get('size', 0) / 1e9:.1f} GB", details.get("parameter_size"),
             details.get("quantization_level"), "current" if model["name"] == current else None]
    return model["name"], " · ".join(p for p in parts if p)


def format_sessions(sessions, style, current_id=None, numbered=False):
    if not sessions:
        return "no sessions"
    rows = [("", "ID", "TITLE", "MODEL", "SKILLS", "MSGS", "UPDATED")]
    for s in sessions:
        rows.append(("*" if s.id == current_id else "", s.id, _clip(_session_title(s), 44),
                     _clip(s.model, 28), _clip(skills_label(s.skills), 24),
                     str(s.message_count), ago(s.updated_at)))
    if numbered:
        rows = [("#",) + rows[0]] + [(str(n),) + row for n, row in enumerate(rows[1:], 1)]
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip() for row in rows]
    return "\n".join([style.dim(lines[0])] + lines[1:])


def format_message(msg, style, thinking=False, markdown=False):
    if msg.role == "user":
        lines = [style.bold(style.cyan(">>> ")) + msg.content]
        lines += [style.dim(f"    attached {line}") for line in files.summarize(msg.attachments)]
        return "\n".join(lines)
    parts = []
    if thinking and msg.thinking:
        parts.append(style.dim(msg.thinking.strip()))
    parts.append(render_markdown(msg.content) if markdown and style.enabled else msg.content)
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
            return session.id in f.read(1000)  # both formats state the id near the top
    except OSError:
        return False


def export_path(session, fmt):
    """Default file for an export: "<date the session began> <title>.<fmt>" in the export folder.

    The session's own date, not today's, so exporting again updates the same file. If another
    session already owns that name, this one's id is added rather than overwriting it.
    """
    day = datetime.fromisoformat(session.created_at).astimezone().date().isoformat()
    folder = config.export_dir() or Path.cwd()
    name = safe_filename(session.title) or f"ac-{session.id}"
    path = folder / f"{day} {name}.{fmt}"
    if path.exists() and not _is_export_of(path, session):
        path = folder / f"{day} {name} ({session.id}).{fmt}"
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


def to_json(session, messages):
    keep = ("seq", "role", "content", "thinking", "status", "model", "skills", "prompt_tokens",
            "eval_tokens", "duration_ms", "created_at")
    return json.dumps({
        "id": session.id, "title": session.title, "model": session.model,
        "system": session.system, "options": session.options, "skills": session.skills,
        "parent_id": session.parent_id, "forked_at_seq": session.forked_at_seq,
        "created_at": session.created_at, "updated_at": session.updated_at,
        "names": config.speaker_names(),  # roles below stay "user"/"assistant" for programs
        "messages": [{**{k: getattr(m, k) for k in keep},
                      "attachments": [{"path": a.path, "kind": a.kind, "bytes": a.size,
                                       "note": a.note} for a in m.attachments]}
                     for m in messages],
    }, indent=2, ensure_ascii=False) + "\n"
