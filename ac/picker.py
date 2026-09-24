"""An arrow-key list picker for the terminal: type to filter, Enter to choose (stdlib only).

Picker holds the state and draws lines, and knows nothing about terminals, so it can be tested
directly. pick() is the thin part that reads keys and repaints.
"""

import codecs
import os
import select
import shutil
import sys

from .markdown import _char_width

try:
    import termios
    import tty
except ImportError:  # not a Unix terminal: callers fall back to a numbered list
    termios = tty = None

BOLD_CYAN, BOLD_RED, DIM, RESET = "\033[1;36m", "\033[1;31m", "\033[2m", "\033[0m"


def available(stream_in=None, stream_out=None):
    stream_in, stream_out = stream_in or sys.stdin, stream_out or sys.stdout
    return termios is not None and stream_in.isatty() and stream_out.isatty()


def _clip(text, width):
    """text cut to a display width, so a row never wraps and the repaint stays aligned."""
    used, out = 0, []
    for ch in text:
        used += _char_width(ch)
        if used > width:
            return "".join(out[:-1]) + "…" if out else ""
        out.append(ch)
    return "".join(out)


class Picker:
    def __init__(self, items, label, *, title="", search=None, key=None, height=10, query="",
                 start=0, current=None, delete=None, protect=None, wording=None,
                 delete_label="delete", toggle=None, toggle_label="toggles", marked=None,
                 opens=None):
        """label(item) -> (text, detail). search(query) -> items found some other way (say,
        by message text), matched to `items` by key(item). query pre-fills the filter; start
        is the row the cursor begins on. current is the key of the item in use now: its row
        carries a •, in a column of its own so that it shows under the cursor too.

        delete(item) makes rows deletable (Ctrl-D, then y to confirm). protect(item) returns
        the reason an item may not be deleted, or None. wording(item) returns (question, done)
        to use instead of "Delete ...?" and "deleted ...", for lists where deleting a row does
        not destroy the thing it names; delete_label is the word for it in the key hints.

        toggle(item) makes this a list of things to switch on and off rather than a choice of
        one: Enter calls it and the list stays open, until Esc. It returns a line saying what
        it did; toggle_label says what Enter does in the key hints. marked(item) then decides
        which rows carry the •, since several can be on at once. opens(item) picks out the rows
        that Enter chooses even so, closing the list: a folder to go into, say."""
        self.items, self.label, self.title = list(items), label, title
        self.search, self.key, self.height = search, key or id, height
        self.current, self.delete, self.protect = current, delete, protect
        self.wording, self.delete_label = wording, delete_label
        self.toggle, self.toggle_label, self.marked = toggle, toggle_label, marked
        self.opens = opens
        self.confirming = None      # the item whose deletion is waiting for a "y"
        self.message = ""           # one line about what the last key did
        self.query, self.index, self.top = query, 0, 0
        self.matches, self.deep = list(self.items), set()
        if query:
            self._refilter()
        elif self.items:
            self.index = max(0, min(start, len(self.items) - 1))
            self.top = max(0, self.index - height + 1)

    @property
    def selected(self):
        return self.matches[self.index] if self.matches else None

    def _refilter(self):
        words = self.query.lower().split()
        found = set()
        if self.search and len(self.query.strip()) >= 3:
            found = {self.key(item) for item in self.search(self.query.strip())}

        def by_label(item):
            haystack = " ".join(self.label(item)).lower()
            return all(word in haystack for word in words)

        self.matches = [i for i in self.items if by_label(i) or self.key(i) in found]
        self.deep = {self.key(i) for i in self.matches if not by_label(i)}
        self.index = self.top = 0

    def handle(self, key):
        """Apply one key. Returns "accept" or "cancel" when the picker is done."""
        if self.confirming is not None:
            return self._answer(key)
        self.message = ""
        if key in ("ctrl-d", "delete"):
            if self.delete is None:
                return "cancel" if key == "ctrl-d" else None
            reason = self.protect(self.selected) if self.protect and self.selected else None
            if reason:
                self.message = reason
            else:
                self.confirming = self.selected
            return None
        if key == "enter":
            if self.opens and self.matches and self.opens(self.selected):
                return "accept"
            if self.toggle and self.matches:
                self.message = self.toggle(self.selected) or ""
                return None
            return "accept" if self.matches else None
        if key == "esc":
            return "cancel"
        if key in ("up", "down", "pageup", "pagedown") and self.matches:
            step = {"up": -1, "down": 1, "pageup": -self.height, "pagedown": self.height}[key]
            self.index = max(0, min(len(self.matches) - 1, self.index + step))
        elif key in ("backspace", "clear"):
            self.query = self.query[:-1] if key == "backspace" else ""
            self._refilter()
        elif isinstance(key, str) and len(key) == 1 and key.isprintable():
            self.query += key
            self._refilter()
        self.top = min(max(self.top, self.index - self.height + 1), self.index)
        return None

    def _answer(self, key):
        """The reply to "delete this?". Only a y deletes; any other key is a no."""
        item, self.confirming = self.confirming, None
        if key not in ("y", "Y"):
            self.message = "kept."
            return None
        done = self.wording(item)[1] if self.wording else f"deleted “{self.label(item)[0]}”"
        self.delete(item)
        self.items = [i for i in self.items if self.key(i) != self.key(item)]
        index, query = self.index, self.query
        self._refilter()            # with the same filter, and the cursor where it was
        self.query, self.index = query, max(0, min(index, len(self.matches) - 1))
        self.top = max(0, self.index - self.height + 1)
        self.message = done
        return None

    def _marked(self, item):
        if self.marked is not None:
            return self.marked(item)
        return self.current is not None and self.key(item) == self.current

    def lines(self, width):
        width = max(width - 1, 20)
        count = f"{len(self.matches)} of {len(self.items)}" if self.query else f"{len(self.items)}"
        keys = ("type to filter · ↑↓ · Enter" + f" {self.toggle_label}" * bool(self.toggle)
                + f" · Ctrl-D {self.delete_label}" * bool(self.delete) + " · Esc")
        head = f"{DIM}{_clip(f'{self.title} ({count}) · {keys}', width)}{RESET}"
        if self.confirming is not None:
            name = self.label(self.confirming)[0]
            question = (self.wording(self.confirming)[0] if self.wording
                        else f"Delete “{name}”? y deletes it for good")
            head = f"{BOLD_RED}{_clip(f'{question} · any other key keeps it', width)}{RESET}"
        elif self.message:
            head = f"{DIM}{_clip(self.message, width)}{RESET}"
        lines = [head, _clip(f"› {self.query}", width)]
        shown, marks = [], []
        for item in self.matches[self.top:self.top + self.height]:
            text, detail = self.label(item)
            shown.append((text, detail + " · matched in messages" * (self.key(item) in self.deep)))
            marks.append("•" if self._marked(item) else " ")
        # One detail column for all rows, so titles and details each line up. A row is
        # "❯ " + "• " (in lists that mark rows) + title (room) + "  " + detail (wide): it must
        # never exceed width, because a row that wraps throws off the repaint, which counts lines.
        marking = self.current is not None or self.marked is not None
        fixed = 6 if marking else 4
        widest = max((sum(_char_width(c) for c in d) for _, d in shown), default=0)
        room = max(width - fixed - min(widest, width // 2), 10)
        wide = max(width - fixed - room, 0)
        for n, (text, detail) in enumerate(shown, self.top):
            chosen = n == self.index
            row = _clip(text, room)
            pad = " " * max(room - sum(_char_width(c) for c in row), 0)
            mark = f"{marks[n - self.top]} " * marking
            lines.append(f"{BOLD_CYAN if chosen else ''}{'❯' if chosen else ' '} {mark}{row}{RESET}"
                         f"{pad}  {DIM}{_clip(detail, wide)}{RESET}")
        if not self.matches:
            lines.append(f"{DIM}  nothing matches{RESET}")
        hidden = len(self.matches) - self.top - self.height
        if hidden > 0:
            lines.append(f"{DIM}  … {hidden} more{RESET}")
        return lines


_ESCAPES = {b"[A": "up", b"OA": "up", b"[B": "down", b"OB": "down",
            b"[5~": "pageup", b"[6~": "pagedown", b"[3~": "delete"}
_CONTROLS = {b"\r": "enter", b"\n": "enter", b"\x7f": "backspace", b"\x08": "backspace",
             b"\x15": "clear", b"\x03": "esc", b"\x04": "ctrl-d", b"\x10": "up", b"\x0e": "down",
             b"\t": "down"}


def _read_key(fd, decoder):
    byte = os.read(fd, 1)
    if not byte:
        return "esc"                                    # end of input
    if byte == b"\x1b":
        if not select.select([fd], [], [], 0.05)[0]:
            return "esc"                                # a bare Escape, not an arrow key
        # Read exactly one sequence: a held-down arrow key delivers several in one burst.
        sequence = os.read(fd, 1)
        while sequence in (b"[", b"O") or (sequence[:1] == b"[" and not 0x40 <= sequence[-1] <= 0x7e):
            more = os.read(fd, 1)
            if not more:
                break
            sequence += more
        return _ESCAPES.get(sequence)
    if byte in _CONTROLS:
        return _CONTROLS[byte]
    return decoder.decode(byte) or None                 # None until a multi-byte character is whole


def pick(items, label, *, out=None, fd=None, **options):
    """Let the user choose one of items. Returns it, or None if they cancelled. The options
    are Picker's."""
    out = out or sys.stdout
    fd = sys.stdin.fileno() if fd is None else fd
    picker = Picker(items, label, **options)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
    saved = termios.tcgetattr(fd)
    drawn, result = 0, "cancel"
    try:
        tty.setcbreak(fd)
        # Hide the cursor, and turn line-wrapping off: a row that were ever too long is then cut
        # short by the terminal instead of wrapping, which would break the repaint below.
        out.write("\033[?25l\033[?7l")
        while True:
            lines = picker.lines(shutil.get_terminal_size((80, 24)).columns)
            out.write((f"\033[{drawn}A" if drawn else "") + "\r\033[J" + "\n".join(lines) + "\n")
            out.flush()
            drawn = len(lines)
            result = picker.handle(_read_key(fd, decoder))
            if result:
                break
    except KeyboardInterrupt:
        result = "cancel"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        out.write((f"\033[{drawn}A" if drawn else "") + "\r\033[J\033[?7h\033[?25h")  # no trace
        out.flush()
    return picker.selected if result == "accept" else None
