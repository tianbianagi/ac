"""Command-line entry point."""

import argparse
import json
import sys
from pathlib import Path

from . import __version__, config, files, render, skills
from .ollama import Client, OllamaError, resolve_model
from .repl import Repl, setup_readline
from .store import Store, StoreError


_open_stores = []


def open_store(path=None):
    """Open the session database; main() closes whatever a command opened."""
    store = Store(path or config.db_path())
    _open_stores.append(store)
    return store


def pick_model(client, requested=None):
    """Model for a new session: -m, then $AC_MODEL, then the built-in default."""
    name = requested or config.model_override()
    if name:
        return resolve_model(client, name)
    names = [m["name"] for m in client.list_models()]
    if config.DEFAULT_MODEL in names:
        return config.DEFAULT_MODEL
    if not names:
        raise OllamaError(f"no models installed. Try `ollama pull {config.DEFAULT_MODEL}`.")
    return names[0]  # the default isn't installed here; don't refuse to start over it


def _interactive(store, client, session):
    repl = Repl(store, client, session)
    if sys.stdin.isatty():
        setup_readline(repl)
    repl.run()


def cmd_new(args):
    store, client = open_store(), Client()
    session = store.draft(pick_model(client, args.model), title=args.title,
                          system=args.system,
                          skills=[skills.normalize_ref(s) for s in args.skill])
    _interactive(store, client, session)


def cmd_resume(args):
    store = open_store()
    session = store.get(args.id) if args.id else store.latest()
    if session is None:
        raise StoreError(f"no sessions yet. Start one with `{config.COMMAND}`.")
    _interactive(store, Client(), session)


def cmd_ls(args):
    store = open_store()
    print(render.format_sessions(store.list(search=args.search),
                                 render.Style(render.use_color(sys.stdout))))


def cmd_show(args):
    store = open_store()
    session = store.get(args.id)
    style = render.Style(render.use_color(sys.stdout))
    print(style.dim(f"{session.id} · {session.title or '(untitled)'} · {session.model} · "
                    f"skills: {render.skills_label(session.skills)}"))
    for m in store.messages(session.id):
        print()
        print(render.format_message(m, style, thinking=args.thinking))


def cmd_rename(args):
    store = open_store()
    session = store.get(args.id)
    session.title = " ".join(args.title)
    store.save(session)
    print(f"{session.id} renamed to '{session.title}'")


