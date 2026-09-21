# ac

An agentless CLI chat for local [Ollama](https://ollama.com) models, with persistent sessions
and opt-in skills. Python 3.11+, standard library only.

**Agentless** means there is no global instruction file. A new session sends the model no system
prompt at all. Anything that shapes the model is attached to one session, explicitly, and can be
inspected (`/context`) and detached again.

The command is `acc`, because macOS already ships an unrelated `/usr/sbin/ac`.

## Install

```sh
ln -s "$PWD/bin/acc" ~/.local/bin/acc     # or run ./bin/acc, or python3 -m ac
```

## Use

```sh
acc                          # new session
acc -m qwen3.8:27b -s concise
acc -c                       # continue the latest session
acc resume                   # pick a session from a list: type to filter, arrows, Enter
acc resume ID                # ...or name one: id, unique id prefix, or exact title

acc ls [--search QUERY]      # full-text search over titles and messages
acc show ID                  acc rename ID TITLE
acc fork ID [--at SEQ]       acc rm ID... [-y]
acc set ID [-m MODEL] [--system TEXT] [--think on|off] [-o temperature=0.2] [--add-skill NAME]
acc export ID [--format md|json] [-o FILE | --save]   # prints unless told where to write

acc ask "question"           # one-shot; prints only the answer
git diff | acc ask "review this:" -        # `-` marks where piped stdin goes
acc ask --no-save "..."      # don't keep it as a session

acc skills [NAME]            acc models
```

Inside a chat, `/help` lists the commands: session management (`/new /sessions /rename
/title /fork /delete /export`), skills (`/skills`, `/skill add|rm`), prompt (`/system`, `/context`, `/files`),
model (`/models`, `/set`, `/think`), display (`/markdown`), and conversation editing (`/retry /edit /undo /compact`).
`/sessions` and `/models` work the same way. On their own they open a list to pick from: typing
narrows it (for sessions, by title, id or anything said in the conversation), the arrow keys move,
Enter chooses and Esc cancels. With an argument they go straight there: `/sessions 3e05` (an id,
id prefix, exact title, or a number), `/models qwen3.5`. Any other text after `/sessions` opens the
list already filtered by it. In the session list, Ctrl-D (or the forward-delete key) deletes the
highlighted session after a `y`, and the list stays open so you can clear out several; the session
you are in is protected there (`/delete` handles that one). Wrap multi-line input in `"""`. Ctrl-C stops a reply and keeps the partial text; Ctrl-D quits.

## Files

Name a path in your message and its contents are sent along with it:

```
>>> what does ~/notes/plan.md say about the budget?
attached ~/notes/plan.md (text, 3.1 KB)
```

- A word is treated as a path when it **exists** and starts with `/` or `~`, or contains a `/`
  (`./notes.md`, `src/main.py`). For a bare filename in the current directory, write `@notes.md`.
  Paths with spaces work quoted (`"my file.txt"`) or escaped (`my\ file.txt`, which is what
  dragging a file into the terminal types). Tab completes paths.
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
  is hidden. You get one line back, `attached 14 files from src/** (138 KB)`; `/files` lists them.
- **`/files` and names.** `/files` on its own opens a list, like `/sessions`: every file in the
  conversation, what is queued, and your names. Type to filter; Enter queues a fresh copy of the
  highlighted file (or a named set); Ctrl-D removes the row after a `y`: a file leaves the
  conversation and stops costing context, a queued file leaves the queue, a name is forgotten.
  Nothing on disk is ever touched. `/files PATH...` queues files for your next message: one path or
  several, and a path with spaces needs no quotes, because the longest run of words that names
  something real is taken as one path (quotes and backslash-escapes work too):
  `/files ~/Library/Mobile Documents/notes/*.md ~/plan.pdf`. If one of them matches nothing,
  nothing is queued. End with `@NAME` and the paths get a name *instead* of being queued (so
  that defining a name never sends files along with some unrelated next message): after
  `/files ~/vault/mom/*.md ~/photos/mom.md @mom`, writing `@mom` in any message, in any session
  or in `acc ask`, attaches whatever those paths hold at that moment. Only an `@` makes a name; a
  plain last word is just another path. An `@NAME` that isn't last is an existing name, so
  `/files @mom ~/dad/*.md @family` builds one name from another. `/files @mom` queues it,
  `/files forget mom` removes a name and `/files clear` empties the queue. Names complete with
  Tab. A name wins over a file of the same name, and if one of its paths has gone you are told
  and get the rest.
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
- This is not a tool the model can call. Only a path that *you type* is ever read: nothing in a
  skill or in the model's output can make `acc` open a file.
- It works in one-shots too: `acc ask "review ./diff.patch"`.

## Skills

A skill is a folder with a `SKILL.md`: optional frontmatter, then instructions.

```markdown
---
description: Short, direct answers with no preamble or recap
---
Answer in as few words as the question allows. ...
```

Put skills in `~/.config/ac/skills/<name>/SKILL.md` (see `examples/skills/`), or add directories
with `AC_SKILLS_PATH`. Attach by name, or by path to any markdown file: `/skill add ./notes/style.md`.

- Skills are **instructions only**. Nothing is executed and the model gets no tools.
- The system prompt is rebuilt on every turn from the session's own system text plus its attached
  skills, and is never written into the transcript. So skills can be added or removed mid-chat,
  edits to a SKILL.md apply on the next turn, and history stays a plain user/assistant log.
- Each reply records which skills (and which version, by hash) were active when it was written.
- The frontmatter parser handles `key: value` lines and folded values, not full YAML.

## Behaviour worth knowing

- **Context.** Ollama silently drops the oldest messages when the context fills. The status line
  after each reply shows tokens used against the model's real context window, and warns at 80% and
  95%. `/compact` continues in a *new* session seeded with a summary; the original is untouched.
  Set a window explicitly with `/set num_ctx 32768`.
- **Switching models.** `/models` lets you pick from the installed models; `/models NAME` (a
  unique prefix is enough) or `/models NUMBER` switches mid-chat. The whole conversation carries over to the new model, the
  choice is saved with the session, and every reply records which model wrote it (`acc export
  --format json`). If the new model isn't in memory, you're told how much Ollama has to load first.
- **Markdown** in replies is rendered for the terminal as it streams: headings, bold, italics,
  inline code, links, lists with hanging indents, quotes, tables, and word-wrapping to the window.
  Code blocks are printed exactly as written, never wrapped, so they copy out cleanly. Text is
  held back only until it can be read correctly: a bold or code span appears when it closes,
  and one that never closes (`*args, **kwargs`, `__init__`) is printed literally. This happens
  only on a terminal: piped or redirected output, exports and the stored transcript are always
  the model's own markdown. `/markdown off` shows replies raw; `markdown = false` in
  `config.toml` or `AC_MARKDOWN=0` makes that the default.
- **Thinking** is shown dimmed (`/think hide` collapses it), stored for `acc show --thinking`, and
  never sent back to the model.
- **Failures.** If a reply fails, your message is kept and `/retry` resends it. An interrupted
  reply is kept as-is and stays part of the conversation unless you `/retry` or `/undo`.
- Changing skills or system text changes the start of the prompt, so the next turn re-evaluates
  the whole conversation once. On large models that pause is noticeable.

## Configuration

Personal settings live in `~/.config/ac/config.toml` (optional):

```toml
# Where /export and `acc export --save` write when no filename is given.
export_dir = "~/Documents/chats"

# Render replies as markdown in the terminal (default true).
markdown = true

# How much text a pattern such as src/** may attach to one message, in KB (default 400).
max_attach_kb = 400

# What exports call the two sides of the conversation (default "User" and "Assistant").
user_name = "Sam"
assistant_name = "Robin"
```

Exports are named `DATE TITLE.md`, for example `2026-09-20 Planning a Trip to Lisbon.md`. The
date is the day the session began, so exporting a session again updates its file rather than
adding another. Without an `export_dir` they go to the current folder. A filename given to
`/export` or `-o` is used exactly as written.

The names only label exported transcripts (`## Sam`, `## Robin`). They are never sent to the
model, so they don't give it a persona; JSON exports keep `"role": "user"`/`"assistant"` and list
the names once at the top.

A session starts out titled with its first message. Before its first export the model is asked,
once, for a proper title (a short separate request that ignores the session's skills), and that
becomes the session's title everywhere. A title you chose (`--title`, `/rename`, `acc rename`)
is never replaced; `/title` asks the model for a new one on demand. If the model can't be
reached, the export goes ahead under the current title. Characters that are unsafe in filenames
are dropped, and if another session already owns the name, the session id is appended.

| Variable | Meaning | Default |
| --- | --- | --- |
| `AC_MODEL` | model for new sessions | `qwen3.8:27b` (`DEFAULT_MODEL` in `ac/config.py`); first installed model if that is missing |
| `AC_SKILLS_PATH` | extra skill directories (`:`-separated), searched first | |
| `AC_MARKDOWN` | `0` shows replies as raw markdown; overrides `markdown` in `config.toml` | rendered |
| `AC_EXPORT_DIR` | export folder; overrides `export_dir` in `config.toml` | current folder |
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
