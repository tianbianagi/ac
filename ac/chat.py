"""What the browser app and `acc ask` share: the payload sent to Ollama, and one exchange."""

import base64
import sys

from . import files, skills
from .ollama import OllamaError
from .store import first_message_title

# Session options that are ours; every other key is passed to Ollama as a model option.
RESERVED_OPTIONS = ("think", "show_thinking", "keep_alive")


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


def ask(store, client, session, text, out=None, log=None):
    """Send one message and stream the reply's text (never its thinking) to `out`, saving both.
    Notes and errors go to `log`. Returns the reply's status: complete, interrupted or error."""
    out, log = out or sys.stdout, log or sys.stderr

    def note(line):
        print(line, file=log)

    if not session.persisted:
        if not session.title:
            session.title, session.title_source = first_message_title(text), "auto"
        store.save(session)
    attachments, problems = files.collect(text, store.resources(session.id))
    for line in problems + files.announce(attachments):
        note(line)
    store.add_message(session.id, "user", text, attachments=attachments)

    system, active, missing = skills.compose_system(session.system, session.skills)
    for ref in missing:
        note(f"skill '{skills.label(ref)}' can't be loaded; continuing without it")
    history = store.messages(session.id)
    images = sum(a.kind == "image" for m in history for a in m.attachments)
    vision = not images or client.supports(session.model, "vision")
    if not vision:
        note(f"{session.model} can't see images; leaving {images} out")

    options = {k: v for k, v in session.options.items() if k not in RESERVED_OPTIONS}
    content, thinking, stats, status = [], [], {}, "complete"
    stream = client.chat(session.model, build_messages(system, history, vision),
                         think=session.options.get("think"), options=options,
                         keep_alive=session.options.get("keep_alive"))
    try:
        for kind, data in stream:
            if kind == "done":
                stats = data
            elif kind == "thinking":
                thinking.append(data)
            else:
                content.append(data)
                out.write(data)
                out.flush()
    except KeyboardInterrupt:
        status = "interrupted"
    except OllamaError as e:
        status = "error"
        note(f"error: {e}")
    finally:
        stream.close()
    if content:
        out.write("\n")

    if content or thinking:
        total_ns = stats.get("total_duration")
        store.add_message(
            session.id, "assistant", "".join(content), thinking="".join(thinking) or None,
            status=status, model=session.model,
            skills=[{"name": k.name, "sha": k.sha} for k in active],
            prompt_tokens=stats.get("prompt_eval_count"), eval_tokens=stats.get("eval_count"),
            duration_ms=total_ns // 1_000_000 if total_ns else None)
    return status
