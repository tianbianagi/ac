"""Paths, environment variables and defaults."""

import os
from pathlib import Path
from urllib.parse import urlsplit

APP = "ac"
DEFAULT_MODEL = "qwen3.8:27b"


def _xdg(var, fallback):
    base = os.environ.get(var)
    return (Path(base) if base else Path.home() / fallback) / APP


def db_path():
    override = os.environ.get("AC_DB")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_DATA_HOME", ".local/share") / "ac.db"


def skills_dirs():
    """Skill search path: AC_SKILLS_PATH entries first, then the user library."""
    extra = os.environ.get("AC_SKILLS_PATH", "")
    dirs = [Path(p).expanduser() for p in extra.split(os.pathsep) if p]
    dirs.append(_xdg("XDG_CONFIG_HOME", ".config") / "skills")
    return dirs


def history_path():
    return _xdg("XDG_STATE_HOME", ".local/state") / "history"


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
