"""Skills: opt-in instruction files a session can attach.

A skill is a directory holding a SKILL.md: optional `---` frontmatter (name, description)
followed by a markdown body. Nothing in a skill is executed; the body is simply composed
into the system prompt of sessions that attach it.
"""

import hashlib
import os
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
    path = Path(path).expanduser()
    if path.is_dir():
        path = path / FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SkillError(f"can't read skill {path}: {e.strerror or e}") from None
    meta, body = parse(text)
    # The name is what the user types to attach it: the directory (or file) name.
    name = path.parent.name if path.name == FILENAME else path.stem
    return Skill(name=name, description=meta.get("description", ""), body=body, path=path,
                 sha=hashlib.sha256(text.encode()).hexdigest()[:12])


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


def _is_path(ref):
    return os.sep in ref or ref.endswith(".md") or ref.startswith(("~", "."))


def label(ref):
    """Short display name for a stored ref."""
    if not _is_path(ref):
        return ref
    path = Path(ref)
    return path.parent.name if path.name == FILENAME else path.stem


def normalize_ref(ref, dirs=None):
    """Validate a user-typed skill reference; return the form stored on the session."""
    if _is_path(ref):
        return str(load(Path(ref).expanduser().resolve()).path)
    resolve(ref, dirs)
    return ref


def resolve(ref, dirs=None):
    if _is_path(ref):
        return load(ref)
    for base in dirs if dirs is not None else config.skills_dirs():
        path = base / ref / FILENAME
        if path.is_file():
            return load(path)
    available = ", ".join(sorted(discover(dirs))) or "none"
    raise SkillError(f"no skill named '{ref}' (available: {available})")


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
