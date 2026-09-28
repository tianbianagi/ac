# ac

An agentless chat for local [Ollama](https://ollama.com) models, in the browser, with persistent
sessions and opt-in skills, plus a few terminal commands for scripting. Python 3.11+, standard
library only.

**Agentless** means there is no global instruction file. A new session sends the model no system
prompt at all. Anything that shapes the model is attached to one session, explicitly, and can be
inspected and detached again.

The command is `acc`, because macOS already ships an unrelated `/usr/sbin/ac`.

## Install

```sh
ln -s "$PWD/bin/acc" ~/.local/bin/acc     # or run ./bin/acc, or python3 -m ac
```

## Use

`acc serve` opens the app in the browser at `http://127.0.0.1:8765/` (`--port N` picks another
port, `--no-open` skips opening a window). The sidebar lists your sessions, with search (the ‹ at its bottom, or ⌘B / Ctrl+B, shrinks it to a rail of icons for new chat and search; › widens it again); the pencil
starts a new chat with the default model. Enter sends and Shift-Enter starts a new line; pasted
text keeps its line breaks. Replies stream in rendered as markdown, with reasoning folded away;
**Stop** keeps the partial reply, and **Retry** asks again after a reply failed
or was stopped. A reply goes on being written when you close or reload the page, lose the
connection or switch to another chat; opening its session again picks it up where it is (only
restarting `acc serve` loses a reply still being written). A session takes one reply at a time. Attach files with the paperclip, by dropping them on the chat, or by pasting an image; they go with the message, are read as described under [Files](#files), and attached images show in the conversation. The × on a sent file takes it out of the conversation, so the model stops seeing it from the next message on. The model menu and **Skills** switch the session's model and skills, or set them for a new chat before its first message.
The header, beside the session id, shows the context in use as a share of the model's window (Ollama's own figure while the model is loaded, otherwise marked ~ and taken from `num_ctx` or the model's maximum) (amber at 80%, red at 95%, when Ollama starts dropping the oldest messages) and how fast the latest reply came, counted live while it streams. The chevron at the header's right folds it to just the title, for more room on a phone; each browser remembers the choice. Hovering a message shows copy (the text as written, markdown for a reply) and delete, which takes just that message and its files out of the conversation. The pencil beside the title (or a double-click on it) renames the open session; the archive, export and delete icons at the top right act on it too. Archiving takes a session out of the list without deleting it: **Archived** at the bottom of the list shows those, a new message brings one back, and `acc ls --archived` lists them in the terminal, where `acc ls` leaves them out. Export
downloads the session as `DATE TITLE.md`; it
first has the model name a session that still carries its first message as its title. The app
can be installed from the browser (Add to Home Screen on an iPhone, Install in Chrome, Add to
Dock in Safari) and then opens in its own window.

It listens
only on this machine, answers only to `127.0.0.1` or `localhost`, and accepts changes only from
its own page, so a web page elsewhere can neither read your sessions nor send messages through it.
To reach it from another device through a proxy of your own, add the name the proxy passes on,
`acc serve --allow-host acc.example.com`; the proxy must do the authenticating, since the server
itself has no login.

acc can serve more than one person, each with their own sessions and skills; none of
them sees the others'. Add each extra user to `config.toml` as a `[users.NAME]` table (see
[Configuration](#configuration)). The proxy says who is asking in an `X-Acc-User: NAME` header,
typically taken from the client certificate it checked; a request without that header, such as
from a browser on this machine, is yours (the owner, known by your login name), and one naming a
user `config.toml` doesn't list is turned away. The terminal commands always act as the owner.
Nothing anyone sends can read a file on this machine (see [Files](#files)), so a user gets
their own conversations, the models, and the skills, and nothing else.

The other commands work on the same sessions from the terminal:

```sh
acc ls [--search QUERY] [--archived]   # full-text search over titles and messages
acc show ID [--thinking]     acc rename ID TITLE
acc fork ID [--at SEQ]       acc rm ID... [-y]
acc set ID [-m MODEL] [--system TEXT] [--think on|off] [-o temperature=0.2] [--add-skill NAME]
acc export ID [--thinking] [--save PATH]       # markdown; prints unless given a file or folder

acc ask "question"           # one-shot; prints only the answer
git diff | acc ask "review this:" -        # `-` marks where piped stdin goes
acc ask --no-save "..."      # don't keep it as a session
acc ask -S ID "..."          # ...or ask within an existing session

acc skills [NAME]            acc models
```

A session is named by its id, a unique id prefix, or its exact title.

## Files

A file reaches the model only as part of a message you send from the browser: attach it with
the paperclip, drop it on the chat, or paste an image. The server reads it once, keeps its
contents with that message, and writes nothing to disk. In the terminal, pipe a file's contents
in where `-` stands: `acc ask "summarize this:" - < notes.md`.

- **Text files** are inlined (the first 256 KB of larger ones, and you're told when that happens).
  **Images** (png, jpg, webp, gif) go to models with vision; other models get a warning instead.
  Other binary files are refused.
- **PDFs** are read as text, page by page, with `[page N]` markers so the model can cite pages.
  A long PDF is cut at a page boundary (about 256 KB of text, very roughly 60k tokens) and you're
  told how much was sent. A scanned PDF with no text layer is sent as images of its first 8
  pages, for models with vision. No PDF library is involved: on macOS the system's PDFKit does
  the work through `osascript`; elsewhere `pdftotext` (poppler) is used if installed, and scans
  can't be rendered.
- The file is **snapshotted when you send the message** and stays in the conversation from then
  on, so later turns can refer to it and the history always matches what the model really saw.
  Send it again to send its current contents.
  Exports name the files that were attached but never include their contents.
- Nothing names a path on the machine running `acc serve`: not a message (a path in one is just
  words), not a skill, not the model's output. acc never opens a file it wasn't handed, so
  someone reaching it through a proxy can read their own conversations and nothing else.

## Skills

A skill is a folder with a `SKILL.md`: optional frontmatter, then instructions.

```markdown
---
description: Short, direct answers with no preamble or recap
---
Answer in as few words as the question allows. ...
```

Each user has a skill library of their own, `~/accspace/config/users/<user>/skills/<name>/SKILL.md`
(see `examples/skills/`), and nobody else's: no skill is shared, so nothing one user writes can
end up in another's prompt. The owner can add read-only directories of skills with
`AC_SKILLS_PATH`. In the browser, **Skills → Manage skills…** makes, edits, renames and deletes
your skills. Attach them from **Skills** in the browser, or with `acc set ID --add-skill NAME` and
`acc ask -s NAME`. A skill is always named, never given by path: only your library is read.

- Skills are **instructions only**. Nothing is executed and the model gets no tools.
- The system prompt is rebuilt on every turn from the session's own system text plus its attached
  skills, and is never written into the transcript. So skills can be added or removed mid-chat,
  edits to a SKILL.md apply on the next turn, and history stays a plain user/assistant log.
- Each reply records which skills (and which version, by hash) were active when it was written.
- The frontmatter parser handles `key: value` lines and folded values, not full YAML.

## Behaviour worth knowing

- **Context.** Ollama silently drops the oldest messages when the context fills; the header's
  meter warns before that happens. Set a window explicitly with `acc set ID -o num_ctx=32768`.
- **Switching models.** The whole conversation carries over to the new model, the choice is
  saved with the session, and every reply records which model wrote it.
- **Thinking** is folded under each reply, stored for `acc show --thinking`, and never sent back
  to the model.
- **Failures.** If a reply fails, your message is kept and **Retry** resends it. A stopped reply
  is kept as-is and stays part of the conversation.
- Changing skills or system text changes the start of the prompt, so the next turn re-evaluates
  the whole conversation once. On large models that pause is noticeable.

## Configuration

Personal settings live in `~/accspace/config/config.toml` (optional):

```toml
# What exports call the two sides of the conversation (default "User" and "Assistant").
user_name = "Sam"
assistant_name = "Robin"

# Someone else acc serves (see Use). Their table can set any of the settings above for them;
# they are called by their own name ("Alex") unless it sets user_name.
[users.alex]
assistant_name = "Robin"
```

`acc export --save` always takes a path (`-o` is the same option); there is no default export
folder. Given a folder (one that exists, or a path ending in `/`), it writes `DATE TITLE.md` there,
for example `2026-09-20 Planning a Trip to Lisbon.md`; a browser download gets the same name. The
date is the day the session began, so exporting a session again updates its file rather than
adding another. Any other path is used exactly as written.

The names only label exported transcripts (`## Sam`, `## Robin`). They are never sent to the
model, so they don't give it a persona.

A session starts out titled with its first message. Before its first export the model is asked,
once, for a proper title (a short separate request that ignores the session's skills), and that
becomes the session's title everywhere. A title you chose (renaming in the browser, `acc rename`)
is never replaced. If the model can't be
reached, the export goes ahead under the current title. Characters that are unsafe in filenames
are dropped, and if another session already owns the name, the session id is appended.

| Variable | Meaning | Default |
| --- | --- | --- |
| `AC_MODEL` | model for new sessions | `qwen3.8:27b` (`DEFAULT_MODEL` in `ac/config.py`); first installed model if that is missing |
| `AC_SKILLS_PATH` | the owner's extra skill directories (`:`-separated), searched first | |
| `AC_CONFIG_DIR` | folder holding `config.toml` and `users/` | `~/accspace/config` |
| `AC_DB` | session database | `~/.local/share/ac/ac.db` |
| `OLLAMA_HOST` | Ollama server | `127.0.0.1:11434` |
| `AC_DEBUG=1` | print each request payload to stderr | |
| `NO_COLOR` | disable colour | |

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

The suite runs against an in-process fake Ollama server; it needs no network or models.

## License

[MIT](LICENSE)
