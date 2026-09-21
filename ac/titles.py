"""Asking the model for a session title.

A session starts out titled with its first message. Before an export that is replaced, once,
by a title the model writes. Titles the user chose are never replaced.
"""

import re
from pathlib import Path

from .ollama import OllamaError

PROMPT = (
    "Below is a conversation between a user and an assistant. Write a title for it: 3 to 8 "
    "words, specific to what was actually discussed, in the language the user writes in. "
    "Reply with the title only: no quotes, no markdown, no trailing punctuation.")
MAX_LENGTH = 80
KEEP_MESSAGES = 6        # from each end of a long conversation
KEEP_CHARACTERS = 1200   # of each message


def _transcript(messages):
    """A bounded digest of the conversation, so naming a session costs about the same however
    long it is, and whatever files were attached to it."""
    usable = [m for m in messages if m.content.strip() and m.status != "error"]
    if len(usable) > 2 * KEEP_MESSAGES:
        usable = usable[:KEEP_MESSAGES] + usable[-KEEP_MESSAGES:]
    lines = []
    for m in usable:
        text = " ".join(m.content.split())
        if len(text) > KEEP_CHARACTERS:
            text = text[:KEEP_CHARACTERS] + " …"
        names = ", ".join(Path(a.path).name for a in m.attachments)
        lines.append(f"{m.role.upper()}{f' [attached: {names}]' if names else ''}: {text}")
    return "\n\n".join(lines)


def clean(raw):
    """The model's reply as a usable title, or None if there isn't one in it."""
    line = next((ln for ln in raw.strip().splitlines() if ln.strip()), "")
    line = " ".join(line.replace("`", "").replace("*", "").replace("#", "").split())
    line = re.sub(r"^(title|titel)\s*[:\-]\s*", "", line, flags=re.IGNORECASE)
    line = line.strip("\"'“”‘’ ").rstrip(".!,:; ")
    if len(line) > MAX_LENGTH:
        line = line[:MAX_LENGTH].rsplit(" ", 1)[0].rstrip(".!,:; ")
    return line or None


def generate(client, model, messages):
    """A title for the conversation, or None. Raises OllamaError if the model can't be reached."""
    digest = _transcript(messages)
    if not digest:
        return None
    # Not the session's own prompt: a skill that says "answer in haiku" must not name the file.
    payload = [{"role": "user", "content": f"{PROMPT}\n\n<conversation>\n{digest}\n</conversation>"}]
    stream = client.chat(model, payload, think=False, options={"temperature": 0.3})
    try:
        reply = "".join(text for kind, text in stream if kind == "content")
    finally:
        stream.close()
    return clean(reply)


def ensure(store, client, session, note, warn):
    """Give the session a model-written title unless it already has a chosen one.

    Never fails: if the model can't be asked, the existing title stays and the caller goes on.
    """
    if session.title and session.title_source in ("model", "user"):
        return
    note("naming this session…")
    try:
        title = generate(client, session.model, store.messages(session.id))
    except (OllamaError, KeyboardInterrupt) as e:
        return warn(f"couldn't ask the model for a title ({e or 'interrupted'}); "
                    "keeping the current one")
    if not title:
        return warn("the model didn't offer a usable title; keeping the current one")
    session.title, session.title_source = title, "model"
    store.save(session)
    note(f"titled: {title}")
