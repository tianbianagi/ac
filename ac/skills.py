"""Skills: opt-in instruction files a session can attach.

A skill is a directory holding a SKILL.md: optional `---` frontmatter (name, description)
followed by a markdown body. Nothing in a skill is executed; the body is simply composed
into the system prompt of sessions that attach it. A session names its skills, and a name is
looked up in the skill libraries only: nothing outside them can be attached.
"""

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import config

FILENAME = "SKILL.md"


class SkillError(Exception):
    pass


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    sha: str


def parse(text):
    """Split SKILL.md text into (frontmatter dict, body).

    Frontmatter is a small YAML subset: `key: value` lines, optionally quoted, with indented
    continuation lines (including after `>` or `|`) folded into the value.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return {}, text.strip()
    meta, key = {}, None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t" and key:
            meta[key] = f"{meta[key]} {line.strip()}".strip()
            continue
        key, sep, value = line.partition(":")
        if not sep:
            key = None
            continue
        key, value = key.strip(), value.strip()
        if value in (">", "|", ">-", "|-"):
            value = ""
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        meta[key] = value
    return meta, "\n".join(lines[end + 1:]).strip()


def load(path):
    """The skill in a library folder (or its SKILL.md)."""
    path = Path(path)
    if path.is_dir():
        path = path / FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SkillError(f"can't read skill {path}: {e.strerror or e}") from None
    meta, body = parse(text)
    return Skill(name=path.parent.name, description=meta.get("description", ""), body=body,
                 path=path, sha=hashlib.sha256(text.encode()).hexdigest()[:12])


def discover(dirs=None):
    """All skills on the search path, by name. Earlier directories win."""
    found = {}
    for base in dirs if dirs is not None else config.skills_dirs():
        if not base.is_dir():
            continue
        for path in sorted(base.glob(f"*/{FILENAME}")):
            if path.parent.name not in found:
                try:
                    found[path.parent.name] = load(path)
                except SkillError:
                    continue
    return found


def label(ref):
    """Short display name for a stored ref. A session from before skills were names only may
    still hold a path; it shows by its folder."""
    if "/" not in ref:
        return ref
    path = Path(ref)
    return path.parent.name if path.name == FILENAME else path.stem


def normalize_ref(ref, dirs=None):
    """Validate a user-typed skill name; return the form stored on the session."""
    resolve(ref, dirs)
    return ref


def resolve(ref, dirs=None):
    """The skill a name stands for, searching the libraries in order. A name is one folder's
    name, never a path."""
    ref = str(ref)
    if ref and "/" not in ref and not ref.startswith("."):
        for base in dirs if dirs is not None else config.skills_dirs():
            path = base / ref / FILENAME
            if path.is_file():
                return load(path)
    available = ", ".join(sorted(discover(dirs))) or "none"
    raise SkillError(f"no skill named '{label(ref)}' (available: {available})")


def compose_system(system, refs, dirs=None):
    """Build the system prompt for one request.

    Returns (prompt or None, skills that were included, refs that could not be loaded).
    Skills are read from disk every time, so edits apply on the next turn.
    """
    parts, active, missing = [], [], []
    if system and system.strip():
        parts.append(system.strip())
    for ref in refs:
        try:
            skill = resolve(ref, dirs)
        except SkillError:
            missing.append(ref)
            continue
        active.append(skill)
        parts.append(f'<skill name="{skill.name}">\n{skill.body}\n</skill>')
    return ("\n\n".join(parts) or None), active, missing


# -- the library a user can change from the browser ---------------------------------------------

NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def library(user):
    """The folder a user's own skills live in."""
    return config.skills_dir(user)


def text(description, body):
    """A SKILL.md's text: the description as frontmatter, then the instructions."""
    description = " ".join(str(description or "").split())
    if len(description) >= 2 and description[0] == description[-1] and description[0] in "\"'":
        description = f"'{description}'" if description[0] == '"' else f'"{description}"'
    body = str(body or "").strip()
    return f"---\ndescription: {description}\n---\n\n{body}\n"   # so a body can't pass for frontmatter


def save(user, name, description, body, was=None):
    """Write a skill into the user's library, creating it or changing it. `was` is the name it
    had until now, so a skill can be renamed, with any other files in its folder going along."""
    name = str(name or "").strip()
    if not NAME.fullmatch(name):
        raise SkillError("a skill's name is lowercase letters, digits, - and _")
    if not str(body or "").strip():
        raise SkillError("a skill needs instructions")
    folder = library(user) / name
    old = library(user) / str(was) if was else None
    if old is not None and not (NAME.fullmatch(str(was)) and (old / FILENAME).is_file()):
        raise SkillError(f"no skill named '{was}'")
    if (folder / FILENAME).exists() and folder != old:
        raise SkillError(f"there is already a skill named '{name}'")
    folder.parent.mkdir(parents=True, exist_ok=True)
    if old is not None and folder != old:
        shutil.move(old, folder)
    folder.mkdir(exist_ok=True)
    (folder / FILENAME).write_text(text(description, body), encoding="utf-8")
    return load(folder)


def remove(user, name):
    """Delete a skill's SKILL.md, and its folder once nothing else is in it."""
    folder = library(user) / str(name)
    if not NAME.fullmatch(str(name)) or not (folder / FILENAME).is_file():
        raise SkillError(f"no skill named '{name}'")
    (folder / FILENAME).unlink()
    try:
        folder.rmdir()
    except OSError:
        pass            # other files the skill kept stay where they are
