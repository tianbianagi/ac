"""Render markdown for a terminal while it is still streaming in.

Text is styled as it arrives, and nothing already printed is ever rewritten, so there are no
cursor tricks to go wrong. What can't be interpreted yet is held back until it can be:

- a word, so that lines can be wrapped between words;
- the first characters of a line (is "-" a bullet, a rule or a minus sign?);
- an emphasis, code or link span until it closes. One that never closes is printed literally,
  so `*args, **kwargs` or a stray backtick can't restyle the rest of a line;
- a table until it ends, because column widths depend on every row.

Fenced code is printed exactly as written and never wrapped, so copying it out is safe.
"""

import io
import re
import shutil
import string
import textwrap
import unicodedata

RESET = "\033[0m"
BOLD, DIM, ITALIC, UNDERLINE, STRIKE, CYAN = 1, 2, 3, 4, 9, 36
MAX_SPAN = 600  # characters to wait for a span to close before giving up on it
_ZERO_WIDTH = "\u200d\ufe0f"  # zero-width joiner, emoji variation selector


def _sgr(codes):
    return f"\033[{';'.join(str(c) for c in codes)}m" if codes else ""


def _char_width(ch):
    if unicodedata.combining(ch) or ch in _ZERO_WIDTH:
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def _width(text):
    return sum(_char_width(ch) for ch in text)


def _run(text, ch):
    """Length of the run of `ch` that `text` starts with."""
    return len(text) - len(text.lstrip(ch))


