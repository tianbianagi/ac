"""Attaching local files to a message.

There is no file tool for the model to call. When the user's own message names a path that
exists, the client reads it and sends the contents along. Only what the user types can cause
a read; skills and model output never can.
"""

import glob
import os
import re
import subprocess
from collections import Counter, namedtuple
from pathlib import Path

from . import config, pdf
from .store import Attachment

MAX_TEXT_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 200
MAX_ATTACHMENTS = 20
MAX_PDF_IMAGE_PAGES = 8  # a scanned PDF is sent as page images; each costs ~2k tokens
MAX_PATTERN_FILES = 200
MAX_PATTERN_MATCHES = 20_000    # stop expanding a pattern like ~/** instead of walking the disk
DEFAULT_PATTERN_KB = 400        # roughly 100k tokens; max_attach_kb in config.toml changes it
JUNK_DIRS = {"node_modules", "__pycache__", "venv", "env", "dist", "build", "target", "vendor",
             "site-packages"}

# One thing named in a message: a file or directory (path, maybe PDF pages), or a pattern such as
# src/** together with the files it matched.
# label is what to call it when telling the user: "@mom" for a named path.
Ref = namedtuple("Ref", ["path", "pages", "matches", "label"], defaults=[None, None, None])
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# A double-quoted phrase, a single-quoted phrase, or a bare word that may contain "\ ".
_TOKEN = re.compile(r'"([^"]+)"|\'([^\']+)\'|((?:\\.|[^\s\\])+)')
_TRAILING = ".,;:!?)]}>"
_LEADING = "([{<"
_PAGES = re.compile(r"(\d+)(?:-(\d+))?")  # the 7 or 10-20 of report.pdf#10-20


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


def _existing(word):
    try:
        path = Path(word).expanduser()
        return path.resolve() if path.exists() else None
    except (OSError, RuntimeError, ValueError):
        return None


def resolve(word, explicit=False):
    """The Ref for one path or pattern, exactly as written (spaces and all), or None."""
    if not (explicit or _looks_like_path(word)):
        return None
    if "*" in word:
        matches = expand(word)
        return Ref(word, None, matches) if matches else None
    path = _existing(word)
    if path is None and "#" in word:
        base, _, pages = word.rpartition("#")
        if base.lower().endswith(".pdf") and _PAGES.fullmatch(pages):
            path = _existing(base)
            return Ref(path, pages) if path else None
    return Ref(path) if path else None


def find_refs(text, names=None, problems=None):
    """What a message names, as Refs without duplicates.

    names maps the user's own names to paths: @mom then stands for whatever /files named mom.
    A name whose path has gone is reported in problems rather than silently ignored.

    A word counts when it looks like a path (starts with / or ~, or contains a /) or is marked
    with a leading @, which is how to name a bare file in the current directory: @notes.md.
    A PDF can carry a page selection, report.pdf#10-20, which comes back as pages "10-20".
    A word with a * in it is a pattern: src/** is every file under src, docs/**/*.md only the
    markdown. A directory named without one contributes just a listing of itself.
    """
    found, names = [], names or {}
    for word, explicit in _candidates(text):
        if explicit and word.lower() in names:
            refs = named_refs(word.lower(), names, problems)
        else:
            refs = [resolve(word, explicit)]
        for ref in refs:
            if ref is not None and ref._replace(label=None) not in [r._replace(label=None)
                                                                    for r in found]:
                found.append(ref)
    return found


def named_refs(name, names, problems=None):
    """Refs for everything a name stands for, labelled with it. A path that has gone is
    reported in problems rather than silently skipped."""
    refs = []
    for path in names[name]:
        ref = resolve(path, explicit=True)
        if ref is not None:
            refs.append(ref._replace(label=f"@{name}"))
        elif problems is not None:
            line = f"@{name} includes {display_path(path)}, which isn't there now"
            if line not in problems:
                problems.append(line)
    return refs


def find_paths(text):
    return [ref.path for ref in find_refs(text) if ref.matches is None]


