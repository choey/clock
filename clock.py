#!/usr/bin/env python3
"""Live terminal clock: analog faces for UTC and local time, digital underneath.

Faces are drawn on a braille canvas (2x4 dots per cell). Refreshes every 19ms,
so the second hand sweeps smoothly rather than stepping. Press q (or Ctrl+C)
to quit.
"""

import contextlib
import math
import os
import select
import sys
import termios
import time
from datetime import datetime, timezone

HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
CLEAR_EOL = "\x1b[K"

# 19ms, not 20: coprime to 10, so the millisecond ones digit cycles through
# all ten values instead of sitting still. Reads as a live clock.
TICK = 0.019

ROWS = 11  # face height, in terminal rows
DEFAULT_CELL_RATIO = 2.1  # cell height / width; braille dots are square at 2


def _cell_ratio():
    """How tall a terminal cell is relative to its width.

    This is the only knob that decides whether the face is round, and it varies
    by font and line spacing. Override without editing: CLOCK_CELL_RATIO=2.7
    Raise it if the face looks squished, lower it if it bulges sideways.
    """
    try:
        value = float(os.environ.get("CLOCK_CELL_RATIO", ""))
    except ValueError:
        return DEFAULT_CELL_RATIO
    return value if value > 0 else DEFAULT_CELL_RATIO


CELL_RATIO = _cell_ratio()
COLS = math.floor(ROWS * CELL_RATIO + 0.5)  # face width, in terminal columns
GAP = 3  # blank columns between the two faces

# Hour numerals, every one two characters wide. A cell spans 2 dots, so an
# even-width string centres on a cell boundary while an odd-width one centres
# half a cell off it: "12" stacks exactly over "06", but never over "6".
MARKERS = ((0, "12"), (3, "03"), (6, "06"), (9, "09"))
MARKER_R = 0.70  # numeral distance from the centre, as a fraction of the radius

# (length as a fraction of the radius, two dots thick?), drawn shortest-first
HANDS = ((0.50, True), (0.75, True), (0.88, False))

# braille dot bit for (x % 2, y % 4); the block starts at U+2800
DOT_BITS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))


