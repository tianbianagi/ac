"""The interactive chat loop and its slash commands."""

import atexit
import base64
import difflib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from . import config, files, render, skills
from .ollama import OllamaError, resolve_model
from .store import StoreError

# Session options that are ours; every other key is passed to Ollama as a model option.
RESERVED_OPTIONS = ("think", "show_thinking", "keep_alive")
KNOWN_OPTIONS = ("temperature", "top_p", "top_k", "min_p", "num_ctx", "num_predict", "seed",
                 "repeat_penalty", "presence_penalty", "keep_alive")

COMPACT_PROMPT = (
    "Summarize our conversation so far so that it can be continued from the summary alone. "
    "Keep every fact, decision, name, number, piece of code and open question that later turns "
    "might depend on. Write it as a briefing, not as a transcript, and add nothing new.")
SUMMARY_HEADER = "[Summary of our earlier conversation]"

HELP = """\
Sessions
  /new                    start a fresh session (same model, no skills)
  /sessions [QUERY]       list sessions, optionally searching titles and messages
  /switch ID              switch to another session (id, id prefix, or title)
  /rename TITLE           rename this session
  /fork [SEQ]             branch this session (up to message SEQ) and switch to the copy
  /delete [ID]            delete this (or another) session
  /export [md|json] [FILE]  write the transcript (default: ./ac-ID.md in the current folder)
Skills and prompt
  /skills                 list available skills (* = attached)
  /skill add NAME|PATH    attach a skill to this session
  /skill rm NAME          detach a skill
  /system [TEXT|clear]    show, set or clear this session's own system text
  /context                show token usage and the exact system prompt being sent
  /files                  list the files attached in this conversation
Model
  /model [NAME|NUMBER]    list models, or switch this session to another one
  /set KEY [VALUE]        set a model option (temperature, num_ctx, ...); no VALUE unsets
  /think on|off|default|show|hide
Conversation
  /retry                  regenerate the last reply
  /edit                   edit your last message in $EDITOR and resend
  /undo                   drop the last exchange
  /compact                continue in a new session seeded with a summary of this one
  /help  /quit
Files: name a path in your message (/abs, ~/home, ./relative, or @name for a bare filename) and
its contents are sent along: text files, PDFs (report.pdf#10-20 picks pages), images (for
models with vision) and directory listings.
Input: wrap multi-line text in \"\"\" ... \"\"\". Start a message with // to send a leading /.
Ctrl-C stops a reply (the partial text is kept); Ctrl-D quits."""


def make_title(text, width=60):
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


def build_messages(system, messages, vision=True):
    """The payload for Ollama: usable turns only, never thinking, same-role neighbours merged.

    Attached files go in front of the text of the message they came with; images are left out
    for a model that can't see them.
    """
    payload = [{"role": "system", "content": system}] if system else []
    for m in messages:
        if m.status == "error" or not m.content.strip():
            continue
        shown = [a for a in m.attachments if vision or a.kind != "image"]
        content = "\n\n".join([files.for_model(a) for a in shown] + [m.content])
        images = [base64.b64encode(a.data).decode() for a in shown if a.kind == "image"]
        if payload and payload[-1]["role"] == m.role and m.role != "system":
            payload[-1]["content"] += "\n\n" + content
        else:
            payload.append({"role": m.role, "content": content})
        if images:
            payload[-1].setdefault("images", []).extend(images)
    return payload


