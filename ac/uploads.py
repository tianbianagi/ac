"""Cleaning up uploaded files no tag needs any more: `acc uploads`.

A file is kept in the uploads folder only so a tag can point at it (the conversation it came
with keeps its own copy of what was sent). Once no tag in any session of any user holds it, and
none has for a while, it is garbage: moved to the Trash, never deleted outright.
"""

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

DEFAULT_DAYS = 7


@dataclass
class Unused:
    path: Path
    since: datetime     # the later of when it was saved and when a tag last let go of it
    size: int


def unused(store, days=DEFAULT_DAYS, now=None):
    """Files in the uploads folder (every user's) that no tag holds and none has for `days`
    days, oldest first. Hidden files, such as iCloud's placeholders, are left alone."""
    root = config.uploads_dir()
    if not root.is_dir():
        return []
    now = now or datetime.now(timezone.utc)
    tagged, released = store.tagged_everywhere(), store.released()
    found = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            path = Path(folder) / name
            real = os.path.realpath(path)
            if name.startswith(".") or real in tagged or not path.is_file():
                continue
            stat = path.stat()
            since = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            if real in released:
                since = max(since, datetime.fromisoformat(released[real]))
            if now - since >= timedelta(days=days):
                found.append(Unused(path, since, stat.st_size))
    return sorted(found, key=lambda u: u.since)


def to_trash(path):
    """Move a file to the Trash, beside any of the same name as "name 2.ext"; where it went."""
    trash = Path.home() / ".Trash"
    trash.mkdir(exist_ok=True)
    stem, suffix = path.stem, path.suffix
    for n in range(1, 10000):
        target = trash / (path.name if n == 1 else f"{stem} {n}{suffix}")
        if not target.exists():
            return Path(shutil.move(path, target))
    raise OSError(f"the Trash already holds too many files named {path.name}")
