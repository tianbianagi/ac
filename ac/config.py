"""Paths, environment variables and defaults."""

import getpass
import os
import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

APP = "ac"
COMMAND = "acc"  # not "ac": macOS ships an unrelated /usr/sbin/ac
DEFAULT_MODEL = "qwen3.8:27b"


def _xdg(var, fallback):
    base = os.environ.get(var)
    return (Path(base) if base else Path.home() / fallback) / APP


def db_path():
    override = os.environ.get("AC_DB")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_DATA_HOME", ".local/share") / "ac.db"


def config_dir():
    """Folder holding config.toml and skills/: $AC_CONFIG_DIR, else ~/accspace/config."""
    override = os.environ.get("AC_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "accspace" / "config"


def shared_skills_dir():
    """The skill library everyone shares."""
    return config_dir() / "skills"


def personal_skills_dir(user):
    """One user's own skill library."""
    return config_dir() / "users" / user / "skills"


def skills_dirs(user=None):
    """Skill search path: AC_SKILLS_PATH entries first, then the user's own library (when a
    user is given), then the library everyone shares."""
    extra = os.environ.get("AC_SKILLS_PATH", "")
    dirs = [Path(p).expanduser() for p in extra.split(os.pathsep) if p]
    if user:
        dirs.append(personal_skills_dir(user))
    dirs.append(shared_skills_dir())
    return dirs


def config_path():
    return config_dir() / "config.toml"


def settings():
    """Personal settings from config.toml. No file means no settings."""
    try:
        with open(config_path(), "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"{COMMAND}: ignoring {config_path()}: {e}", file=sys.stderr)
        return {}


USER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


def owner():
    """The user who runs acc: the one the terminal commands, and a browser on this machine,
    act as. Known by their login name."""
    return getpass.getuser().lower()


def users():
    """Everyone acc serves: the owner, then each [users.NAME] table in config.toml."""
    extra = settings().get("users")
    names = [owner()] + [str(n).lower() for n in (extra if isinstance(extra, dict) else {})]
    return [n for n in dict.fromkeys(names) if USER_NAME.fullmatch(n)]


def user_settings(user=None):
    """Settings as they apply to one user: config.toml, with that user's [users.NAME] table
    over it. Anyone but the owner is called by their own name rather than the owner's."""
    chosen = {k: v for k, v in settings().items() if k != "users"}
    if user and user != owner():
        chosen["user_name"] = user.capitalize()
        own = (settings().get("users") or {}).get(user)
        chosen.update(own if isinstance(own, dict) else {})
    return chosen


def speaker_names(user=None):
    """What exports call each side of the conversation: user_name and assistant_name in
    config.toml. Display only: the model is never told these names."""
    chosen = user_settings(user)
    return {"user": str(chosen.get("user_name") or "User"),
            "assistant": str(chosen.get("assistant_name") or "Assistant")}


def ollama_host():
    """Base URL of the Ollama server, accepting the same OLLAMA_HOST forms Ollama does."""
    host = os.environ.get("OLLAMA_HOST", "").strip() or "127.0.0.1:11434"
    if "://" not in host:
        host = "http://" + host
    parts = urlsplit(host)
    hostname = parts.hostname or "127.0.0.1"
    if hostname == "0.0.0.0":
        hostname = "127.0.0.1"
    if ":" in hostname:
        hostname = f"[{hostname}]"
    port = parts.port or (443 if parts.scheme == "https" else 11434)
    return f"{parts.scheme}://{hostname}:{port}"


def model_override():
    """A default chosen through the environment, which beats DEFAULT_MODEL."""
    return os.environ.get("AC_MODEL") or None


def debug():
    return os.environ.get("AC_DEBUG", "") not in ("", "0")
