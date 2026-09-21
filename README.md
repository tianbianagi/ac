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
acc resume ID                # id, unique id prefix, or exact title

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

Inside a chat, `/help` lists the commands: session management (`/new /sessions /switch /rename
/fork /delete /export`), skills (`/skills`, `/skill add|rm`), prompt (`/system`, `/context`, `/files`),
model (`/model`, `/set`, `/think`), and conversation editing (`/retry /edit /undo /compact`).
Wrap multi-line input in `"""`. Ctrl-C stops a reply and keeps the partial text; Ctrl-D quits.

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
- **PDFs** are read as text, page by page, with `[page N]` markers so the model can cite pages.
  `report.pdf#10-20` (or `#7`) sends only those pages. A long PDF is cut at a page boundary
  (about 256 KB of text, very roughly 60k tokens) and you're told how to ask for the rest. A
  scanned PDF with no text layer is sent as images of its first 8 pages, for models with vision.
  No PDF library is involved: on macOS the system's PDFKit does the work through `osascript`;
  elsewhere `pdftotext` (poppler) is used if installed, and scans can't be rendered.
- The file is **snapshotted when you send the message** and stays in the conversation from then
  on, so later turns can refer to it and the history always matches what the model really saw.
  Name the path again to send its current contents. `/files` lists what a conversation holds.
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
- **Switching models.** `/model` lists installed models; `/model NAME` (a unique prefix is enough)
  or `/model NUMBER` switches mid-chat. The whole conversation carries over to the new model, the
  choice is saved with the session, and every reply records which model wrote it (`acc export
  --format json`). If the new model isn't in memory, you're told how much Ollama has to load first.
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
```

Exports are named `DATE ac-ID.md`, for example `2026-09-20 ac-efc6c96d.md`. The date is the day
the session began, so exporting a session again updates its file rather than adding another.
Without an `export_dir` they go to the current folder. A filename given to `/export` or `-o` is
used exactly as written.

| Variable | Meaning | Default |
| --- | --- | --- |
| `AC_MODEL` | model for new sessions | `qwen3.8:27b` (`DEFAULT_MODEL` in `ac/config.py`); first installed model if that is missing |
| `AC_SKILLS_PATH` | extra skill directories (`:`-separated), searched first | |
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