def edit_text(text):
    """Round-trip text through the user's editor."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(text)
    try:
        subprocess.call(shlex.split(editor) + [f.name])
        return Path(f.name).read_text(encoding="utf-8")
    finally:
        os.unlink(f.name)


class Repl:
    def __init__(self, store, client, session, *, out=None, log=None, input_fn=input,
                 style=None, editor=edit_text, quiet=False):
        self.store, self.client, self.session = store, client, session
        self.out = out or sys.stdout
        self.log = log or self.out  # notes, warnings and errors; stderr for one-shot use
        self.input_fn, self.editor, self.quiet = input_fn, editor, quiet
        self.style = style or render.Style(render.use_color(self.log))
        self.last_usage = None  # (tokens used, context length) after the latest reply

    # -- output -----------------------------------------------------------

    def say(self, text=""):
        print(text, file=self.out)

    def note(self, text):
        print(self.style.dim(text), file=self.log)

    def warn(self, text):
        print(self.style.yellow(text), file=self.log)

    def error(self, text):
        print(self.style.red(f"error: {text}"), file=self.log)

    def banner(self):
        s = self.session
        state = f"{self.store.get(s.id).message_count} messages" if s.persisted else "new"
        title = f" · {s.title}" if s.title else ""
        self.note(f"session {s.id} ({state}){title} · {s.model} · "
                  f"skills: {render.skills_label(s.skills)} · /help for commands")

    def show_tail(self, count=4):
        messages = self.store.messages(self.session.id)
        if len(messages) > count:
            self.note(f"… {len(messages) - count} earlier messages")
        for m in messages[-count:]:
            self.say(render.format_message(m, self.style))
            self.say()

    # -- input ------------------------------------------------------------

    def read(self):
        line = self.input_fn(">>> ")
        if not line.lstrip().startswith('"""'):
            return line.strip()
        first = line.lstrip()[3:]
        if first.rstrip().endswith('"""'):
            return first.rstrip()[:-3].strip()
        lines = [first]
        while True:
            line = self.input_fn("... ")
            if line.rstrip().endswith('"""'):
                lines.append(line.rstrip()[:-3])
                return "\n".join(lines).strip()
            lines.append(line)

    def run(self):
        self.banner()
        if self.session.persisted:
            self.show_tail()
        while True:
            try:
                line = self.read()
            except EOFError:
                self.say()
                return
            except KeyboardInterrupt:
                self.say()
                continue
            if line and self.handle(line) is False:
                return

    def handle(self, line):
        """Process one input. Returns False when the REPL should exit."""
        if line.startswith("//"):
            line = line[1:]
        elif line.startswith("/") and "/" not in line[1:].split(" ", 1)[0]:
            name, _, arg = line[1:].partition(" ")
            command = getattr(self, f"cmd_{name.lower()}", None)
            if command is None:
                close = difflib.get_close_matches(name.lower(), self.command_names(), n=1)
                hint = f" Did you mean /{close[0]}?" if close else ""
                self.error(f"unknown command /{name}.{hint} /help lists commands.")
                return True
            try:
                return command(arg.strip()) is not False
            except (StoreError, skills.SkillError, OllamaError) as e:
                self.error(str(e))
                return True
        self.send(line)
        return True

    @classmethod
    def command_names(cls):
        return sorted(n[4:] for n in dir(cls) if n.startswith("cmd_"))

    # -- chatting ---------------------------------------------------------

    def send(self, text):
        s = self.session
        if not s.persisted:
            s.title = s.title or make_title(text)
            self.store.save(s)
        attachments, problems = files.collect(text)
        for problem in problems:
            self.warn(problem)
        for attachment in attachments:
            self.note(f"attached {files.describe(attachment)}")
        self.store.add_message(s.id, "user", text, attachments=attachments)
        return self.generate()

    def _stream(self, payload):
        """Stream one reply to the terminal. Returns (content, thinking, stats, status, error)."""
        s = self.session
        if self.quiet:
            mode = "silent"
        else:
            mode = "show" if s.options.get("show_thinking", True) else "hide"
        renderer = render.StreamRenderer(self.out, self.style, mode)
        options = {k: v for k, v in s.options.items() if k not in RESERVED_OPTIONS}
        content, thinking, stats, status, error = [], [], {}, "complete", None
        stream = self.client.chat(s.model, payload, think=s.options.get("think"),
                                  options=options, keep_alive=s.options.get("keep_alive"))
        try:
            for kind, data in stream:
                if kind == "done":
                    stats = data
                else:
                    (thinking if kind == "thinking" else content).append(data)
                    renderer.feed(kind, data)
        except KeyboardInterrupt:
            status = "interrupted"
        except OllamaError as e:
            status, error = "error", e
        finally:
            stream.close()
            renderer.finish()
        return "".join(content), "".join(thinking), stats, status, error

    def generate(self):
        """Ask the model to reply to the conversation as stored; save and return the reply."""
        s = self.session
        system, active, missing = skills.compose_system(s.system, s.skills)
        for ref in missing:
            self.warn(f"skill '{skills.label(ref)}' can't be loaded; continuing without it")
        payload = build_messages(system, *self._visible(self.store.messages(s.id)))
        content, thinking, stats, status, error = self._stream(payload)

        message = None
        if content or thinking:
            total_ns = stats.get("total_duration")
            message = self.store.add_message(
                s.id, "assistant", content, thinking=thinking or None, status=status,
                model=s.model, skills=[{"name": k.name, "sha": k.sha} for k in active],
                prompt_tokens=stats.get("prompt_eval_count"), eval_tokens=stats.get("eval_count"),
                duration_ms=total_ns // 1_000_000 if total_ns else None)
        if status == "interrupted":
            self.note("interrupted — partial reply kept. /retry regenerates, /undo drops it.")
        elif status == "error":
            self.report(error)
        elif not self.quiet:
            self._after_reply(stats, active)
        return message

    def _visible(self, messages):
        """(messages, vision) for build_messages, warning when images have to be left out."""
        images = sum(a.kind == "image" for m in messages for a in m.attachments)
        vision = not images or self.client.supports(self.session.model, "vision")
        if not vision:
            self.warn(f"{self.session.model} can't see images; leaving {images} out")
        return messages, vision

    def report(self, error):
        self.error(str(error))
        if error.status == 404:
            try:
                names = ", ".join(m["name"] for m in self.client.list_models())
                self.note(f"installed models: {names or 'none'} — change with /model NAME")
            except OllamaError:
                pass
        else:
            self.note("your message is saved; /retry sends it again.")

    def _after_reply(self, stats, active):
        used = None
        if stats.get("prompt_eval_count") is not None:
            used = stats["prompt_eval_count"] + stats.get("eval_count", 0)
        context = self.client.context_length(self.session.model)
        self.last_usage = (used, context)
        self.note(render.status_line(self.session.model, used, context, stats,
                                     [k.name for k in active]))
        if used and context:
            if used >= 0.95 * context:
                self.say(self.style.red(
                    "context is nearly full: Ollama will silently drop the oldest messages. "
                    "/compact continues from a summary."))
            elif used >= 0.8 * context:
                self.warn("context is over 80% full; consider /compact.")

    # -- helpers ----------------------------------------------------------

    def _changed(self):
        if self.session.persisted:
            self.store.save(self.session)

    def _require_saved(self, what):
        if not self.session.persisted:
            self.note(f"nothing to {what}: this session has no messages yet.")
            return False
        return True

    def _switch(self, session):
        self.session = session
        self.last_usage = None
        self.banner()

    # -- commands: sessions -----------------------------------------------

    def cmd_help(self, arg):
        self.say(HELP)

    def cmd_quit(self, arg):
        return False

    cmd_exit = cmd_q = cmd_quit

    def cmd_new(self, arg):
        self._switch(self.store.draft(self.session.model))

    def cmd_sessions(self, arg):
        self.say(render.format_sessions(self.store.list(search=arg or None), self.style,
                                        self.session.id))

    cmd_ls = cmd_sessions

    def cmd_switch(self, arg):
        if not arg:
            return self.error("usage: /switch ID")
        self._switch(self.store.get(arg))
        self.show_tail()

    def cmd_rename(self, arg):
        if not arg:
            return self.error("usage: /rename TITLE")
        self.session.title = arg
        self._changed()
        self.note(f"renamed to '{arg}'")

    def cmd_fork(self, arg):
        if not self._require_saved("fork"):
            return
        if arg and not arg.isdigit():
            return self.error("usage: /fork [SEQ]   (SEQ is a message number)")
        self._switch(self.store.fork(self.session.id, at_seq=int(arg) if arg else None))

    def cmd_delete(self, arg):
        if not arg and not self.session.persisted:
            return self.note("this session was never saved; nothing to delete.")
        target = self.store.get(arg or self.session.id)
        answer = self.input_fn(f"delete '{target.title or target.id}' "
                               f"({target.message_count} messages)? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            return self.note("kept.")
        self.store.delete(target.id)
        self.note(f"deleted {target.id}")
        if target.id == self.session.id:
            self._switch(self.store.draft(self.session.model))

    cmd_rm = cmd_delete

    def cmd_export(self, arg):
        if not self._require_saved("export"):
            return
        parts = shlex.split(arg)
        fmt = parts.pop(0) if parts and parts[0] in ("md", "json") else "md"
        path = Path(parts[0]).expanduser() if parts else Path(f"ac-{self.session.id}.{fmt}")
        session = self.store.get(self.session.id)
        messages = self.store.messages(session.id)
        text = (render.to_json(session, messages) if fmt == "json"
                else render.to_markdown(session, messages))
        path.write_text(text, encoding="utf-8")
        self.note(f"wrote {files.display_path(path)}")

    # -- commands: skills and prompt --------------------------------------

    def cmd_skills(self, arg):
        found = skills.discover()
        if not found:
            where = config.skills_dirs()[-1]
            return self.note(f"no skills found. Create {where}/<name>/SKILL.md")
        attached = {skills.label(r) for r in self.session.skills}
        for name, skill in sorted(found.items()):
            mark = "*" if name in attached else " "
            self.say(f"{mark} {name:<20} {self.style.dim(skill.description)}")

    def cmd_skill(self, arg):
        action, _, names = arg.partition(" ")
        refs = shlex.split(names)
        if action == "" or action == "list":
            return self.note(f"attached: {render.skills_label(self.session.skills)}")
        if action not in ("add", "rm", "remove") or not refs:
            return self.error("usage: /skill add NAME|PATH ...  |  /skill rm NAME ...")
        for ref in refs:
            if action == "add":
                stored = skills.normalize_ref(ref)
                if stored in self.session.skills:
                    self.note(f"'{skills.label(stored)}' is already attached")
                    continue
                self.session.skills.append(stored)
            else:
                matches = [r for r in self.session.skills if ref in (r, skills.label(r))]
                if not matches:
                    self.error(f"'{ref}' is not attached")
                    continue
                self.session.skills.remove(matches[0])
        self._changed()
        self.note(f"attached: {render.skills_label(self.session.skills)}")

    def cmd_system(self, arg):
        if not arg:
            return self.say(self.session.system or self.style.dim("(no system text)"))
        self.session.system = None if arg == "clear" else arg
        self._changed()
        self.note("system text cleared" if arg == "clear" else "system text set")

    def cmd_files(self, arg):
        attached = [(m, a) for m in self.store.messages(self.session.id) for a in m.attachments]
        if not attached:
            return self.note("no files in this conversation. Name a path in a message "
                             "(/path, ~/path, ./path or @name) and it is read and sent along.")
        for m, a in attached:
            self.say(f"#{m.seq}  {files.describe(a)}")
        self.note("these are snapshots from when each message was sent; name a path again to "
                  "send its current contents")

    def cmd_context(self, arg):
        s = self.session
        system, active, missing = skills.compose_system(s.system, s.skills)
        self.say(f"model    {s.model}")
        self.say(f"options  {json.dumps(s.options) if s.options else '(model defaults)'}")
        self.say(f"skills   {', '.join(k.name for k in active) or 'none'}"
                 + (f"   missing: {', '.join(map(skills.label, missing))}" if missing else ""))
        if self.last_usage and self.last_usage[0] is not None:
            used, context = self.last_usage
            self.say(f"usage    {render.fmt_tokens(used)} of {render.fmt_tokens(context)} tokens")
        else:
            self.say("usage    (known after the next reply)")
        if system:
            self.say(f"system   {len(system)} chars, roughly {len(system) // 4} tokens:")
            self.say(self.style.dim(system))
        else:
            self.say("system   (none — no system message is sent)")

    # -- commands: model --------------------------------------------------

    def cmd_model(self, arg):
        models = self.client.list_models()
        if not arg:
            width = max((len(m["name"]) for m in models), default=0)
            for i, m in enumerate(models, 1):
                mark = "*" if m["name"] == self.session.model else " "
                self.say(f"{mark} {i}  {m['name']:<{width}}  {m.get('size', 0) / 1e9:>6.1f} GB")
            return self.note("switch with /model NAME or /model NUMBER; the conversation carries over")
        if arg.isdigit():
            if not 1 <= int(arg) <= len(models):
                return self.error(f"no model number {arg}; /model lists them")
            name = models[int(arg) - 1]["name"]
        else:
            name = resolve_model(self.client, arg)
        if name == self.session.model:
            return self.note(f"already using {name}")
        self.session.model = name
        self.last_usage = None
        self._changed()
        self.note(f"model set to {name}")
        try:
            loaded = any(name in (m.get("name"), m.get("model")) for m in self.client.ps())
        except OllamaError:
            return
        if not loaded:
            size = next((m.get("size", 0) for m in models if m["name"] == name), 0)
            self.note(f"it isn't loaded yet: the next reply waits while Ollama loads "
                      f"{size / 1e9:.1f} GB")

    def cmd_set(self, arg):
        key, _, raw = arg.partition(" ")
        raw = raw.strip()
        if not key:
            self.say(json.dumps(self.session.options, indent=2) if self.session.options
                     else self.style.dim("(model defaults)"))
            return self.note(f"keys: {', '.join(KNOWN_OPTIONS)}")
        if raw in ("", "default", "unset"):
            self.session.options.pop(key, None)
            self.note(f"{key} unset")
        else:
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw
            self.session.options[key] = value
            self.note(f"{key} = {json.dumps(value)}")
        self._changed()

    def cmd_think(self, arg):
        changes = {"on": ("think", True), "off": ("think", False), "default": ("think", None),
                   "show": ("show_thinking", True), "hide": ("show_thinking", False)}
        if arg not in changes:
            return self.error("usage: /think on|off|default|show|hide")
        key, value = changes[arg]
        if value is None:
            self.session.options.pop(key, None)
        else:
            self.session.options[key] = value
        self._changed()
        self.note(f"thinking: {arg}")

    # -- commands: conversation -------------------------------------------

    def cmd_retry(self, arg):
        if not self._require_saved("retry"):
            return
        messages = self.store.messages(self.session.id)
        if messages and messages[-1].role == "assistant":
            self.store.delete_messages_from(self.session.id, messages[-1].seq)
            messages.pop()
        if not messages or messages[-1].role != "user":
            return self.note("nothing to retry.")
        self.generate()

    def _last_user(self):
        users = [m for m in self.store.messages(self.session.id) if m.role == "user"]
        return users[-1] if users else None

    def cmd_undo(self, arg):
        last = self._last_user() if self.session.persisted else None
        if last is None:
            return self.note("nothing to undo.")
        self.store.delete_messages_from(self.session.id, last.seq)
        self.note(f"dropped: {make_title(last.content)}")

    def cmd_edit(self, arg):
        last = self._last_user() if self.session.persisted else None
        if last is None:
            return self.note("nothing to edit.")
        text = self.editor(last.content).strip()
        if not text or text == last.content.strip():
            return self.note("unchanged; nothing sent.")
        self.store.delete_messages_from(self.session.id, last.seq)
        self.say(self.style.bold(self.style.cyan(">>> ")) + text)
        self.send(text)  # named files are read again, as they are now

    def cmd_compact(self, arg):
        if not self._require_saved("compact"):
            return
        s = self.session
        messages = self.store.messages(s.id)
        system, _, _ = skills.compose_system(s.system, s.skills)
        payload = build_messages(system, *self._visible(messages))
        payload.append({"role": "user", "content": COMPACT_PROMPT})
        self.note("summarizing…")
        summary, _, _, status, error = self._stream(payload)
        if status != "complete" or not summary.strip():
            if error:
                self.report(error)
            return self.note("compaction abandoned; this session is unchanged.")
        new = self.store.draft(s.model, title=f"{s.title or s.id} (compacted)", system=s.system,
                               options=s.options, skills=s.skills)
        new.parent_id, new.forked_at_seq = s.id, messages[-1].seq if messages else 0
        self.store.save(new)
        self.store.add_message(new.id, "user", f"{SUMMARY_HEADER}\n\n{summary.strip()}")
        self.note(f"original kept as {s.id}.")
        self._switch(self.store.get(new.id))

    # -- completion -------------------------------------------------------

    def completions(self, buffer):
        """Candidates for the word being typed, given the whole input line so far."""
        words = buffer.split(" ")
        last = words[-1]
        is_command = buffer.startswith("/") and "/" not in words[0][1:]
        if is_command and len(words) == 1:
            commands = [f"/{n}" for n in self.command_names() if f"/{n}".startswith(last)]
            if commands:
                return commands
        if last.startswith(("/", "~", "./", "../", "@")):
            return files.complete(last)  # a path, in a message or as a command's argument
        if not is_command or len(words) == 1:
            return []
        command, prefix = words[0][1:], words[-1]
        try:
            if command == "skill" and len(words) == 2:
                options = ["add", "rm"]
            elif command == "skill" and words[1] == "add":
                options = list(skills.discover())
            elif command == "skill":
                options = [skills.label(r) for r in self.session.skills]
            elif command in ("switch", "delete", "rm") and len(words) == 2:
                options = [x.id for x in self.store.list()]
            elif command == "model" and len(words) == 2:
                options = [m["name"] for m in self.client.list_models()]
            elif command == "set" and len(words) == 2:
                options = list(KNOWN_OPTIONS)
            elif command == "think" and len(words) == 2:
                options = ["on", "off", "default", "show", "hide"]
            else:
                options = []
        except (OllamaError, StoreError):
            options = []
        return [o for o in options if o.startswith(prefix)]


def setup_readline(repl):
    """Line editing, persistent input history and tab completion for an interactive REPL."""
    try:
        import readline
    except ImportError:
        return
    history = config.history_path()
    history.parent.mkdir(parents=True, exist_ok=True)
    try:
        readline.read_history_file(history)
    except OSError:
        pass
    readline.set_history_length(2000)

    def save_history():
        try:
            readline.write_history_file(history)
        except OSError:
            pass

    atexit.register(save_history)

    def complete(text, state):
        matches = repl.completions(readline.get_line_buffer())
        return matches[state] if state < len(matches) else None

    readline.set_completer_delims(" \t\n")
    readline.set_completer(complete)
    libedit = getattr(readline, "backend", "") == "editline" or "libedit" in (readline.__doc__ or "")
    readline.parse_and_bind("bind ^I rl_complete" if libedit else "tab: complete")
