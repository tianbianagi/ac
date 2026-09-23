"""A status bar pinned to the last row of the terminal (stdlib only).

The transcript scrolls in a region above it (DECSTBM), so readline, streamed replies and the
picker all work unchanged: they never see the last row. The REPL repaints the bar whenever
what it shows may have changed. StatusBar knows the terminal and nothing else; what the bar
says is render.status_bar's business, handed in as a paint(width) -> line callable so that it
can be redone when the terminal is resized.
"""

import atexit
import shutil
import signal
import sys
from contextlib import contextmanager

SIGWINCH = getattr(signal, "SIGWINCH", None)


class StatusBar:
    def __init__(self, out=None, size=None):
        self.out = out or sys.stdout
        self.size = size or (lambda: tuple(shutil.get_terminal_size((80, 24))))
        self.rows = None            # rows the region was set up for; None while closed
        self.paint = None           # paint(width) -> the line to show
        self.previous = None        # the SIGWINCH handler that was there before

    def open(self):
        if self.rows is not None:
            return
        self._region()
        if SIGWINCH is not None:    # readline's own handler chains to whatever was set here
            self.previous = signal.signal(SIGWINCH, lambda *_: self._repaint())
        atexit.register(self.close)

    def _region(self):
        """Keep the last row out of the scrolling region, for the terminal's size right now."""
        self.rows = self.size()[1]
        # The cursor must be above the last row before the region excludes it. Index (down,
        # scrolling if on the last row) then reverse index (up) leave it where it was, one row
        # higher if it was at the bottom, and keep its column, which readline relies on.
        self.out.write(f"\033D\033M\0337\033[1;{self.rows - 1}r\0338")
        self.out.flush()

    def close(self):
        """Give the whole screen back, leaving no trace of the bar."""
        if self.rows is None:
            return
        self.out.write(f"\0337\033[{self.rows};1H\033[2K\033[r\0338")
        self.out.flush()
        self.rows = None
        if SIGWINCH is not None:
            signal.signal(SIGWINCH, self.previous or signal.SIG_DFL)
        atexit.unregister(self.close)

    def draw(self, paint):
        self.paint = paint
        self._repaint()

    def _repaint(self):
        if self.rows is None or self.paint is None:
            return
        columns, rows = self.size()
        if rows != self.rows:
            # Resized. The terminal drops the region when that happens, and may have moved
            # every row (tmux pulls history in when a pane grows), so nothing is cleared by
            # the old geometry: the region is set up afresh and the bar painted on what is
            # the last row now. A stale copy left mid-screen is overwritten as output reaches it.
            self._region()
        # Save the cursor, paint the last row with wrapping off (a line of exactly the
        # terminal's width must not push anything), and go back where the cursor was.
        self.out.write(f"\0337\033[?7l\033[{self.rows};1H\033[2K{self.paint(columns)}"
                       "\033[?7h\0338")
        self.out.flush()

    @contextmanager
    def suspended(self):
        """Lend the whole screen to something else, an editor say, then put the bar back."""
        self.close()
        try:
            yield
        finally:
            self.open()
            self._repaint()