def clean_path(text):
    """A path typed after /files: the whole of it is the path, so spaces need no quoting, but
    quotes and the backslashes a terminal adds when a file is dragged in are accepted too."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return re.sub(r"\\(.)", r"\1", text)


def parse_command(text, names):
    """What "/files ARG..." asks for: (entries, name, problems).

    Each entry is (refs, paths to remember). Arguments are paths, patterns, or existing names;
    a last argument written @NAME, after at least one other, is the name to give them all.
    Quoting is optional even for paths with spaces: the longest run of words that names
    something real is taken as one path, so "~/My Notes/a.md ~/b.md" is two paths.
    """
    tokens = list(_TOKEN.finditer(text))
    name = None
    if len(tokens) > 1 and tokens[-1].group(3) and tokens[-1].group(3).startswith("@"):
        name = tokens.pop().group(3)[1:].lower()
    entries, problems, i = [], [], 0
    while i < len(tokens):
        quoted = tokens[i].group(1) or tokens[i].group(2)
        last = i if quoted else next((k - 1 for k in range(i + 1, len(tokens))
                                      if not tokens[k].group(3)), len(tokens) - 1)
        for j in range(last, i - 1, -1):        # the longest reading first
            word = quoted or re.sub(r"\\(.)", r"\1", text[tokens[i].start():tokens[j].end()])
            known = word.lstrip("@").lower()
            if j == i and not quoted and known in names:
                entries.append((named_refs(known, names, problems), names[known]))
                break
            ref = resolve(word, explicit=True)
            if ref is not None:
                entries.append(([ref], [portable(word)]))
                break
        else:
            j = i
            problems.append(f"nothing matches {quoted or tokens[i].group(3)}")
        i = j + 1
    return entries, name, problems


def portable(path):
    """A path or pattern made independent of the current folder, for saving under a name."""
    return os.path.abspath(os.path.expanduser(path))


def expand(pattern):
    """The files a pattern matches, sorted: nothing hidden, no dependency or build folders, and
    nothing that git would ignore.

    Only what the wildcards matched is judged. The part of the pattern that was spelled out is
    taken as meant, so ~/.config/ac/** works even though .config is hidden."""
    pattern = os.path.expanduser(pattern)
    spelled_out = pattern.split("*", 1)[0]
    base = spelled_out if spelled_out.endswith(os.sep) else os.path.dirname(spelled_out)
    matches = []
    try:
        for n, match in enumerate(glob.iglob(pattern, recursive=True)):
            if n >= MAX_PATTERN_MATCHES:
                break
            matched = Path(os.path.relpath(match, base or ".")).parts
            junk = any(part.startswith(".") or part in JUNK_DIRS for part in matched)
            if not junk and os.path.isfile(match):
                matches.append(Path(match).resolve())
    except (OSError, ValueError, re.error):
        return []
    return _drop_git_ignored(sorted(set(matches)))


def _drop_git_ignored(paths):
    """Without the files a .gitignore excludes, when they live in a git repository."""
    if not paths:
        return paths
    try:
        top = subprocess.run(["git", "-C", str(paths[0].parent), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10)
        if top.returncode != 0:
            return paths
        root = Path(top.stdout.strip()).resolve()
        inside = [p for p in paths if p.is_relative_to(root)]
        listed = "\0".join(str(p.relative_to(root)) for p in inside)
        ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-z", "--stdin"],
                                 input=listed, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return paths
    dropped = {root / name for name in ignored.stdout.split("\0") if name}
    return [p for p in paths if p not in dropped]


def size_label(n):
    return _size(n)


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
                raise FileError(f"{display_path(path)} is too large to attach ({_size(size)}; images are "
                                f"limited to {_size(MAX_IMAGE_BYTES)})")
            return Attachment(path=str(path), kind="image", data=path.read_bytes())
        with open(path, "rb") as f:
            raw = f.read(MAX_TEXT_BYTES + 1)
    except OSError as e:
        raise FileError(f"can't read {display_path(path)}: {e.strerror or e}") from None

    truncated = len(raw) > MAX_TEXT_BYTES
    raw = raw[:MAX_TEXT_BYTES]
    try:
        if b"\0" in raw:
            raise ValueError
        # A cut can land inside a multi-byte character; only then is a decode error forgivable.
        content = raw.decode("utf-8", errors="ignore" if truncated else "strict")
    except ValueError:
        raise FileError(f"{display_path(path)} isn't text, a PDF or an image, so it can't be attached") from None
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


def _page_range(numbers):
    first, last = numbers[0], numbers[-1]
    return f"page {first}" if first == last else f"pages {first}-{last}"


def read_pdf(path, pages=None):
    """Attachments for a PDF, plus lines the user should see about what was left out.

    Normally that is one text attachment with [page N] markers. A PDF with no text layer (a
    scan) is sent as images of its pages instead, where the system can render them.
    """
    try:
        texts = pdf.page_texts(path)
    except pdf.PdfError as e:
        raise FileError(f"can't read {display_path(path)}: {e}") from None
    total = len(texts)
    numbers = list(range(1, total + 1))
    if pages:
        first, last = _PAGES.fullmatch(pages).groups()
        numbers = [n for n in numbers if int(first) <= n <= int(last or first)]
        if not numbers:
            raise FileError(f"{display_path(path)} has {total} pages; there is no {_page_range([int(first), int(last or first)])}")
    if not numbers:
        raise FileError(f"{display_path(path)} has no pages")

    if sum(len(texts[n - 1].strip()) for n in numbers) < 10 * len(numbers):
        return _pdf_as_images(path, numbers, total)

    kept, used = [], 0
    for n in numbers:
        block = f"[page {n}]\n{texts[n - 1].strip()}"
        size = len(block.encode()) + 2
        if kept and used + size > MAX_TEXT_BYTES:
            break
        if not kept and size > MAX_TEXT_BYTES:  # one enormous page: keep what fits
            block = block.encode()[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
        kept.append((n, block))
        used += size
    sent = [n for n, _ in kept]
    whole = sent == list(range(1, total + 1))
    note = f"PDF, {total} pages" if whole else f"PDF, {_page_range(sent)} of {total}"
    notes = []
    if len(sent) < len(numbers):
        notes.append(f"{path.name} is long: sent {_page_range(sent)} of {total}. To read on, "
                     f"name {path.name}#{sent[-1] + 1}-{numbers[-1]}")
    text = "\n\n".join(block for _, block in kept)
    return [Attachment(path=str(path), kind="text", content=text, note=note)], notes


def _pdf_as_images(path, numbers, total):
    if not pdf.can_render():
        raise FileError(f"{display_path(path)} has no text layer (a scan?), and its pages can't be turned "
                        "into images on this system")
    sent = numbers[:MAX_PDF_IMAGE_PAGES]
    try:
        rendered = pdf.render_pages(path, sent)
    except pdf.PdfError as e:
        raise FileError(f"can't read {display_path(path)}: {e}") from None
    notes = [f"{path.name} has no text layer (a scan?), so {_page_range(sent)} of {total} "
             f"went as {'an image' if len(sent) == 1 else 'images'}"]
    if len(sent) < len(numbers):
        notes[0] += f". For more, name {path.name}#{sent[-1] + 1}-{numbers[-1]}"
    return [Attachment(path=f"{path}#{n}", kind="image", data=data) for n, data in rendered], notes


def read_all(path, pages=None):
    """Everything one named path contributes: (attachments, lines for the user)."""
    if Path(path).suffix.lower() == ".pdf" and Path(path).is_file():
        return read_pdf(Path(path), pages)
    return [read(path)], []


def read_pattern(pattern, paths, already=()):
    """Attachments for the files a pattern matched, within a budget, plus what was left out."""
    budget = int(config.settings().get("max_attach_kb", DEFAULT_PATTERN_KB)) * 1024
    attachments, skipped, used = [], Counter(), 0
    taken = {a.path for a in already}
    for n, path in enumerate(paths):
        if str(path) in taken:
            continue
        if len(attachments) >= MAX_PATTERN_FILES or used >= budget:
            left = len(paths) - n
            limit = (f"{MAX_PATTERN_FILES} files" if len(attachments) >= MAX_PATTERN_FILES
                     else _size(budget))
            skipped[f"past the {limit} limit (a narrower pattern, such as **/*.py, fits more "
                    f"of what matters)"] += left
            break
        if path.suffix.lower() in IMAGE_SUFFIXES:
            skipped["images (name one directly to send it)"] += 1
            continue
        try:
            got, _ = read_all(path)
        except FileError:
            skipped["not text"] += 1
            continue
        got = [a for a in got if a.kind == "text"]      # a scanned PDF would come as page images
        if not got:
            skipped["scanned PDFs"] += 1
            continue
        if used + got[0].size > budget and attachments:
            skipped[f"past the {_size(budget)} limit (a narrower pattern, such as **/*.py, fits "
                    f"more of what matters)"] += len(paths) - n
            break
        got[0].group = pattern
        attachments.append(got[0])
        used += got[0].size
    notes = [f"{pattern}: left out {count} {reason}" for reason, count in skipped.items()]
    return attachments, notes