class Canvas:
    """A dot canvas that renders to braille cells, 2 dots wide by 4 tall each."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.cols = (w + 1) // 2
        self.cells = [[0] * self.cols for _ in range((h + 3) // 4)]

    def set(self, x, y):
        # floor(v + 0.5), not round(): round() is half-to-even here but
        # half-away-from-zero in the Go port, which would split the renders
        x, y = math.floor(x + 0.5), math.floor(y + 0.5)
        if 0 <= x < self.w and 0 <= y < self.h:
            self.cells[y // 4][x // 2] |= DOT_BITS[x % 2][y % 4]

    def line(self, x0, y0, x1, y1):
        steps = max(1, math.floor(max(abs(x1 - x0), abs(y1 - y0)) + 0.5))
        for i in range(steps + 1):
            t = i / steps
            self.set(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)

    def rows(self):
        return ["".join(chr(0x2800 + bits) for bits in row) for row in self.cells]


def face(now):
    """Render one analog face for `now`, returning a list of cell rows."""
    rx, ry = COLS, 2 * ROWS
    # Horizontally the centre sits on a cell boundary, vertically in the middle
    # of a row. The dot grid mirrors about both, which is what makes 09 and 03
    # land the same distance from the rim.
    cx, cy = rx - 0.5, ry - 0.5
    canvas = Canvas(2 * COLS, 4 * ROWS)

    def spoke(angle, r0, r1, thick):
        """Radial segment from r0 to r1, as fractions of the radius."""
        sin_a, cos_a = math.sin(angle), math.cos(angle)
        x0, y0 = cx + rx * r0 * sin_a, cy - ry * r0 * cos_a
        x1, y1 = cx + rx * r1 * sin_a, cy - ry * r1 * cos_a
        # two dots thick straddles the axis, so the spoke centres on it
        for off in (-0.5, 0.5) if thick else (0.0,):
            dx, dy = off * cos_a, off * sin_a
            canvas.line(x0 + dx, y0 + dy, x1 + dx, y1 + dy)

    # rim: sample densely enough that adjacent dots touch
    steps = math.floor(4 * math.pi * max(rx, ry) + 0.5)
    for i in range(steps):
        a = 2 * math.pi * i / steps
        canvas.set(cx + rx * math.sin(a), cy - ry * math.cos(a))

    # hour ticks, the quarters longer and thicker so they sit on the axes
    for h in range(12):
        major = h % 3 == 0
        spoke(2 * math.pi * h / 12, 0.80 if major else 0.90, 1.0, major)

    # hands: fractional seconds drive the sweep
    frac = now.microsecond / 1e6
    turns = (
        (now.hour % 12 + now.minute / 60 + now.second / 3600) / 12,
        (now.minute + (now.second + frac) / 60) / 60,
        (now.second + frac) / 60,
    )
    for (length, thick), turn in zip(HANDS, turns):
        spoke(2 * math.pi * turn, 0, length, thick)

    rows = canvas.rows()

    # hour numerals, overlaid as real characters: a cell holds braille or text
    # but never both, so a numeral hides whatever dots share its cell
    for hour, text in MARKERS:
        a = 2 * math.pi * hour / 12
        x = cx + rx * MARKER_R * math.sin(a)
        y = cy - ry * MARKER_R * math.cos(a)
        # centre an n-char string on x: it spans 2n dots, so its left edge
        # wants to sit at x - n, snapped to the nearest cell boundary
        col = math.floor((x - len(text) + 0.5) / 2 + 0.5)
        row = math.floor(y + 0.5) // 4
        if 0 <= row < len(rows) and 0 <= col <= canvas.cols - len(text):
            rows[row] = rows[row][:col] + text + rows[row][col + len(text) :]

    return rows


def frame(clocks):
    """Compose side-by-side faces with a digital readout under each."""
    faces = [face(t) for _, t in clocks]
    width = max(len(row) for row in faces[0])
    gap = " " * GAP

    rows = [gap.join(row) for row in zip(*faces)]
    rows.append(
        gap.join(
            f"{f'{label}: {t:%H:%M:%S}.{t.microsecond // 1000:03d}':^{width}}"
            for label, t in clocks
        )
    )
    return rows


@contextlib.contextmanager
def quiet_terminal():
    """Swallow keystrokes so they can't scroll the frame out from under us.

    Clears ECHO/ECHONL/ICANON but leaves ISIG set, so Ctrl+C still raises
    KeyboardInterrupt. Restoring with TCSAFLUSH discards whatever was typed
    during the run, so stray keys can't land in the shell afterwards.

    Yields whether stdin is a terminal, i.e. whether keys can be read at all.
    """
    try:
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except (AttributeError, ValueError, termios.error):
        yield False  # not a terminal (piped or redirected): nothing to quieten
        return

    quiet = list(saved)
    quiet[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON)  # lflag
    try:
        termios.tcsetattr(fd, termios.TCSANOW, quiet)
        yield True
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, saved)


def wants_quit():
    """True if a q is waiting on stdin. Never blocks; assumes cbreak mode."""
    if not select.select([sys.stdin], [], [], 0)[0]:
        return False
    return b"q" in os.read(sys.stdin.fileno(), 64).lower()


def main():
    sys.stdout.write(HIDE_CURSOR)
    height = 0
    try:
        with quiet_terminal() as interactive:
            while True:
                utc = datetime.now(timezone.utc)
                local = utc.astimezone()  # same instant, local zone
                rows = frame((("UTC", utc), (local.strftime("%Z"), local)))

                # rewind over the rows drawn last time, repaint in one write
                rewind = f"\x1b[{height}A" if height else ""
                sys.stdout.write(rewind + "".join(r + CLEAR_EOL + "\n" for r in rows))
                sys.stdout.flush()
                height = len(rows)

                if interactive and wants_quit():
                    break
                time.sleep(TICK - (time.monotonic() % TICK))
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.flush()


if __name__ == "__main__":
    main()
