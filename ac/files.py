"""Reading the files that come with a message.

A file reaches the model only as part of a message: the page sends it along, the server reads
it once, and its contents are kept with that message. There is no file tool for the model to
call, and nothing names a path on this machine: not a message, not a skill, not the model's
output.
"""

import os
from pathlib import Path

from . import pdf
from .store import Attachment

MAX_TEXT_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PDF_IMAGE_PAGES = 8  # a scanned PDF is sent as page images; each costs ~2k tokens
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class FileError(Exception):
    pass


def _size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def read(path):
    """Snapshot one file as an Attachment. Raises FileError when it can't be attached."""
    path = Path(path)
    try:
        if not path.is_file():
            raise FileError(f"{path.name} isn't a file")
        size = path.stat().st_size
        if path.suffix.lower() in IMAGE_SUFFIXES:
            if size > MAX_IMAGE_BYTES:
                raise FileError(f"{path.name} is too large to attach ({_size(size)}; images are "
                                f"limited to {_size(MAX_IMAGE_BYTES)})")
            return Attachment(path=str(path), kind="image", data=path.read_bytes())
        with open(path, "rb") as f:
            raw = f.read(MAX_TEXT_BYTES + 1)
    except OSError as e:
        raise FileError(f"can't read {path.name}: {e.strerror or e}") from None

    truncated = len(raw) > MAX_TEXT_BYTES
    raw = raw[:MAX_TEXT_BYTES]
    try:
        if b"\0" in raw:
            raise ValueError
        # A cut can land inside a multi-byte character; only then is a decode error forgivable.
        content = raw.decode("utf-8", errors="ignore" if truncated else "strict")
    except ValueError:
        raise FileError(f"{path.name} isn't text, a PDF or an image, so it can't be attached") from None
    note = f"first {_size(len(raw))} of {_size(size)}" if truncated else None
    return Attachment(path=str(path), kind="text", content=content, note=note)


def _page_range(numbers):
    first, last = numbers[0], numbers[-1]
    return f"page {first}" if first == last else f"pages {first}-{last}"


def read_pdf(path):
    """Attachments for a PDF, plus lines the user should see about what was left out.

    Normally that is one text attachment with [page N] markers. A PDF with no text layer (a
    scan) is sent as images of its pages instead, where the system can render them.
    """
    try:
        texts = pdf.page_texts(path)
    except pdf.PdfError as e:
        raise FileError(f"can't read {path.name}: {e}") from None
    total = len(texts)
    numbers = list(range(1, total + 1))
    if not numbers:
        raise FileError(f"{path.name} has no pages")

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
    whole = sent == numbers
    note = f"PDF, {total} pages" if whole else f"PDF, {_page_range(sent)} of {total}"
    notes = [] if whole else [f"{path.name} is long: sent {_page_range(sent)} of {total}"]
    text = "\n\n".join(block for _, block in kept)
    return [Attachment(path=str(path), kind="text", content=text, note=note)], notes


def _pdf_as_images(path, numbers, total):
    if not pdf.can_render():
        raise FileError(f"{path.name} has no text layer (a scan?), and its pages can't be turned "
                        "into images on this system")
    sent = numbers[:MAX_PDF_IMAGE_PAGES]
    try:
        rendered = pdf.render_pages(path, sent)
    except pdf.PdfError as e:
        raise FileError(f"can't read {path.name}: {e}") from None
    notes = [f"{path.name} has no text layer (a scan?), so {_page_range(sent)} of {total} "
             f"went as {'an image' if len(sent) == 1 else 'images'}"]
    return [Attachment(path=f"{path}#{n}", kind="image", data=data) for n, data in rendered], notes


def read_all(path):
    """Everything one file contributes: (attachments, lines for the user)."""
    path = Path(path)
    if path.suffix.lower() == ".pdf" and path.is_file():
        return read_pdf(path)
    return [read(path)], []


def announce(attachments, verb="attached"):
    """Lines telling the user what was attached, one per file."""
    return [f"{verb} {describe(a)}" for a in attachments]


def summarize(attachments, limit=5):
    """describe() for each attachment, or for the first few when there are many."""
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
    """One line for the user: what it is and how big. A file that came with a message is known
    by its name; one attached by path, from before, by where it was."""
    where = display_path(attachment.path) if Path(attachment.path).is_absolute() else attachment.path
    detail = f"{attachment.kind}, {_size(attachment.size)}"
    note = f", {attachment.note}" if attachment.note else ""
    return f"{where} ({detail}{note})"


def for_model(attachment):
    """How an attachment appears in the text sent to the model."""
    note = f' note="{attachment.note}"' if attachment.note else ""
    if attachment.kind == "image":
        return f'<image path="{attachment.path}"/>'
    tag = "directory" if attachment.kind == "directory" else "file"
    return f'<{tag} path="{attachment.path}"{note}>\n{attachment.content}\n</{tag}>'