def read_refs(refs, already=()):
    """Attachments for Refs, skipping files in `already`, plus lines the user should see."""
    attachments, problems = [], []
    for ref in refs:
        try:
            if ref.matches is not None:
                got, notes = read_pattern(ref.label or ref.path, ref.matches,
                                          list(already) + attachments)
            else:
                got, notes = read_all(ref.path, ref.pages)
                # The note tells report.pdf#2-3 from report.pdf#5: same path, different pages.
                taken = {(a.path, a.note) for a in list(already) + attachments}
                got = [a for a in got if (a.path, a.note) not in taken]
                for a in got:
                    a.group = ref.label         # a named file is announced under its name
            attachments += got
            problems += notes
        except FileError as e:
            problems.append(str(e))
    return attachments, problems


def collect(text, names=None, already=()):
    """Attachments for everything named in a message, plus lines the user should see."""
    problems = []
    refs = find_refs(text, names, problems)
    if len(refs) > MAX_ATTACHMENTS:
        problems.append(f"only the first {MAX_ATTACHMENTS} of {len(refs)} paths were attached")
    attachments, notes = read_refs(refs[:MAX_ATTACHMENTS], already)
    return attachments, problems + notes


def announce(attachments, verb="attached"):
    """Lines telling the user what was attached: one per file, or one per pattern."""
    lines, groups = [], {}
    for a in attachments:
        if a.group:
            groups.setdefault(a.group, []).append(a)
        else:
            lines.append(f"{verb} {describe(a)}")
    for pattern, items in groups.items():
        if len(items) == 1:
            lines.append(f"{verb} {describe(items[0])}")
            continue
        lines.append(f"{verb} {len(items)} files from {pattern} "
                     f"({_size(sum(a.size for a in items))})"
                     + ("; /files lists them" if verb == "attached" else ""))
    return lines


def summarize(attachments, limit=5):
    """describe() for each attachment, or for the first few when a pattern brought in many."""
    if len(attachments) <= limit:
        return [describe(a) for a in attachments]
    rest = attachments[limit - 2:]
    return [describe(a) for a in attachments[:limit - 2]] + [
        f"and {len(rest)} more files ({_size(sum(a.size for a in rest))})"]


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
