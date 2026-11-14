"""A model of a terminal: enough of one to say what is on its screen.

Shared by painttest, which asks whether a clock painted the screen it meant
to, and keytest, which asks what was on the screen when a key was pressed.
Neither can read the stream directly any more: on a terminal a repaint writes
the rows that changed and leaves the rest, so a chunk of the stream is not a
frame and what a case is looking for can be on the screen while appearing in
none of it.

The vocabulary is small because the clocks' is: home, absolute cursor
positions at column one, erase to end of line, erase below, and the
alternate-screen and cursor-visibility pairs. Everything else -- colour SGR
included -- is text, and rides along inside the row it colours.
"""

import re

CLEAR_EOL = "\x1b[K"
CLEAR_BELOW = "\x1b[J"
HOME = "\x1b[H"
ENTER_ALT, LEAVE_ALT = "\x1b[?1049h", "\x1b[?1049l"
HIDE_CURSOR, SHOW_CURSOR = "\x1b[?25l", "\x1b[?25h"

# The escapes the model below answers. Everything between two of them is text
# to put on the screen -- colour SGR included, which rides along inside a row
# and is compared as part of it.
NEWLINE = re.compile(r"([\r\n])")
CONTROL = re.compile(
    r"\x1b\[\?(?:1049|25)[hl]"  # alternate screen, cursor visibility
    r"|\x1b\[(?:(\d+);(\d+))?H"  # cursor position, 1-based, or home
    r"|\x1b\[(\d+)A"  # cursor up
    r"|\x1b\[[KJ]"  # erase to end of line, erase below
)


class Screen:
    """Enough of a terminal to say what is on it, by rows.

    Both painters write whole rows from column one and end each with an erase
    to end of line, so a row is the finest thing either can touch and the model
    needs nothing finer. It says so out loud: a write that starts anywhere but
    column one would mean the painting has changed shape and this model has
    stopped describing it, and raises rather than quietly comparing the wrong
    thing.
    """

    def __init__(self):
        self.rows = {}  # 1-based row -> what is on it
        self.row, self.at_col1 = 1, True
        self.pending, self.writing = "", False
        self.left_alt = False

    def feed(self, data):
        pos = 0
        for m in CONTROL.finditer(data):
            self._text(data[pos : m.start()])
            pos = m.end()
            self._control(m)
        self._text(data[pos:])
        return self

    def _text(self, text):
        # Carriage returns are the pty's, not the clock's: a terminal in its
        # usual mode puts one in front of every newline written to it. They
        # mean what they say -- back to column one, same row -- and are
        # answered rather than stripped, so that a real one would be too.
        for part in NEWLINE.split(text):
            if part == "\n":
                self._commit()
                self.row += 1
                self.at_col1 = True
            elif part == "\r":
                self.at_col1 = True
            elif part:
                if not self.at_col1 and not self.writing:
                    raise AssertionError(
                        f"write away from column one on row {self.row}")
                self.pending += part
                self.writing = True

    def _commit(self):
        """End a row write: what was written is now the whole of that row."""
        if self.writing:
            self.rows[self.row] = self.pending
            self.pending, self.writing = "", False
            self.at_col1 = False

    def _control(self, m):
        seq = m.group(0)
        if seq.endswith("H"):
            self._commit()
            row, col = (int(m.group(1)), int(m.group(2))) if m.group(1) else (1, 1)
            if col != 1:
                raise AssertionError(f"cursor to column {col}: not a row-aligned paint")
            self.row, self.at_col1 = row, True
        elif seq.endswith("A"):
            self._commit()
            self.row = max(1, self.row - int(m.group(3)))
            self.at_col1 = True
        elif seq == CLEAR_EOL:
            # Whatever was written since column one is the whole row now --
            # including nothing at all, which is how a blank row is painted.
            self.writing = True
            self._commit()
        elif seq == CLEAR_BELOW:
            # At column one it takes this row too; after a row was written it
            # starts below one that has just been covered either way.
            first = self.row if self.at_col1 and not self.writing else self.row + 1
            self._commit()
            for r in [r for r in self.rows if r >= first]:
                del self.rows[r]
        elif seq == ENTER_ALT:
            self.rows, self.row, self.at_col1 = {}, 1, True
            self.pending, self.writing = "", False
        elif seq == LEAVE_ALT:
            self.left_alt = True

    def cleared(self):
        """What a terminal may do to its screen when the window changes.

        Some keep what was on the alternate screen across a resize, some
        truncate it, some throw it away; nothing the clock can read says
        which. So the model takes the worst of them -- a screen wiped the
        moment the window moves -- because that is the one a clock trusting
        its last frame would not survive, and surviving it costs one frame
        drawn whole.
        """
        self.rows, self.pending, self.writing = {}, "", False
        return self

    def shown(self):
        """The screen as a list of rows, trailing blanks dropped."""
        self._commit()
        out = [self.rows.get(r, "").rstrip()
               for r in range(1, max(self.rows, default=0) + 1)]
        while out and not out[-1]:
            out.pop()
        return out
