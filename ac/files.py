"""Attaching local files to a message.

There is no file tool for the model to call. When the user's own message names a path that
exists, the client reads it and sends the contents along. Only what the user types can cause
a read; skills and model output never can.
"""

import os
import re
from pathlib import Path

from .store import Attachment

MAX_TEXT_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 200
MAX_ATTACHMENTS = 20
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# A double-quoted phrase, a single-quoted phrase, or a bare word that may contain "\ ".
_TOKEN = re.compile(r'"([^"]+)"|\'([^\']+)\'|((?:\\.|[^\s\\])+)')
_TRAILING = ".,;:!?)]}>"
_LEADING = "([{<"


class FileError(Exception):
    pass


def _candidates(text):
    """Strings in the message that might be paths, most literal reading first."""
    for quoted, single, bare in _TOKEN.findall(text):
        if quoted or single:
            yield quoted or single, False
            continue
        word = re.sub(r"\\(.)", r"\1", bare)  # a path dragged into a terminal has "\ " in it
        explicit = word.startswith("@")
        word = word[1:] if explicit else word
        yield word, explicit
        stripped = word.lstrip(_LEADING).rstrip(_TRAILING)
        if stripped != word:
            yield stripped, explicit


def _looks_like_path(word):
    return len(word) > 1 and (word.startswith(("/", "~")) or "/" in word)


def find_paths(text):
    """Existing files and directories named in a message, in order, without duplicates.

    A word counts when it looks like a path (starts with / or ~, or contains a /) or is marked
    with a leading @, which is how to name a bare file in the current directory: @notes.md.
    """
    found = []
    for word, explicit in _candidates(text):
        if not (explicit or _looks_like_path(word)):
            continue
        try:
            path = Path(word).expanduser()
            if not path.exists():
                continue
            path = path.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if path not in found:
            found.append(path)
    return found


def _size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def read(path):
    """Snapshot one path as an Attachment. Raises FileError when it can't be attached."""
    path = Path(path)
    try:
        if path.is_dir():
            return _read_directory(path)
        size = path.stat().st_size
        if path.suffix.lower() in IMAGE_SUFFIXES:
            if size > MAX_IMAGE_BYTES:
                raise FileError(f"{path} is too large to attach ({_size(size)}; images are "
                                f"limited to {_size(MAX_IMAGE_BYTES)})")
            return Attachment(path=str(path), kind="image", data=path.read_bytes())
        with open(path, "rb") as f:
            raw = f.read(MAX_TEXT_BYTES + 1)
    except OSError as e:
        raise FileError(f"can't read {path}: {e.strerror or e}") from None

    truncated = len(raw) > MAX_TEXT_BYTES
    raw = raw[:MAX_TEXT_BYTES]
    try:
        if b"\0" in raw:
            raise ValueError
        # A cut can land inside a multi-byte character; only then is a decode error forgivable.
        content = raw.decode("utf-8", errors="ignore" if truncated else "strict")
    except ValueError:
        raise FileError(f"{path} isn't text or an image, so it can't be attached") from None
    note = f"first {_size(len(raw))} of {_size(size)}" if truncated else None
    return Attachment(path=str(path), kind="text", content=content, note=note)


def _read_directory(path):
    entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    names = [p.name + ("/" if p.is_dir() else "") for p in entries]
    note = None
    if len(names) > MAX_DIRECTORY_ENTRIES:
        note = f"first {MAX_DIRECTORY_ENTRIES} of {len(names)} entries"
        names = names[:MAX_DIRECTORY_ENTRIES]
    return Attachment(path=str(path), kind="directory", content="\n".join(names), note=note)


def collect(text):
    """Attachments for every path named in a message, plus a problem line for each failure."""
    attachments, problems = [], []
    paths = find_paths(text)
    if len(paths) > MAX_ATTACHMENTS:
        problems.append(f"only the first {MAX_ATTACHMENTS} of {len(paths)} paths were attached")
    for path in paths[:MAX_ATTACHMENTS]:
        try:
            attachments.append(read(path))
        except FileError as e:
            problems.append(str(e))
    return attachments, problems


def display_path(path):
    """An absolute path for showing to the user, with ~ standing in for the home directory."""
    path, home = str(Path(path).expanduser().absolute()), str(Path.home())
    if path == home or path.startswith(home + os.sep):
        path = "~" + path[len(home):]
    return path


def describe(attachment):
    """One line for the user: where it is, what it is, and how big."""
    detail = f"{attachment.kind}, {_size(attachment.size)}"
    note = f", {attachment.note}" if attachment.note else ""
    return f"{display_path(attachment.path)} ({detail}{note})"


def for_model(attachment):
    """How an attachment appears in the text sent to the model."""
    note = f' note="{attachment.note}"' if attachment.note else ""
    if attachment.kind == "image":
        return f'<image path="{attachment.path}"/>'
    tag = "directory" if attachment.kind == "directory" else "file"
    return f'<{tag} path="{attachment.path}"{note}>\n{attachment.content}\n</{tag}>'


def complete(word):
    """Tab-completion candidates for a partly typed path, in the style the user typed it."""
    marker = "@" if word.startswith("@") else ""
    typed = word[len(marker):]
    head, _, partial = typed.rpartition("/")
    directory = Path(head + "/" if head or typed.startswith("/") else ".").expanduser()
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError:
        return []
    prefix = f"{head}/" if head or typed.startswith("/") else ""
    matches = []
    for entry in entries:
        if entry.name.startswith(partial) and (partial.startswith(".") or
                                                not entry.name.startswith(".")):
            name = entry.name.replace(" ", "\\ ")
            matches.append(f"{marker}{prefix}{name}{'/' if entry.is_dir() else ''}")
    return matches