def _plain(text):
    """Inline markdown with the markup removed, for places that are laid out as plain text."""
    text = re.sub(r"<br\s*/?>", " ", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\*\*|__|~~|`", "", text)
    text = re.sub(r"(?<![\w*])[*_](?=\S)|(?<=\S)[*_](?![\w*])", "", text)
    return re.sub(r"\\([" + re.escape(string.punctuation) + "])", r"\1", text).strip()


class _Writer:
    """Buffers styled output and wraps it between words, with a prefix on continuation lines."""

    def __init__(self, width):
        self.width = width          # callable: the terminal's current width
        self.buf = []
        self.active = ""            # the SGR sequence in effect on the terminal right now
        self.col = 0
        self.indent = 0             # where text starts on this line: nothing to wrap before it
        self.cont = ("", "")        # (text, sgr) that starts each continuation line
        self.word, self.word_width, self.spaces = [], 0, []
        self.at_line_start = True

    def _style(self, sgr):
        if sgr != self.active:
            if self.active:
                self.buf.append(RESET)
            self.buf.append(sgr)
            self.active = sgr

    def _put(self, text, sgr):
        if text:
            self._style(sgr)
            self.buf.append(text)
            self.col += _width(text)
            self.at_line_start = False

    def begin(self, first=("", ""), cont=("", "")):
        self._put(*first)
        self.indent, self.cont = self.col, cont

    def text(self, ch, sgr):
        if ch in " \t":
            self._flush_word()
            self.spaces.append(sgr)
        elif _char_width(ch) == 2:  # CJK and the like have no spaces: any character may wrap
            self._flush_word()
            self.word, self.word_width = [(ch, sgr)], 2
            self._flush_word()
        else:
            self.word.append((ch, sgr))
            self.word_width += _char_width(ch)

    def _flush_word(self):
        if not self.word:
            return
        if self.col > self.indent and self.col + len(self.spaces) + self.word_width > self.width():
            self._style("")
            self.buf.append("\n")
            self.col = 0
            self._put(*self.cont)
            self.indent = self.col
            self.spaces = []        # a line doesn't start with the space that ended the last one
        for sgr in self.spaces:
            self._put(" ", sgr)
        for ch, sgr in self.word:
            self._style(sgr)
            self.buf.append(ch)
        self.col += self.word_width
        self.at_line_start = False
        self.word, self.word_width, self.spaces = [], 0, []

    def raw(self, text, sgr=""):
        """Unwrapped output, for code and ready-made lines."""
        self._style(sgr)
        self.buf.append(text)
        self.at_line_start = False

    def newline(self):
        self._flush_word()
        self.spaces = []
        self._style("")
        self.buf.append("\n")
        self.col, self.indent, self.cont, self.at_line_start = 0, 0, ("", ""), True

    def take(self):
        out, self.buf = "".join(self.buf), []
        return out


class MarkdownStream:
    def __init__(self, out, width=None):
        self.out = out
        self._fixed_width = width
        self.w = _Writer(self._width)
        self.state = "start"        # start | inline | code_start | code
        self.head = ""              # the undecided beginning of the current line
        self.fence = None           # (character, length) of the open code fence
        self.table = []
        self._reset_inline()

    def _width(self):
        return self._fixed_width or max(shutil.get_terminal_size((80, 24)).columns, 20)

    # -- driving ----------------------------------------------------------

    def feed(self, text):
        for ch in text:
            if ch != "\r":
                getattr(self, "_" + self.state)(ch)
        self._write()

    def finish(self):
        """Flush whatever is still held back and leave the cursor at the start of a line."""
        if self.state == "start":
            if self.head:
                self._line_end_undecided()
        elif self.state == "inline":
            self._end_line()
        else:
            if self.head:
                self.w.raw(self.head, _sgr([CYAN]))
            if not self.w.at_line_start:
                self.w.newline()
        self._flush_table()
        self.w._style("")
        self.state, self.head, self.fence = "start", "", None
        self._write()

    def _write(self):
        text = self.w.take()
        if text:
            self.out.write(text)
            self.out.flush()

    # -- the start of a line ----------------------------------------------

    def _start(self, ch):
        if ch == "\n":
            return self._line_end_undecided()
        self.head += ch
        text = self.head.lstrip(" ")
        if not text:
            return
        indent = self.head[:len(self.head) - len(text)]
        first = text[0]
        if self.table and first != "|":
            self._flush_table()
        if first == "|":
            return                                   # a table row: wait for the whole line
        if first in "`~":
            run = _run(text, first)
            if run == len(text) or run >= 3:
                return                               # perhaps a code fence: wait for the line
        elif first == "#":
            run = _run(text, "#")
            if run == len(text) and run <= 6:
                return
            if run <= 6 and run < len(text) and text[run] == " ":
                codes = [BOLD, UNDERLINE] if run == 1 else [BOLD]
                return self._begin(text[run + 1:], base=codes)
        elif first == ">":
            if len(text) == 1:
                return
            bar = ("│ ", _sgr([DIM]))
            return self._begin(text[2:] if text[1] == " " else text[1:], base=[ITALIC],
                               first=bar, cont=bar)
        elif first in "-*+_":
            run = _run(text, first)
            if run == len(text):
                return                               # a bullet, a rule, or emphasis: not known yet
            if run == 1 and first != "_" and text[1] == " ":
                if set(text) <= {first, " "}:
                    return                           # "- " so far: a bullet, or a rule "- - -"
                return self._begin(text[2:], first=(indent + "• ", ""),
                                   cont=(indent + "  ", ""), marker=True)
        elif first.isdigit():
            digits = re.match(r"\d{1,9}", text).group()
            rest = text[len(digits):]
            if rest in ("", ".", ")"):
                return
            if rest[0] in ".)" and rest[1] == " ":
                marker = f"{indent}{digits}{rest[0]} "
                return self._begin(rest[2:], first=(marker, ""), cont=(" " * len(marker), ""),
                                   marker=True)
        self._begin(text, first=(indent, ""), cont=(indent, ""))

    def _begin(self, rest, base=(), first=("", ""), cont=("", ""), marker=False):
        """The line's kind is known: print its prefix and treat the rest as inline text."""
        self.base, self.after_marker = list(base), marker
        self.skip_spaces = marker   # "-   item": extra spaces after a marker aren't content
        self.w.begin(first, cont)
        self.state, self.head = "inline", ""
        for ch in rest:
            self._inline(ch)

    def _line_end_undecided(self):
        """A newline arrived while the line's kind was still open, so the whole line is here."""
        line, self.head = self.head, ""
        text = line.strip()
        if not text:
            self._flush_table()
            return self.w.newline()
        if text[0] == "|":
            return self.table.append(text)
        if text[0] in "`~" and _run(text, text[0]) >= 3 and not (
                text[0] == "`" and "`" in text.lstrip("`")):
            self.fence = (text[0], _run(text, text[0]))
            self.w.raw(line, _sgr([DIM]))
            self.w.newline()
            self.state = "code_start"
            return
        if text[0] in "-*_" and len(text.replace(" ", "")) >= 3 and set(text) <= {text[0], " "}:
            self.w.raw("─" * self._width(), _sgr([DIM]))
            return self.w.newline()
        indent = line[:len(line) - len(line.lstrip(" "))]
        self._begin(line.strip(), first=(indent, ""), cont=(indent, ""))
        self._end_line()

    # -- inline text ------------------------------------------------------

    def _reset_inline(self):
        self.base = []              # SGR codes of the line itself (heading, quote)
        self.styles = []            # SGR codes of the closed spans being printed
        self.run = ""               # a run of delimiter characters whose meaning isn't known yet
        self.escape = False
        self.span = None            # the span being held back until it closes
        self.prev = None            # last visible character, for the flanking rules
        self.after_marker = False   # nothing printed yet after a list marker
        self.skip_spaces = False

    def _emit(self, text, extra=()):
        sgr = _sgr(self.base + [c for codes in self.styles for c in codes] + list(extra))
        for ch in text:
            self.w.text(ch, sgr)
        self.prev, self.after_marker = text[-1], False

    def _inline(self, ch):
        if self.skip_spaces:
            if ch in " \t":
                return
            self.skip_spaces = False
        if self.span is not None:
            return self._capture(ch)
        if self.run:
            if ch == self.run[0]:
                self.run += ch
                return
            run, self.run = self.run, ""
            self._opener(run, ch)
            return self._inline(ch)
        if ch == "\n":
            return self._end_line()
        if self.escape:
            self.escape = False
            if ch in string.punctuation:
                return self._emit(ch)
            self._emit("\\")
        if ch == "\\":
            self.escape = True
        elif ch in "*_~`":
            self.run = ch
        elif ch == "[":
            self.span = {"kind": "link", "delim": "[", "buf": "", "url": None, "closed": False}
        else:
            self._emit(ch)

    def _opener(self, run, following):
        """A run of * _ ~ or ` has ended. Start holding a span back, or print the run as it is."""
        ch, n = run[0], len(run)
        opens = following is not None and not following.isspace()
        if ch == "`":
            kind = "code"
        else:
            kind = "emphasis"
            ascii_word = self.prev is not None and self.prev.isascii() and self.prev.isalnum()
            if ascii_word and (ch == "_" or (following or " ").isalnum()):
                opens = False       # snake_case, 5*3: not emphasis. (Not applied to CJK text.)
            if n > 3 or (ch == "~" and n != 2):
                opens = False
        if not opens:
            return self._emit(run)
        self.span = {"kind": kind, "delim": run, "buf": "", "run": 0, "skip": False,
                     "in_code": False}

    def _capture(self, ch):
        span = self.span
        if span["kind"] == "link":
            return self._capture_link(ch)
        mark, n = span["delim"][0], len(span["delim"])
        if span["run"]:
            if ch == mark:
                span["run"] += 1
                return
            if self._end_run(span, ch):
                return self._inline(ch)
        if ch == "\n" or len(span["buf"]) > MAX_SPAN:
            self._abandon_span()
            return self._inline(ch)
        if span["skip"]:
            span["skip"] = False
        elif ch == "\\" and span["kind"] != "code":
            span["skip"] = True                     # an escaped delimiter doesn't close anything
        elif ch == "`" and span["kind"] != "code":
            span["in_code"] = not span["in_code"]   # nor does one inside a code span
        elif ch == mark and not span["in_code"]:
            span["run"] = 1
            return
        span["buf"] += ch

    def _end_run(self, span, following):
        """A run of the delimiter inside a held span has ended. True if that finished the span."""
        mark, n = span["delim"][0], len(span["delim"])
        run, span["run"] = span["run"], 0
        verdict = self._closes(span, run, following)
        if verdict:
            span["buf"] += mark * (run - n)         # "**bold *it***": the extra belongs inside
            self._close_span()
            return True
        span["buf"] += mark * run
        if verdict is None:
            self._abandon_span()
            return True
        return False

    def _closes(self, span, run, following):
        """True: this run closes the span. False: it doesn't. None: this was never a span."""
        mark, n, buf = span["delim"][0], len(span["delim"]), span["buf"]
        if span["kind"] == "code":
            return run == n
        if run < n or not buf or buf[-1].isspace():
            return False
        if mark == "_":
            if following is not None and following.isascii() and following.isalnum():
                return False
            if n == 2 and re.fullmatch(r"\w+", buf):
                return None         # __init__ and __name__ are names, not bold text
        return True

    def _close_span(self):
        span, self.span = self.span, None
        if span["kind"] == "code":
            text = span["buf"]
            if len(text) > 2 and text[0] == text[-1] == " " and text.strip():
                text = text[1:-1]   # "`` `x` ``" pads a span that itself contains backticks
            return self._emit(text, extra=[CYAN]) if text else None
        codes = {"~": [STRIKE]}.get(span["delim"][0]) or {1: [ITALIC], 2: [BOLD],
                                                           3: [BOLD, ITALIC]}[len(span["delim"])]
        self._replay(span["buf"], codes)

    def _replay(self, text, codes=()):
        """Print held text. It is complete, so anything inside it that opens must also close."""
        self.styles.append(list(codes))
        for ch in text:
            self._inline(ch)
        self._settle()
        self.styles.pop()

    def _settle(self):
        """Nothing more is coming for what is pending: print it as it stands."""
        while self.span is not None or self.run or self.escape:
            if self.span is not None:
                held = self.span
                if held["kind"] != "link" and held["run"] and self._end_run(held, None):
                    continue        # the text ended on the span's closing delimiter
                self._abandon_span()
            if self.run and self.span is None:
                run, self.run = self.run, ""
                self._opener(run, None)
            if self.escape and self.span is None and not self.run:
                self.escape = False
                self._emit("\\")

    def _abandon_span(self):
        """The span never closed: its delimiter was just a character, and so is what followed."""
        span, self.span = self.span, None
        if span["kind"] == "link":
            return self._abandon_link(span)
        self._emit(span["delim"])
        text = span["buf"] + span["delim"][0] * span["run"]
        for ch in text:             # not settled: the line may still be streaming in
            self._inline(ch)

    def _capture_link(self, ch):
        span = self.span
        if span["url"] is not None:
            if ch == ")":
                self.span = None
                return self._print_link(span)
            if ch in " \n" or len(span["url"]) > 2000:
                self._abandon_span()
                return self._inline(ch)
            span["url"] += ch
        elif span["closed"]:
            if ch == "(":
                span["url"] = ""
                return
            self._abandon_span()
            self._inline(ch)
        elif ch == "]":
            span["closed"] = True
        elif ch == "\n" or len(span["buf"]) > MAX_SPAN:
            self._abandon_span()
            self._inline(ch)
        else:
            span["buf"] += ch

    def _print_link(self, span):
        url = span["url"].strip()
        text = _plain(span["buf"]) or url
        if not text:
            return
        self._emit(text, extra=[UNDERLINE])
        if url and url != text:
            self._emit(f" ({url})", extra=[DIM])

    def _abandon_link(self, span):
        boxes = {" ": "☐", "x": "☑", "X": "☑"}
        if self.after_marker and span["closed"] and span["url"] is None and span["buf"] in boxes:
            return self._emit(boxes[span["buf"]])
        self._emit("[")
        text = span["buf"] + ("]" if span["closed"] else "")
        text += "(" + span["url"] if span["url"] is not None else ""
        for ch in text:
            self._inline(ch)

    def _end_line(self):
        self._settle()
        self.w.newline()
        self._reset_inline()
        self.state, self.head = "start", ""

    # -- fenced code ------------------------------------------------------

    def _code_start(self, ch):
        """Inside a fence, at the start of a line: is this the closing fence?"""
        mark, length = self.fence
        if ch == "\n":
            closing = (_run(self.head.strip(), mark) >= length and set(self.head.strip()) == {mark}
                       and _run(self.head, " ") <= 3)
            self.w.raw(self.head, _sgr([DIM] if closing else [CYAN]))
            self.w.newline()
            self.head = ""
            if closing:
                self.fence, self.state = None, "start"
        elif ch in (" ", mark):
            self.head += ch
        else:
            self.w.raw(self.head + ch, _sgr([CYAN]))
            self.head, self.state = "", "code"

    def _code(self, ch):
        if ch == "\n":
            self.w.newline()
            self.state = "code_start"
        else:
            self.w.raw(ch, _sgr([CYAN]))

    # -- tables -----------------------------------------------------------

    def _flush_table(self):
        rows, self.table = self.table, []
        if not rows:
            return
        cells = [[c.strip() for c in re.split(r"(?<!\\)\|", row.strip().strip("|"))]
                 for row in rows]
        is_rule = len(cells) > 1 and all(re.fullmatch(r":?-{1,}:?", c) for c in cells[1])
        if not is_rule:             # pipes, but not a table
            head = self.head        # this can run while the next line is still being sniffed
            for row in rows:
                self._begin(row)
                self._end_line()
            self.head = head
            return
        for line in _table_lines(cells[0], cells[2:], cells[1], self._width()):
            self.w.raw(line)
            self.w.newline()


def _table_lines(header, body, rule, available):
    columns = max(len(header), *(len(r) for r in body)) if body else len(header)
    rows = [[_plain(c) for c in row] + [""] * (columns - len(row)) for row in [header] + body]
    align = [("right" if r.endswith(":") and not r.startswith(":") else
              "center" if r.endswith(":") else "left") for r in rule] + ["left"] * columns
    widths = [max(3, *(_width(row[i]) for row in rows)) for i in range(columns)]
    # Too wide for the terminal: take width from the widest column until it fits, then wrap cells.
    while sum(widths) + 3 * columns + 1 > available and max(widths) > 8:
        widths[widths.index(max(widths))] -= 1

    def pad(text, i):
        gap = widths[i] - _width(text)
        left = gap if align[i] == "right" else gap // 2 if align[i] == "center" else 0
        return " " * left + text + " " * (gap - left)

    dim = _sgr([DIM])
    bar = f"{dim}│{RESET}"
    lines = [f"{dim}┌{'┬'.join('─' * (w + 2) for w in widths)}┐{RESET}"]
    cells = [[textwrap.wrap(cell, widths[i]) or [""] for i, cell in enumerate(row)] for row in rows]
    # Rules between body rows only where rows span several lines and would otherwise run together.
    ruled = any(len(cell) > 1 for row in cells for cell in row)
    for n, wrapped in enumerate(cells):
        for k in range(max(len(w) for w in wrapped)):
            parts = [pad(w[k] if k < len(w) else "", i) for i, w in enumerate(wrapped)]
            if n == 0:
                parts = [f"{_sgr([BOLD])}{p}{RESET}" for p in parts]
            lines.append(f"{bar} " + f" {bar} ".join(parts) + f" {bar}")
        if n < len(rows) - 1 and (n == 0 or ruled):
            lines.append(f"{dim}├{'┼'.join('─' * (w + 2) for w in widths)}┤{RESET}")
    lines.append(f"{dim}└{'┴'.join('─' * (w + 2) for w in widths)}┘{RESET}")
    return lines


def render(text, width=None):
    """A whole markdown document rendered at once, for replaying saved messages."""
    out = io.StringIO()
    stream = MarkdownStream(out, width)
    stream.feed(text)
    stream.finish()
    return out.getvalue().rstrip("\n")
