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
or was stopped. Attach files with the upload button, by dropping them on the chat, or by pasting an image; they are read as described under [Files](#files), and attached images show in the conversation. The folder button picks files on the machine running `acc serve`, however you reach it: browse folders from your home, filter, and tick as many files as you like; ticking a folder takes everything in it, as `folder/**` would (hidden, ignored and binary files stay out). The box under the list holds everything queued, one path per line, and follows your ticks; edit it and press Enter to queue a path, a pattern such as `~/notes/**/*.md` or one of your `@names`, or delete a line to unqueue it. Paths named in a message are attached as well. The × on a sent file takes it out of the conversation, so the model stops seeing it from the next message on; the file itself is never touched. The model menu and **Skills** switch the session's model and skills, or set them for a new chat before its first message.
The header, beside the session id, shows the context in use as a share of the model's window (Ollama's own figure while the model is loaded, otherwise marked ~ and taken from `num_ctx` or the model's maximum) (amber at 80%, red at 95%, when Ollama starts dropping the oldest messages) and how fast the latest reply came, counted live while it streams. The chevron at the header's right folds it to just the title, for more room on a phone; each browser remembers the choice. Hovering a message shows copy (the text as written, markdown for a reply) and delete, which takes just that message and its files out of the conversation. The pencil beside the title (or a double-click on it) renames the open session; the archive, export and delete icons at the top right act on it too. Archiving takes a session out of the list without deleting it: **Archived** at the bottom of the list shows those, a new message brings one back, and `acc ls --archived` lists them in the terminal, where `acc ls` leaves them out. Export
saves `DATE TITLE.md` into your export folder or downloads it; it
first has the model name a session that still carries its first message as its title. The app
can be installed from the browser (Add to Home Screen on an iPhone, Install in Chrome, Add to
Dock in Safari) and then opens in its own window.

It listens
only on this machine, answers only to `127.0.0.1` or `localhost`, and accepts changes only from
its own page, so a web page elsewhere can neither read your sessions nor send messages through it.
To reach it from another device through a proxy of your own, add the name the proxy passes on,
`acc serve --allow-host acc.example.com`; the proxy must do the authenticating, since the server
itself has no login.

acc can serve more than one person, each with their own sessions, tags and skills; none of
them sees the others'. Add each extra user to `config.toml` as a `[users.NAME]` table (see
[Configuration](#configuration)). The proxy says who is asking in an `X-Acc-User: NAME` header,
typically taken from the client certificate it checked; a request without that header, such as
from a browser on this machine, is yours (the owner, known by your login name), and one naming a
user `config.toml` doesn't list is turned away. The terminal commands always act as the owner.
Everyone can read the same files on this machine: users keep their conversations apart, not
their files.

The other commands work on the same sessions from the terminal:

```sh
acc ls [--search QUERY] [--archived]   # full-text search over titles and messages
acc show ID [--thinking]     acc rename ID TITLE
acc fork ID [--at SEQ]       acc rm ID... [-y]
acc set ID [-m MODEL] [--system TEXT] [--think on|off] [-o temperature=0.2] [--add-skill NAME]
acc export ID [--thinking] [-o FILE | --save]   # markdown; prints unless told where to write

acc ask "question"           # one-shot; prints only the answer
git diff | acc ask "review this:" -        # `-` marks where piped stdin goes
acc ask --no-save "..."      # don't keep it as a session
acc ask -S ID "..."          # ...or ask within an existing session

acc skills [NAME]            acc models
```

A session is named by its id, a unique id prefix, or its exact title.

## Files

Name a path in a message, in the browser or in `acc ask`, and its contents are sent along with
it: `what does ~/notes/plan.md say about the budget?`

- A word is treated as a path when it **exists** and starts with `/` or `~`, or contains a `/`
  (`./notes.md`, `src/main.py`). For a bare filename in the current directory, write `@notes.md`.
  Paths with spaces work quoted (`"my file.txt"`) or escaped (`my\ file.txt`).
- **Text files** are inlined (the first 256 KB of larger ones, and you're told when that happens).
  **Images** (png, jpg, webp, gif) go to models with vision; other models get a warning instead.
  A **directory** becomes a listing of its entries. Other binary files are refused.
- **A whole folder**: a path with a `*` in it is a pattern. `src/**` sends every file under
  `src`, however deep; `docs/**/*.md` only the markdown; `@*.py` the Python files right here.
  (A folder named *without* a star still sends just a listing of it, so mentioning `~/` in
  passing can't pull in your home directory.) Left out automatically: hidden files and folders,
  dependency and build folders (`node_modules`, `__pycache__`, `venv`, ...), anything your
  `.gitignore` excludes, images, and files that aren't text. It stops at 200 files or 400 KB of
  text (about 100k tokens; `max_attach_kb` in `config.toml` changes that) and tells you what it
  left out. What you spell out is taken as meant, so `~/.config/ac/**` works although `.config`
  is hidden.
- **Tags.** Every file chip, queued or already sent, has a `#`: tap it and type a tag (Enter
  saves, Esc cancels) to tag that file. A tag points at a file on the machine running
  `acc serve`, so tagging a file uploaded from your device first keeps a copy of it in
  `~/accspace/uploads/<user>` (`AC_UPLOADS_DIR` moves that); one already sent is kept as the model saw
  it, so a PDF becomes its text. A tag belongs to the session it was made in: from then on
  `@TAG` in any message of that session (from the browser or `acc ask -S`) attaches every file
  with that tag, as it is at that moment, and other sessions don't see it. A forked session takes
  its tags along; a new chat keeps its tags until its first message makes the session. A
  file's tags show on its chip; tap one, then again to confirm, to untag it, and a tag with no
  files left is forgotten (the files themselves are never touched). Tags are listed at the top of
  the folder picker, where one can be queued like a file. A tag wins over a file of the same
  name, and if one of its files has gone you are told and get the rest.
- **PDFs** are read as text, page by page, with `[page N]` markers so the model can cite pages.
  `report.pdf#10-20` (or `#7`) sends only those pages. A long PDF is cut at a page boundary
  (about 256 KB of text, very roughly 60k tokens) and you're told how to ask for the rest. A
  scanned PDF with no text layer is sent as images of its first 8 pages, for models with vision.
  No PDF library is involved: on macOS the system's PDFKit does the work through `osascript`;
  elsewhere `pdftotext` (poppler) is used if installed, and scans can't be rendered.
- The file is **snapshotted when you send the message** and stays in the conversation from then
  on, so later turns can refer to it and the history always matches what the model really saw.
  Name the path again to send its current contents.
  Exports name the files that were attached but never include their contents.
- This is not a tool the model can call. Only a path that *you* give is ever read: nothing in a
  skill or in the model's output can make `acc` open a file.

## Skills

A skill is a folder with a `SKILL.md`: optional frontmatter, then instructions.

```markdown
---
description: Short, direct answers with no preamble or recap
---
Answer in as few words as the question allows. ...
```

Put skills in `~/accspace/config/skills/<name>/SKILL.md` (see `examples/skills/`), or add directories
with `AC_SKILLS_PATH`. Skills there are everyone's; one user's own go in
`~/accspace/config/users/<user>/skills/<name>/SKILL.md`, and win over a shared skill of that name.
In the browser, **Skills → Manage skills…** makes, edits, renames and deletes both kinds, and moves
a skill between just yours and everyone's. Attach them from **Skills** in the browser, or with `acc set ID --add-skill`
and `acc ask -s`, by name or by path to any markdown file: `acc ask -s ./notes/style.md "..."`.

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
# Where exports are saved when no filename is given.
export_dir = "~/Documents/chats"

# How much text a pattern such as src/** may attach to one message, in KB (default 400).
max_attach_kb = 400

# What exports call the two sides of the conversation (default "User" and "Assistant").
user_name = "Sam"
assistant_name = "Robin"

# Someone else acc serves (see Use). Their table can set any of the settings above for them;
# they are called by their own name ("Alex") unless it sets user_name.
[users.alex]
assistant_name = "Robin"
```

Exports are named `DATE TITLE.md`, for example `2026-09-20 Planning a Trip to Lisbon.md`. The
date is the day the session began, so exporting a session again updates its file rather than
adding another. Without an `export_dir` they go to the current folder. A filename given to
`-o` is used exactly as written.

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
| `AC_SKILLS_PATH` | extra skill directories (`:`-separated), searched first | |
| `AC_UPLOADS_DIR` | where uploads are kept once tagged, in a folder per user | `~/accspace/uploads` |
| `AC_EXPORT_DIR` | export folder; overrides `export_dir` in `config.toml` | current folder |
| `AC_CONFIG_DIR` | folder holding `config.toml` and `skills/` | `~/accspace/config` |
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
