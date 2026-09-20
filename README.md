# ac

An agentless CLI chat for local [Ollama](https://ollama.com) models, with persistent sessions
and opt-in skills. Python 3.11+, standard library only.

**Agentless** means there is no global instruction file. A new session sends the model no system
prompt at all. Anything that shapes the model is attached to one session, explicitly, and can be
inspected (`/context`) and detached again.

## Install

```sh
ln -s "$PWD/bin/ac" ~/.local/bin/ac     # or run ./bin/ac, or python3 -m ac
```

## Use

```sh
ac                          # new session
ac -m qwen3.8:27b -s concise
ac -c                       # continue the latest session
ac resume ID                # id, unique id prefix, or exact title

ac ls [--search QUERY]      # full-text search over titles and messages
ac show ID                  ac rename ID TITLE
ac fork ID [--at SEQ]       ac rm ID... [-y]
ac set ID [-m MODEL] [--system TEXT] [--think on|off] [-o temperature=0.2] [--add-skill NAME]
ac export ID [--format md|json] [-o FILE]

ac ask "question"           # one-shot; prints only the answer
git diff | ac ask "review this:" -        # `-` marks where piped stdin goes
ac ask --no-save "..."      # don't keep it as a session

ac skills [NAME]            ac models
```

Inside a chat, `/help` lists the commands: session management (`/new /sessions /switch /rename
/fork /delete /export`), skills (`/skills`, `/skill add|rm`), prompt (`/system`, `/context`),
model (`/model`, `/set`, `/think`), and conversation editing (`/retry /edit /undo /compact`).
Wrap multi-line input in `"""`. Ctrl-C stops a reply and keeps the partial text; Ctrl-D quits.

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
  choice is saved with the session, and every reply records which model wrote it (`ac export
  --format json`). If the new model isn't in memory, you're told how much Ollama has to load first.
- **Thinking** is shown dimmed (`/think hide` collapses it), stored for `ac show --thinking`, and
  never sent back to the model.
- **Failures.** If a reply fails, your message is kept and `/retry` resends it. An interrupted
  reply is kept as-is and stays part of the conversation unless you `/retry` or `/undo`.
- Changing skills or system text changes the start of the prompt, so the next turn re-evaluates
  the whole conversation once. On large models that pause is noticeable.

## Configuration

| Variable | Meaning | Default |
| --- | --- | --- |
| `AC_MODEL` | model for new sessions | `qwen3.8:27b` (`DEFAULT_MODEL` in `ac/config.py`); first installed model if that is missing |
| `AC_SKILLS_PATH` | extra skill directories (`:`-separated), searched first | |
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