def _parse_value(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def cmd_set(args):
    store = open_store()
    session = store.get(args.id)
    if args.model:
        session.model = resolve_model(Client(), args.model)
    if args.clear_system:
        session.system = None
    elif args.system is not None:
        session.system = args.system
    if args.think:
        session.options.pop("think", None)
        if args.think != "default":
            session.options["think"] = args.think == "on"
    for item in args.option:
        key, sep, raw = item.partition("=")
        if not sep:
            raise StoreError(f"--option expects KEY=VALUE, got '{item}'")
        if raw in ("", "default"):
            session.options.pop(key, None)
        else:
            session.options[key] = _parse_value(raw)
    for ref in args.add_skill:
        stored = skills.normalize_ref(ref)
        if stored not in session.skills:
            session.skills.append(stored)
    for ref in args.rm_skill:
        session.skills = [r for r in session.skills if ref not in (r, skills.label(r))]
    store.save(session)
    print(f"{session.id} · {session.model} · skills: {render.skills_label(session.skills)} · "
          f"options: {session.options or '{}'}")


def cmd_fork(args):
    store = open_store()
    new = store.fork(store.get(args.id).id, at_seq=args.at, title=args.title)
    print(f"{new.id}  {new.title}  ({new.message_count} messages)")


def cmd_rm(args):
    store = open_store()
    targets = [store.get(ref) for ref in args.ids]
    for s in targets:
        if not args.yes:
            if not sys.stdin.isatty():
                raise StoreError("refusing to delete without confirmation; pass -y")
            answer = input(f"delete {s.id} '{s.title or '(untitled)'}' "
                           f"({s.message_count} messages)? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print(f"kept {s.id}")
                continue
        store.delete(s.id)
        print(f"deleted {s.id}")


def cmd_export(args):
    store = open_store()
    session = store.get(args.id)
    messages = store.messages(session.id)
    text = (render.to_json(session, messages) if args.format == "json"
            else render.to_markdown(session, messages, thinking=args.thinking))
    if args.output or args.save:
        path = Path(args.output).expanduser() if args.output else render.export_path(
            session, args.format)
        try:
            render.write_export(path, text)
        except OSError as e:
            raise StoreError(f"can't write {files.display_path(path)}: {e.strerror or e}") from None
        print(f"wrote {files.display_path(path)}", file=sys.stderr)
    else:
        sys.stdout.write(text)


def _join_prompt(words, piped):
    """Join prompt words, putting piped text in place of each `-` as its own paragraph."""
    parts, run = [], []
    for word in words:
        if word == "-":
            parts += [" ".join(run), piped.strip()]
            run = []
        else:
            run.append(word)
    parts.append(" ".join(run))
    return "\n\n".join(p for p in parts if p.strip())


def cmd_ask(args):
    # Stdin is read only where a `-` asks for it (or when piped with no prompt at all): reading
    # it whenever it isn't a terminal would hang under cron, CI and other non-interactive callers.
    words = args.prompt or ([] if sys.stdin.isatty() else ["-"])
    prompt = _join_prompt(words, sys.stdin.read() if "-" in words else "")
    if not prompt:
        raise StoreError("nothing to ask: give a prompt, or pipe one in "
                         "(`-` marks where piped text goes)")

    client = Client()
    if args.session:
        store = open_store()
        session = store.get(args.session)
    else:
        store = open_store(":memory:" if args.no_save else None)
        session = store.draft(pick_model(client, args.model), system=args.system,
                              skills=[skills.normalize_ref(s) for s in args.skill])
    repl = Repl(store, client, session, out=sys.stdout, log=sys.stderr, quiet=True)
    try:
        reply = repl.send(prompt.strip())
    except KeyboardInterrupt:
        return 130
    if reply is None or reply.status != "complete":
        return 130 if reply is not None and reply.status == "interrupted" else 1
    return 0


def cmd_skills(args):
    if args.name:
        skill = skills.resolve(args.name)
        style = render.Style(render.use_color(sys.stdout))
        print(style.dim(f"{skill.name} · {skill.path} · {skill.sha}"))
        if skill.description:
            print(style.dim(skill.description))
        print()
        print(skill.body)
        return
    found = skills.discover()
    if not found:
        print(f"no skills found. Create {config.skills_dirs()[-1]}/<name>/SKILL.md")
        return
    width = max(len(n) for n in found)
    for name, skill in sorted(found.items()):
        print(f"{name.ljust(width)}  {skill.description}")


def cmd_models(args):
    client = Client()
    rows = [("NAME", "PARAMS", "QUANT", "SIZE", "CAPABILITIES")]
    for m in client.list_models():
        details = m.get("details") or {}
        try:
            caps = ", ".join(client.show(m["name"]).get("capabilities") or [])
        except OllamaError:
            caps = "?"
        rows.append((m["name"], details.get("parameter_size", ""),
                     details.get("quantization_level", ""),
                     f"{m.get('size', 0) / 1e9:.1f} GB", caps))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())


def build_parser():
    parser = argparse.ArgumentParser(
        prog=config.COMMAND, description="Agentless chat with local Ollama models. "
        f"Run with no command to start a new session; `{config.COMMAND} -c` continues the "
        "latest one.")
    parser.add_argument("--version", action="version", version=f"{config.COMMAND} {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name, func, help, aliases=()):
        p = sub.add_parser(name, help=help, description=help, aliases=list(aliases))
        p.set_defaults(func=func)
        return p

    def session_setup(p):
        p.add_argument("-m", "--model", help=f"model to use (default: $AC_MODEL, else {config.DEFAULT_MODEL})")
        p.add_argument("-s", "--skill", action="append", default=[], metavar="NAME|PATH",
                       help="attach a skill (repeatable)")
        p.add_argument("--system", help="session-specific system text")

    p = add("new", cmd_new, "start a new chat session")
    session_setup(p)
    p.add_argument("--title")

    p = add("resume", cmd_resume, "continue a session (the latest if no ID is given)")
    p.add_argument("id", nargs="?")

    p = add("ls", cmd_ls, "list sessions", aliases=["list"])
    p.add_argument("--search", metavar="QUERY", help="match titles and message text")

    p = add("show", cmd_show, "print a session's transcript")
    p.add_argument("id")
    p.add_argument("--thinking", action="store_true", help="include the model's reasoning")

    p = add("rename", cmd_rename, "rename a session")
    p.add_argument("id")
    p.add_argument("title", nargs="+")

    p = add("set", cmd_set, "change a session's model, system text, options or skills")
    p.add_argument("id")
    p.add_argument("-m", "--model")
    p.add_argument("--system")
    p.add_argument("--clear-system", action="store_true")
    p.add_argument("--think", choices=["on", "off", "default"])
    p.add_argument("-o", "--option", action="append", default=[], metavar="KEY=VALUE",
                   help="model option such as temperature=0.2 or num_ctx=32768; KEY= unsets")
    p.add_argument("--add-skill", action="append", default=[], metavar="NAME|PATH")
    p.add_argument("--rm-skill", action="append", default=[], metavar="NAME")

    p = add("fork", cmd_fork, "copy a session into a new one")
    p.add_argument("id")
    p.add_argument("--at", type=int, metavar="SEQ", help="copy messages up to this number only")
    p.add_argument("--title")

    p = add("rm", cmd_rm, "delete sessions", aliases=["delete"])
    p.add_argument("ids", nargs="+", metavar="id")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    p = add("export", cmd_export, "export a session as markdown or JSON")
    p.add_argument("id")
    p.add_argument("--format", choices=["md", "json"], default="md")
    p.add_argument("--thinking", action="store_true", help="include reasoning (markdown)")
    p.add_argument("-o", "--output", metavar="FILE")
    p.add_argument("--save", action="store_true",
                   help='write "DATE ac-ID.md" into the export folder instead of printing')

    p = add("ask", cmd_ask, "one-shot question; `-` or piped stdin supplies the prompt")
    session_setup(p)
    p.add_argument("-S", "--session", metavar="ID", help="ask within an existing session")
    p.add_argument("--no-save", action="store_true", help="don't keep this exchange as a session")
    p.add_argument("prompt", nargs="*")

    p = add("skills", cmd_skills, "list available skills, or show one")
    p.add_argument("name", nargs="?")

    add("models", cmd_models, "list installed Ollama models")
    return parser


def _default_command(argv):
    """`acc` and `acc -m x` mean `acc new ...`; `acc -c` means `acc resume`."""
    if not argv:
        return ["new"]
    if argv[0] in ("-c", "--continue"):
        return ["resume"] + argv[1:]
    if argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version"):
        return ["new"] + argv
    return argv


def main(argv=None):
    argv = _default_command(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except (StoreError, skills.SkillError, OllamaError) as e:
        print(f"{config.COMMAND}: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0
    finally:
        while _open_stores:
            _open_stores.pop().close()
