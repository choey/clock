#!/usr/bin/env python3
"""Live terminal clock: one analog face per time zone, digital underneath.

Faces are drawn on a braille canvas (2x4 dots per cell). Refreshes every 19ms,
so the second hand sweeps smoothly rather than stepping. Press q (or Ctrl+C)
to quit.
"""

import collections
import contextlib
import math
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ZIP resolution is a pair of tables with a release cycle of its own -- new
# gazetteer, redrawn boundaries -- so it lives in ziptz, a module installable
# and usable on its own. Absent, the clock is the same program minus ZIP
# tokens, which is worth more than refusing to start over a feature most
# invocations never reach; clock.go links the Go package beside it in at build
# time and so cannot make the same offer.
#
# The attribute lookup is the check that matters: a bare directory named ziptz
# anywhere on sys.path imports as an empty namespace package instead of
# failing, and an empty one answers no ZIP codes at all.
try:
    import ziptz

    ziptz.location
except (ImportError, AttributeError):  # pragma: no cover - ziptz not installed
    ziptz = None

# The clock needs a POSIX terminal: cbreak mode via termios, and select() on
# stdin. Neither exists on Windows, where select() handles sockets only. Say so
# instead of dying in an import traceback; clock.go prints the same line.
try:
    import select
    import termios
except ImportError:  # pragma: no cover - Windows only
    sys.stderr.write(
        "clock: Windows is not supported (needs a POSIX terminal); try WSL\n"
    )
    raise SystemExit(1)

HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
CLEAR_EOL = "\x1b[K"
CLEAR_BELOW = "\x1b[J"
HOME = "\x1b[H"
ENTER_ALT = "\x1b[?1049h"
LEAVE_ALT = "\x1b[?1049l"

USAGE = """clock - analog terminal clocks

usage: clock [-n N | --per-row N] [--color[=WHEN]] [--day[=WHEN]] [-q | --quiet]
             [--halign WHERE] [--valign WHERE] [--hpad SPACE] [--vpad SPACE]
             [--cell-ratio N] [--scale N] [ZONES]

  ZONES              comma-separated zone names, described below; default is
                      your local zone
  -n, --per-row N    clocks per row before wrapping, reduced to fit; auto
                      (default) picks whatever grows --scale auto the most
  --color[=WHEN]     colour the hands: always, auto (default), never or off
  --no-color         same as --color=never
  --day[=WHEN]       weekday on the readout: always, auto (default), never
  --no-day           same as --day=never
  -q, --quiet        skip the "press q to quit" hint shown at startup
  --halign WHERE     the grid across the window: left, center (default), right
  --valign WHERE     the grid down the window: top, center (default), bottom
  --hpad SPACE       between clocks: even (default), or a share of the width
                      like 10%
  --vpad SPACE       between rows: even (default), or a share of the height
                      like 5%
  --cell-ratio N     font cell height / width (default 2.1); raise if the
                      face looks squished, lower if it bulges sideways
  --scale N          resize every face by this factor; auto (default) fills
                      the window, at minimum padding
  -h, --help         this message

A zone is an IANA name (Europe/Berlin), the city off the end of one where
that is unambiguous (Berlin, Jakarta), a regional abbreviation (ET CT MT PT
AKT HT BST IST JST AET ...), a 2-letter country code (JP, GB), or a US ZIP
code (94110). ET/CT/MT/PT follow daylight saving, so they read EST or EDT
depending on the date; EST/EDT/PST/PDT and the rest are the fixed offsets,
which never shift.

The hands are coloured on a terminal and plain when redirected; NO_COLOR
turns the colour off everywhere. Auto puts a weekday on the readouts only
when the clocks on screen disagree about the date. An even fill spreads the
clocks over the whole window; --hpad 10% sets the gaps instead, as a share of
the window, and then the alignment decides where the grid sits. CLOCK_CELL_RATIO
sets the same thing as --cell-ratio, for when it wants to be set once per
terminal rather than typed every time; the flag wins if both are given.

Space holds the frame still, for a screenshot, and h or ? opens the key list.
Press q or Ctrl+C to quit.

examples:
  clock
  clock ET,PT,UTC
  clock -n 2 ET,PT,UTC
  clock Berlin,Jakarta
  clock Europe/Berlin,Asia/Tokyo,94110 --per-row 2
"""

# The key list h or ? puts up in a modal. In the order the keys are reached
# for rather than alphabetically, and kept in the same order as clock.go's
# table.
HOTKEYS = (
    ("space", "hold the frame"),
    ("h ?", "toggle this list"),
    ("q", "quit, or Ctrl+C"),
)
HOTKEY_COL = 8  # where the descriptions start, so the keys get a gutter

# Said once, in the same modal the key list uses, and then dropped: a clock
# that has taken the whole screen owes the reader a way back out, but only
# until it is read. -q/--quiet skips it outright.
FLASH = "Press q or Ctrl+C to quit"
FLASH_SECONDS = 3.0

DEFAULT_PER_ROW = 3  # faces per row before wrapping
MAX_PER_ROW = 64  # an upper bound so a typo can't ask for a million faces

# 19ms, not 20: coprime to 10, so the millisecond ones digit cycles through
# all ten values instead of sitting still. Reads as a live clock.
TICK = 0.019

DEFAULT_ROWS_N = 11  # face height, in terminal rows, at --scale 1
DEFAULT_CELL_RATIO = 2.1  # cell height / width; braille dots are square at 2

# ROWS keeps a face big enough for the hour numerals to have somewhere to
# sit; MAX_ROWS_N is just a guard against a typo asking for a giant canvas.
MIN_ROWS_N = 4
MAX_ROWS_N = 200

# How many rows below the largest fit auto_scale will give up looking for
# one where both ROWS and COLS are odd -- see its own comment for why that
# is worth a few rows of size.
SYMMETRY_WINDOW = 8

# ROWS, CELL_RATIO and COLS start at the defaults and are set for real in
# run(), once --scale, --cell-ratio and CLOCK_CELL_RATIO have been read;
# nothing touches any of them before then.
ROWS = DEFAULT_ROWS_N
CELL_RATIO = DEFAULT_CELL_RATIO
COLS = math.floor(ROWS * DEFAULT_CELL_RATIO + 0.5)  # face width, in terminal columns
GAP = 3  # fewest blank columns between adjacent faces
VGAP = 1  # fewest blank rows between rows of faces


def _positive_float(value):
    return not (math.isnan(value) or math.isinf(value)) and value > 0


def env_cell_ratio():
    """How tall a terminal cell is relative to its width, from CLOCK_CELL_RATIO.

    This is the only knob that decides whether the face is round, and it varies
    by font and line spacing. Override without editing: CLOCK_CELL_RATIO=2.7
    Raise it if the face looks squished, lower it if it bulges sideways.

    Unlike --cell-ratio, an environment variable might be stale or set for
    some other program, so a bad value is not a user error -- it is simply
    ignored, the same way an unset one is.
    """
    try:
        value = float(os.environ.get("CLOCK_CELL_RATIO", ""))
    except ValueError:
        return DEFAULT_CELL_RATIO
    return value if _positive_float(value) else DEFAULT_CELL_RATIO


def parse_ratio(val):
    """Read a positive, finite decimal for --cell-ratio.

    Delegates to float() rather than a hand-rolled scan, the same as
    CLOCK_CELL_RATIO already does: this knob shapes one face, not a zone or a
    count, and does not carry the same cross-language byte-for-byte stakes.
    """
    try:
        value = float(val)
    except ValueError:
        value = None
    if value is None or not _positive_float(value):
        raise ClockError(f'--cell-ratio wants a positive number, e.g. --cell-ratio 2.6, got "{val}"')
    return value


def parse_scale(val):
    """Read a positive, finite decimal for --scale."""
    try:
        value = float(val)
    except ValueError:
        value = None
    if value is None or not _positive_float(value):
        raise ClockError(f'--scale wants auto or a positive number, e.g. --scale 1.5, got "{val}"')
    return value

# Where the grid sits when it does not fill the window, and what --halign and
# --valign accept. Same order as clock.go's tables, and the wording of the
# error they raise comes off these lists.
HALIGNS = ("left", "center", "right")
VALIGNS = ("top", "center", "bottom")

# The one instant format CLOCK_FREEZE accepts. Exactly six fractional digits,
# exactly UTC: datetime stops at microseconds, and pinning the format keeps both
# implementations rejecting the same strings.
FREEZE_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class ClockError(Exception):
    """A startup failure to report before the terminal has been touched."""


def freeze():
    """The instant to pin the clock to, or None to run live.

    CLOCK_FREEZE holds the clock at a fixed instant: the hands never move, and
    space has nothing to hold back. Redirected it draws that one frame and
    exits, which is what lets the Go and Python renders be diffed byte for
    byte; on a terminal it stays up, with h and q still live, so the frame can
    be looked at and photographed. Dev hook, not in --help.
    """
    value = os.environ.get("CLOCK_FREEZE", "")
    if not value:
        return None
    try:
        frozen = datetime.strptime(value, FREEZE_FORMAT)
        # Insist on the canonical spelling, not merely a parseable one. Both
        # parsers are loose in their own directions -- strptime takes fewer
        # than six fractional digits, Go takes a one-digit month -- and the
        # round trip is the one cheap check that pins them to the same strings.
        canonical = frozen.strftime(FREEZE_FORMAT) == value
    except ValueError:
        canonical = False
    if not canonical:
        raise ClockError(
            "CLOCK_FREEZE wants an instant like 2026-07-15T09:53:07.123456Z, "
            f'got "{value}"'
        ) from None
    return frozen.replace(tzinfo=timezone.utc)


def env_whole(name, lowest, default):
    """One whole number out of the environment, or the default if unset.

    Unlike CLOCK_CELL_RATIO, a bad value here is a hard error rather than
    something to shrug off: these are the diff harness's knobs, and a typo that
    quietly fell back to the default would leave a test claiming to cover a
    sequence it never drew. Nine digits at most, so that Python's unbounded int
    and Go's Atoi accept exactly the same strings.
    """
    value = os.environ.get(name, "")
    if not value:
        return default
    if value.isascii() and value.isdigit() and len(value) <= 9 and int(value) >= lowest:
        return int(value)
    raise ClockError(
        f"{name} wants a whole number of {lowest} or more, at most nine digits, "
        f'got "{value}"'
    )


def sequence(frozen):
    """How many frames a pinned clock draws, and how far the instant moves
    between them.

    CLOCK_FRAMES draws that many instead of one, stepping the pinned instant by
    CLOCK_STEP milliseconds each time -- one tick by default, so the sequence
    advances exactly as a live clock would. It is what lets the harness compare
    what a single frame cannot show: the second hand sweeping, the rewind that
    repaints over the frame before it, and the faces regrouping as a zone
    crosses a daylight-saving boundary.

    Only where a pinned clock already draws and exits, which is redirected; on
    a terminal one frame still stays up, so this cannot animate what is meant
    to hold still. Dev hook, not in --help.
    """
    frames = env_whole("CLOCK_FRAMES", 1, 1)
    step = env_whole("CLOCK_STEP", 0, round(TICK * 1000))
    if frozen is None and (frames != 1 or os.environ.get("CLOCK_STEP", "")):
        raise ClockError(
            "CLOCK_FRAMES and CLOCK_STEP need CLOCK_FREEZE, the instant they step from"
        )
    return frames, timedelta(milliseconds=step)


# Hour numerals, every one two characters wide. A cell spans 2 dots, so an
# even-width string centres on a cell boundary while an odd-width one centres
# half a cell off it: "12" stacks exactly over "06", but never over "6".
MARKERS = ((0, "12"), (3, "03"), (6, "06"), (9, "09"))
MARKER_R = 0.70  # numeral distance from the centre, as a fraction of the radius
HAND_TAPER = 0.15  # fraction of a thick hand's length that narrows to a point at the tip

# Which hand a cell belongs to, and so which colour it takes. Higher is on
# top: the hands stack shortest-first, the reverse of the order they are drawn
# in, because a longer hand covers a shorter one along its whole length while
# the short one can only ever hide a slice. Left the other way round, the hour
# hand -- the one you most want to find -- vanishes under the minute hand for
# minutes at a time.
LAYER_NONE, LAYER_SECOND, LAYER_MINUTE, LAYER_HOUR = 0, 1, 2, 3

# Foreground SGR code per layer: red second hand as on a real dial, then cyan
# and yellow, which stay legible on a light and a dark terminal alike. Plain
# 8-colour codes, so they follow whatever palette the terminal is themed with.
HAND_SGR = ("", "\x1b[31m", "\x1b[36m", "\x1b[33m")
DEFAULT_FG = "\x1b[39m"  # foreground back to the terminal's default, nothing else

# (length as a fraction of the radius, two dots thick?, layer), drawn
# shortest-first -- which is not the order they stack in; see LAYER_* above
HANDS = (
    (0.50, True, LAYER_HOUR),
    (0.75, True, LAYER_MINUTE),
    (0.88, False, LAYER_SECOND),
)

# What --color and --day accept; auto reads the situation, the other two do not.
WHENS = ("always", "auto", "never")
# --color takes "off" too, alongside "never": both disable colour, but "off"
# is the more obvious word for it.
COLOR_WHENS = ("always", "auto", "never", "off")

# braille dot bit for (x % 2, y % 4); the block starts at U+2800
DOT_BITS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))


def snap(v):
    """Quantise a dot coordinate to 1e-9 before anything rounds it to a grid.

    Python calls the platform libm for sin/cos while the Go port computes them
    in software; the two agree to well under an ulp but not bit for bit, e.g.
    cos(5.562579797474062) is ...96494540 here and ...96505642 there. Unsnapped,
    a dot whose true position sits within 1e-16 of a half-dot boundary rounds
    into different cells in the two renders — which it does, on the rim, every
    single frame. 1e-9 swallows that disagreement and is still far finer than
    the quarter-cell grid it feeds.
    """
    return math.floor(v * 1e9 + 0.5) / 1e9


class Canvas:
    """A dot canvas that renders to braille cells, 2 dots wide by 4 tall each.

    Alongside the dots each cell keeps the topmost layer that dotted it, which
    is what the colouring reads. Colour is per cell and dots are not: a cell
    holds up to eight of them, so where two hands share a cell the cell takes
    the upper hand's colour and a few of the lower hand's dots come along.
    """

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.cols = (w + 1) // 2
        self.cells = [[0] * self.cols for _ in range((h + 3) // 4)]
        self.layers = [[LAYER_NONE] * self.cols for _ in range((h + 3) // 4)]

    def set(self, x, y, layer):
        # floor(v + 0.5), not round(): round() is half-to-even here but
        # half-away-from-zero in the Go port, which would split the renders
        x, y = math.floor(snap(x) + 0.5), math.floor(snap(y) + 0.5)
        if 0 <= x < self.w and 0 <= y < self.h:
            self.cells[y // 4][x // 2] |= DOT_BITS[x % 2][y % 4]
            if layer > self.layers[y // 4][x // 2]:
                self.layers[y // 4][x // 2] = layer

    def line(self, x0, y0, x1, y1, layer):
        # snap the endpoints too: steps comes off a rounded difference, and a
        # one-ulp wobble there changes the whole dot sequence, not just one dot
        x0, y0, x1, y1 = snap(x0), snap(y0), snap(x1), snap(y1)
        steps = max(1, math.floor(max(abs(x1 - x0), abs(y1 - y0)) + 0.5))
        for i in range(steps + 1):
            t = i / steps
            self.set(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, layer)

    def rows(self):
        return ["".join(chr(0x2800 + bits) for bits in row) for row in self.cells]


def colorize(row, layers):
    """Wrap each run of same-layer cells in that hand's colour.

    Runs rather than cells: a hand lies along a dozen cells at a stretch, and
    one escape per cell would multiply what a frame writes for no visible
    difference. Rows end back on the default foreground, so the gutter between
    two faces, and whatever the terminal paints past the end of the line, stay
    the colour they were.
    """
    out = []
    current = LAYER_NONE
    for cell, layer in zip(row, layers):
        if layer != current:
            out.append(DEFAULT_FG if layer == LAYER_NONE else HAND_SGR[layer])
            current = layer
        out.append(cell)
    if current != LAYER_NONE:
        out.append(DEFAULT_FG)
    return "".join(out)


def face(now, color):
    """Render one analog face for `now`, returning a list of cell rows."""
    rx, ry = COLS, 2 * ROWS
    # Horizontally the centre sits on a cell boundary, vertically in the middle
    # of a row. The dot grid mirrors about both, which is what makes 09 and 03
    # land the same distance from the rim.
    cx, cy = rx - 0.5, ry - 0.5
    canvas = Canvas(2 * COLS, 4 * ROWS)

    def spoke(angle, r0, r1, thick, point, layer):
        """Radial segment from r0 to r1, as fractions of the radius.

        A thick spoke drawn with point set narrows over its last HAND_TAPER
        share to a single dot at r1, instead of ending in a flat,
        two-dot-wide butt -- that is a hand. A thick spoke without point is a
        plain parallel-sided band the same width all the way to r1 -- that is
        always one of the four major hour ticks (h = 0, 3, 6, 9), always
        exactly axis-aligned, always beside a numeral.
        """
        sin_a, cos_a = math.sin(angle), math.cos(angle)
        x0, y0 = cx + rx * r0 * sin_a, cy - ry * r0 * cos_a
        x1, y1 = cx + rx * r1 * sin_a, cy - ry * r1 * cos_a

        if thick and point:
            tip = r1 - (r1 - r0) * HAND_TAPER
            tx, ty = cx + rx * tip * sin_a, cy - ry * tip * cos_a
            for off in (-0.5, 0.5):
                dx, dy = off * cos_a, off * sin_a
                # the offset shrinks to nothing at the tip, not the base:
                # that is what tapers the two edges together into a point
                canvas.line(x0 + dx, y0 + dy, tx, ty, layer)
            canvas.line(tx, ty, x1, y1, layer)
            return
        if not thick:
            canvas.line(x0, y0, x1, y1, layer)
            return

        # A symmetric +-0.5 offset here would straddle a character cell
        # boundary about half the time -- whichever side of a 2-or-4-dot cell
        # the true centre's neighbouring dot falls on -- splitting the
        # tick's two lines into different rows or columns and making it look
        # disjointed from the numeral beside it. Landing both dots in the
        # same cell as the numeral's own dot instead costs at most half a dot
        # of true centring, invisible, for a tick that always reads as
        # attached to its numeral, which is not.
        horizontal = abs(cos_a) < 0.5
        cell_size, center = (4, cy) if horizontal else (2, cx)
        step = 1.0 if math.floor(center + 0.5) % cell_size == 0 else -1.0
        for s in (0.0, step):
            if horizontal:
                canvas.line(x0, y0 + s, x1, y1 + s, layer)
            else:
                canvas.line(x0 + s, y0, x1 + s, y1, layer)

    # rim: sample densely enough that adjacent dots touch
    steps = math.floor(4 * math.pi * max(rx, ry) + 0.5)
    for i in range(steps):
        a = 2 * math.pi * i / steps
        canvas.set(cx + rx * math.sin(a), cy - ry * math.cos(a), LAYER_NONE)

    # hour ticks, the quarters longer and thicker so they sit on the axes
    for h in range(12):
        major = h % 3 == 0
        spoke(2 * math.pi * h / 12, 0.80 if major else 0.90, 1.0, major, False, LAYER_NONE)

    # hands: fractional seconds drive the sweep
    frac = now.microsecond / 1e6
    turns = (
        (now.hour % 12 + now.minute / 60 + now.second / 3600) / 12,
        (now.minute + (now.second + frac) / 60) / 60,
        (now.second + frac) / 60,
    )
    for (length, thick, layer), turn in zip(HANDS, turns):
        spoke(2 * math.pi * turn, 0, length, thick, True, layer)

    rows = canvas.rows()

    # hour numerals, overlaid as real characters: a cell holds braille or text
    # but never both, so a numeral hides whatever dots share its cell
    for hour, text in MARKERS:
        a = 2 * math.pi * hour / 12
        x = snap(cx + rx * MARKER_R * math.sin(a))
        y = snap(cy - ry * MARKER_R * math.cos(a))
        # centre an n-char string on x: it spans 2n dots, so its left edge
        # wants to sit at x - n, snapped to the nearest cell boundary
        col = math.floor((x - len(text) + 0.5) / 2 + 0.5)
        row = math.floor(y + 0.5) // 4
        if 0 <= row < len(rows) and 0 <= col <= canvas.cols - len(text):
            rows[row] = rows[row][:col] + text + rows[row][col + len(text) :]
            # the numeral took the cell's dots with it, so drop their colour
            for i in range(col, col + len(text)):
                canvas.layers[row][i] = LAYER_NONE

    if color:
        rows = [colorize(r, l) for r, l in zip(rows, canvas.layers)]
    return rows


class HelpRequested(Exception):
    """-h or --help: print the usage text and stop, successfully."""


def parse_count(s, what, limit):
    """Read a positive whole number, strictly.

    Not int(), which also takes surrounding space, underscores, a leading sign
    and non-ASCII digits; the Go port's parser has to accept exactly the same
    strings, and it hand-scans ASCII digits.
    """
    bad = ClockError(f'{what} wants a whole number from 1 to {limit}, got "{s}"')
    if not s or len(s) > len(str(limit)):
        raise bad
    n = 0
    for c in s:
        if not ("0" <= c <= "9"):
            raise bad
        n = n * 10 + (ord(c) - ord("0"))
    if not 1 <= n <= limit:
        raise bad
    return n


# What each of the four layout flags suggests when it is handed no value.
NEEDS = {
    "halign": "--halign center",
    "valign": "--valign center",
    "hpad": "--hpad 10%",
    "vpad": "--vpad 5%",
    "cell-ratio": "--cell-ratio 2.6",
    "scale": "--scale 1.5",
}


def parse_choice(flag, val, choices):
    """Read one of a short list of words, or reject it by name.

    The message is built from the list, so a flag cannot come to accept a word
    its own error text does not offer.
    """
    if val in choices:
        return val
    names = ", ".join(choices[:-1]) + " or " + choices[-1]
    raise ClockError(f'--{flag} wants {names}, got "{val}"')


def parse_pad(flag, val):
    """Read a padding: None for the even fill, or a percentage 0-100.

    Takes "10" as readily as "10%", and nothing else -- no sign, no decimal
    point, no space, since the Go port hand-scans the same digits.
    """
    if val == "even":
        return None
    bad = ClockError(f'--{flag} wants even or a share like 10%, got "{val}"')
    digits = val[:-1] if val.endswith("%") else val
    if not digits or len(digits) > 3:
        raise bad
    n = 0
    for c in digits:
        if not ("0" <= c <= "9"):
            raise bad
        n = n * 10 + (ord(c) - ord("0"))
    if n > 100:
        raise bad
    return n


def parse_args(argv):
    """Read the command line: one optional zone list, and the flags anywhere.

    Hand-rolled rather than argparse, which prints its own usage block, exits
    with status 2, and abbreviates long options -- none of which the Go port
    can reproduce. Both implementations run this algorithm verbatim.
    """
    per_row = DEFAULT_PER_ROW  # only used when an explicit -n/--per-row overrides per_row_auto below
    color_when = "auto"
    day_when = ""  # unset: run() picks it, since a pinned clock differs
    halign, valign = "center", "center"
    hpad, vpad = None, None  # None is the even fill
    quiet = False
    cell_ratio_flag = None  # None: run() falls back to CLOCK_CELL_RATIO, then the default
    scale_flag = None  # unset unless a specific --scale overrides scale_auto below
    scale_auto = True  # the default: run() re-solves ROWS every frame to fill the window
    per_row_auto = True  # the default: run() also searches per-row counts, to maximise ROWS
    positional = []
    end_of_flags = False

    i = 0
    while i < len(argv):
        a = argv[i]
        if end_of_flags:
            positional.append(a)
        elif a == "--":
            end_of_flags = True
        elif a in ("-h", "--help"):
            raise HelpRequested
        elif a.startswith("--"):
            name, sep, val = a[2:].partition("=")
            if name == "per-row":
                if not sep:
                    i += 1
                    if i >= len(argv):
                        raise ClockError("--per-row needs a number, e.g. --per-row 2")
                    val = argv[i]
                if val == "auto":
                    per_row_auto = True
                else:
                    per_row = parse_count(val, "--per-row", MAX_PER_ROW)
                    per_row_auto = False
            elif name == "color":
                # Bare --color means always, and takes no separate argument:
                # "clock --color ET" names a zone list, exactly as ls and git
                # read the same flag. The value only ever follows an "=".
                color_when = parse_choice("color", val, COLOR_WHENS) if sep else "always"
            elif name == "no-color":
                if sep:
                    raise ClockError("--no-color takes no value")
                color_when = "never"
            elif name == "day":
                day_when = parse_choice("day", val, WHENS) if sep else "always"
            elif name == "no-day":
                if sep:
                    raise ClockError("--no-day takes no value")
                day_when = "never"
            elif name == "quiet":
                if sep:
                    raise ClockError("--quiet takes no value")
                quiet = True
            elif name in ("halign", "valign", "hpad", "vpad", "cell-ratio", "scale"):
                # These six want a value, and take it either way round, as
                # --per-row does: there is no bare form to be ambiguous with.
                if not sep:
                    i += 1
                    if i >= len(argv):
                        raise ClockError(f"--{name} needs a value, e.g. {NEEDS[name]}")
                    val = argv[i]
                if name == "halign":
                    halign = parse_choice(name, val, HALIGNS)
                elif name == "valign":
                    valign = parse_choice(name, val, VALIGNS)
                elif name == "hpad":
                    hpad = parse_pad(name, val)
                elif name == "vpad":
                    vpad = parse_pad(name, val)
                elif name == "cell-ratio":
                    cell_ratio_flag = parse_ratio(val)
                elif val == "auto":
                    scale_auto = True
                else:
                    scale_flag = parse_scale(val)
                    scale_auto = False
            else:
                raise ClockError(f"unknown option: --{name}")
        elif a == "-q":
            quiet = True
        elif len(a) > 1 and a.startswith("-"):
            if a[1] != "n":
                raise ClockError(f"unknown option: {a}")
            rest = a[2:]
            if rest == "":
                i += 1
                if i >= len(argv):
                    raise ClockError("-n needs a number, e.g. -n 2")
                rest = argv[i]
            elif rest[0] == "=":
                raise ClockError('-n takes its value as "-n N" or "-nN", not "-n=N"')
            if rest == "auto":
                per_row_auto = True
            else:
                per_row = parse_count(rest, "-n", MAX_PER_ROW)
                per_row_auto = False
        else:
            positional.append(a)
        i += 1

    if len(positional) > 1:
        raise ClockError(
            f"expected one comma-separated zone list, got {len(positional)}: "
            + " ".join(positional)
        )
    return (
        per_row,
        positional[0] if positional else "",
        color_when,
        day_when,
        (halign, valign, hpad, vpad),
        quiet,
        cell_ratio_flag,
        scale_flag,
        scale_auto,
        per_row_auto,
    )


# Fills the gaps the tz database leaves, and only those gaps. EST, MST, HST,
# GMT, CET and EET are real zones with fixed, DST-free meanings, so they are
# looked up verbatim instead: aliasing GMT to Europe/London would make it read
# BST every July, which is simply wrong. Sorted, same order as clock.go's table.
ZONE_ALIASES = (
    ("ACT", "Australia/Adelaide"),
    ("AET", "Australia/Sydney"),
    ("AKT", "America/Anchorage"),
    ("AWT", "Australia/Perth"),
    ("BST", "Europe/London"),
    ("CT", "America/Chicago"),
    ("ET", "America/New_York"),
    ("HKT", "Asia/Hong_Kong"),
    ("HT", "Pacific/Honolulu"),
    ("IST", "Asia/Kolkata"),
    ("JST", "Asia/Tokyo"),
    ("KST", "Asia/Seoul"),
    ("MT", "America/Denver"),
    ("NZT", "Pacific/Auckland"),
    ("PT", "America/Los_Angeles"),
    ("SGT", "Asia/Singapore"),
    ("UK", "Europe/London"),
)

# The half of each daylight-saving pair that names an offset rather than a
# place: nowhere is on PDT in January, so these cannot be looked up in the tz
# database. Each becomes a fixed-offset clock that never shifts, which is
# precisely what the name means -- PST is Los Angeles in winter, and stays
# there in July while PT moves to PDT. EST, MST and HST are absent because the
# tz database already carries them as fixed zones, and CST is the US reading;
# China is CN or Asia/Shanghai. Sorted, same order as clock.go's table.
ZONE_FIXED = (
    ("AKDT", -8 * 3600),
    ("AKST", -9 * 3600),
    ("CDT", -5 * 3600),
    ("CST", -6 * 3600),
    ("EDT", -4 * 3600),
    ("HDT", -9 * 3600),
    ("MDT", -6 * 3600),
    ("PDT", -7 * 3600),
    ("PST", -8 * 3600),
)


def alias_names():
    return " ".join(name for name, _ in ZONE_ALIASES)


def zip_zone(token):
    """The zone one ZIP code names, via the ziptz module.

    Its error text is written to be printed as-is, so it passes straight
    through; clock.go prints the same lines from the Go package beside it.
    """
    if ziptz is None:
        raise ClockError(
            f'"{token}" is a ZIP code, and resolving one needs the ziptz'
            " module, which ships beside this file in the clock repository"
        )
    try:
        return ziptz.location(token)
    except ziptz.ZipError as exc:
        raise ClockError(str(exc)) from None


def zone_tab():
    """The tz database's country table, and whether it was found at all.

    Absent on stripped-down systems, so never fatal.
    """
    for directory in (
        os.environ.get("TZDIR", ""),
        "/usr/share/zoneinfo",
        "/usr/share/lib/zoneinfo",
        "/usr/lib/locale/TZ",
    ):
        if not directory:
            continue
        try:
            with open(directory + "/zone.tab", encoding="utf-8") as handle:
                return handle.read(), True
        except OSError:
            continue
    return "", False


def country_zones(cc):
    """A country's zones in file order, which is the tz database's own idea of
    most-populous-first rather than anything alphabetical."""
    data, found = zone_tab()
    if not found:
        return [], False
    out = []
    for line in data.split("\n"):
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 3 and fields[0] == cc:
            out.append(fields[2])
    return out, True


def country_zone(cc, at):
    """Resolve a 2-letter country code, collapsing zones that agree.

    Germany lists Europe/Berlin and Europe/Busingen, an enclave that has kept
    the same time since 1970, so DE is not genuinely ambiguous; the US is.
    """
    names, found = country_zones(cc)
    if not found:
        raise ClockError(
            f'cannot resolve the country code "{cc}": '
            "no zone.tab under /usr/share/zoneinfo"
        )
    zones, kept, seen = [], [], set()
    for name in names:
        try:
            zi = ZoneInfo(name)
        except Exception:
            continue
        here = at.astimezone(zi)
        key = f"{here:%Z}|{int(here.utcoffset().total_seconds())}"
        if key not in seen:
            seen.add(key)
            zones.append(zi)
            kept.append(name)
    if not zones:
        return None  # not a country code we know; caller falls through
    if len(zones) == 1:
        return zones[0]
    shown, tail = kept, ""
    if len(shown) > 8:
        tail = f" (and {len(shown) - 8} more)"
        shown = shown[:8]
    raise ClockError(
        f"{cc} spans {len(kept)} time zones; name one: " + ", ".join(shown) + tail
    )


def suffix_zones(token):
    """The zones whose name ends with the token as a whole path segment.

    Europe/Berlin for "Berlin", and America/Indiana/Indianapolis for either
    "Indianapolis" or "Indiana/Indianapolis". Whole segments only, so "Berl"
    finds nothing and "York" does not answer for "New_York".

    Read out of zone.tab, the same file the country codes come from, which
    lists the canonical zones and leaves out the backward-compatibility links
    -- so "Eastern" is not a name here, and US/Eastern still resolves the
    ordinary way, in full.
    """
    data, found = zone_tab()
    if not found:
        return [], False
    want = "/" + token.lower()
    out = []
    for line in data.split("\n"):
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 3 and fields[2].lower().endswith(want):
            out.append(fields[2])
    return out, True


def suffix_zone(token):
    """One zone named by its tail alone, or None when nothing matches.

    Ambiguity is refused rather than guessed at. Every city in the tz database
    is unique today, but nothing promises it stays that way, and two clocks an
    ocean apart is not a choice to make on the reader's behalf.
    """
    names, found = suffix_zones(token)
    if not found or not names:
        return None
    if len(names) > 1:
        shown, tail = names, ""
        if len(shown) > 8:
            tail = f" (and {len(shown) - 8} more)"
            shown = shown[:8]
        raise ClockError(
            f"{token} names {len(names)} zones; name one in full: "
            + ", ".join(shown)
            + tail
        )
    try:
        return ZoneInfo(names[0])
    except Exception:
        return None


def unknown_zone(token):
    return ClockError(
        f'unknown zone "{token}"; use an IANA name (Europe/Berlin), a city '
        f"off the end of one (Berlin, Jakarta), an abbreviation "
        f"({alias_names()}), a 2-letter country code (JP), or a US ZIP code"
    )


def resolve_zone(token, at):
    """Turn one token into a tzinfo, or None meaning the system's local zone.

    Order matters: the alias table is consulted before the tz database only for
    names the database lacks, the fixed-offset table only after it so that real
    zones win, and "local" and "" are intercepted because Python
    and Go disagree about both -- ZoneInfo("Local") raises where
    LoadLocation("Local") works, and ZoneInfo("") raises where LoadLocation("")
    quietly returns UTC.
    """
    if token.startswith("/") or ".." in token:
        raise ClockError(f'"{token}" is not a zone name')
    if token.lower() == "local":
        return None
    if token.isascii() and token.isdigit():
        return zip_zone(token)
    up = token.upper()
    for name, target in ZONE_ALIASES:
        if name == up:
            try:
                return ZoneInfo(target)
            except Exception:
                raise ClockError(
                    f"{up} means {target}, which this system's time zone database lacks"
                ) from None
    try:
        return ZoneInfo(token)
    except Exception:
        pass
    for name, offset in ZONE_FIXED:
        if name == up:
            return timezone(timedelta(seconds=offset), name)
    if len(up) == 2 and "A" <= up[0] <= "Z" and "A" <= up[1] <= "Z":
        found = country_zone(up, at)
        if found is not None:
            return found
    # Last, so a city can never shadow a name the database itself answers to.
    named = suffix_zone(token)
    if named is not None:
        return named
    raise unknown_zone(token)


def resolve_zones(zone_list, at):
    """Turn the comma-separated list into (token, zone) pairs, left to right.

    The token is carried along because merge_zones labels a face with the
    spellings that asked for it, not just the zone it landed on.
    """
    if not zone_list:
        return [("", None)]
    out = []
    for token in zone_list.split(","):
        token = token.strip(" \t")
        if not token:
            raise ClockError(f'empty zone in "{zone_list}"')
        out.append((token, resolve_zone(token, at)))
    return out


def zone_label(abbr, tokens):
    """The name written over one face.

    Just the abbreviation, unless more than one spelling collapsed onto this
    face -- then each spelling that reads differently is named too, because
    that is the only place the ambiguity is visible. PDT,PDT asked the same
    question twice and gets one plain answer.
    """
    if len(tokens) < 2:
        return abbr
    return "/".join([abbr] + [t for t in tokens if t.upper() != abbr.upper()])


def merge_zones(zones, now):
    """Collapse zones that show the same wall clock at `now` into one face.

    Keyed on abbreviation and offset, the same test country_zone uses: however
    two tokens were spelled, and whether or not one resolves onto the other,
    they are one clock if they read alike. That is a property of the instant,
    not of the zones -- PDT and PT are one clock in July and two in January --
    so this regroups as the clock runs rather than once at startup, and a grid
    crossing a daylight-saving boundary splits itself as it happens. The loop
    calls it once a second, which is as often as its answer can change.
    """
    out, index = [], {}
    for token, zone in zones:
        t = in_zone(now, zone)
        key = (f"{t:%Z}", int(t.utcoffset().total_seconds()))
        if key not in index:
            index[key] = len(out)
            out.append((key[0], [], zone))
        _, tokens, _ = out[index[key]]
        if token and not any(token.upper() == seen.upper() for seen in tokens):
            tokens.append(token)
    return [(zone_label(abbr, tokens), zone) for abbr, tokens, zone in out]


def in_zone(t, zone):
    """The instant t as seen in `zone`; None means the system's local zone."""
    return t.astimezone() if zone is None else t.astimezone(zone)


def utc_offset(t, zone):
    """How far `zone` sits from UTC at t, in seconds east."""
    return int(in_zone(t, zone).utcoffset().total_seconds())


def order_faces(faces, now):
    """Faces in the order their clocks read, earliest first: left to right,
    then top to bottom.

    Sorted on the offset, which is the same thing: every face renders one
    instant, so the time one reads is that instant plus its offset, and the
    westernmost zone is the one furthest behind. Faces that share an offset
    keep the order they were typed in -- the sort is stable in both ports for
    exactly that reason -- which is how UTC and GMT, two faces because they
    are labelled differently, stay where you put them.

    Redone as the clock runs, like the merging: an offset is a property of the
    instant, so a zone entering daylight saving slides a place along. Sorting
    on the offset rather than on the time each face reads is what keeps this
    from also changing at every midnight.
    """
    return sorted(faces, key=lambda face: utc_offset(now, face[1]))


def center(s, w, extra_left):
    """Centre s in w columns, the odd column going left or right as told.

    Not "{:^w}", which always leans right: the frame decides, so that a face
    centred inside a grid that has already leaned right leans left here and
    the two cancel. Nothing wider than w is padded, and nothing is cut.
    """
    pad = max(0, w - len(s))
    left = (pad + 1) // 2 if extra_left else pad // 2
    return " " * left + s + " " * (pad - left)


def truncate(s, n):
    """Cut s to n characters."""
    n = max(0, n)
    return s if len(s) <= n else s[:n]


# Weekday names, Sunday first to match Go's time.Weekday. A table rather than
# strftime("%a"), which follows the locale -- "lun." in a French shell -- where
# Go's Format is fixed English. Hard-coding it keeps the two renders identical
# on every machine.
DAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")

# The width of "Mon 05:02:41.901", against the bare readout's 12. A face
# narrower than this would shove the columns to its right out of true, so a
# very low CLOCK_CELL_RATIO loses the weekday rather than the alignment.
DAY_COLS = 16


def show_weekday(faces, now, when):
    """Whether the readouts carry a weekday.

    Under auto, only when the faces on screen disagree about the date: a
    weekday under every clock is noise when they all fall on the same one.
    Nothing off screen is consulted, so the clock never asserts a date for a
    zone it is not drawing -- name `local` in the list to compare against your
    own day.

    Up to three dates can be on screen at once, since UTC-12 to UTC+14 spans
    26 hours and so crosses two midnights.

    A face too narrow to hold the weekday loses it whatever the setting says,
    since the alternative is a grid out of true.
    """
    if when == "never" or COLS < DAY_COLS or not faces:
        return False
    if when == "always":
        return True
    first = in_zone(now, faces[0][1]).date()
    return any(in_zone(now, zone).date() != first for _, zone in faces)


def digital(t, weekday):
    """The clock under one face: 12 characters, or 16 with a weekday on it."""
    clock = f"{t:%H:%M:%S}.{t.microsecond // 1000:03d}"
    if not weekday:
        return clock
    # isoweekday is Mon=1..Sun=7, and % 7 turns that into Go's Sun=0
    return f"{DAY_NAMES[t.isoweekday() % 7]} {clock}"


def chunk_count(n, per_row):
    """How many rows of faces per_row produces."""
    return -(-n // per_row)


def frame_height(chunks, vgap):
    """The rows a grid occupies: each chunk is a face, its zone name and its
    digital line, with vgap blank rows between chunks."""
    return chunks * (ROWS + 2) + vgap * (chunks - 1)


def gap_floor(span, percent, least):
    """The gap a layout will not go below.

    An even fill starts from the least the grid can be packed to and grows;
    a percentage is that share of the whole span, and is the whole answer. A
    span of 0 is a window that could not be measured, where a percentage of
    nothing is nothing useful, so the least stands.
    """
    if percent is None or span <= 0:
        return least
    return span * percent // 100


def spread(count, size, span, least, percent, align):
    """Lay count blocks of size across span: the gap between two of them, and
    the margin in front of the first.

    An even fill counts the margins as gaps too -- count blocks make count + 1
    spaces, two of them against the edges -- and gives each an equal share of
    what the blocks leave. Sharing between the blocks alone would hand every
    spare column to the gutters and press the outer clocks flat against the
    borders, which is the one arrangement nobody wants.

    The share stops at the size of a block: past that the clocks read as
    scattered rather than as a group, so on a wide window the extra goes to
    the margins and the clocks stay a cluster in the middle. It never drops
    below `least` either, so a window just big enough for the grid gets the
    packed layout rather than a squeeze.

    A percentage fixes the gap outright and leaves everything else to the
    margin, so the alignment has something to work with. An unmeasurable span
    keeps the packed layout this clock had before any of it was adjustable:
    least gap, no margin.

    Comes back as (gap, extra, margin, leaned). A centred layout cannot halve
    an odd slack into two margins, but a gutter can swallow the odd column
    instead: `extra` widens one gutter by one, and the margins come out equal.
    That only works where there is a gutter, so a single clock still has to
    lean, and `leaned` says it did -- the caller's cue to lean the other way
    on the next rounding, so the two cancel rather than adding up.
    """
    gap = gap_floor(span, percent, least)
    if span <= 0:
        return gap, 0, 0, False
    if percent is None:
        share = max(0, span - count * size) // (count + 1)
        gap = min(max(least, share), size)
    slack = max(0, span - count * size - gap * (count - 1))
    extra = 0
    if align == "center" and percent is None and count > 1 and slack % 2 == 1:
        # A padding asked for by name is left exactly as asked for; only the
        # even fill, which chose this gap itself, may nudge one gutter.
        extra, slack = 1, slack - 1
    if align == "center":
        return gap, extra, slack // 2, slack % 2 == 1
    return gap, 0, (slack if align in ("right", "bottom") else 0), False


def help_rows():
    """The key list, one row per key."""
    return [f"{key:<{HOTKEY_COL}}{what}" for key, what in HOTKEYS]


# One frame's spacing, both axes: the blank columns between faces and rows
# between rows of faces, the one gutter each axis widens to swallow an odd
# column, the margins before the first of each, and which way to lean a label
# that will not centre exactly.
Layout = collections.namedtuple(
    "Layout", "gap extra left vgap vextra top extra_left"
)


def frame(faces, now, per_row, color, day_when, lay):
    """Draw the whole grid: faces left to right, wrapping every per_row.

    A short last row keeps the gutters and margin of a full one, so the
    columns stay lined up -- including the widened gutter, which sits at a
    fixed place in the row rather than at whatever the last one happens to be.
    """
    indent = " " * lay.left

    def gutter(i):
        """The i'th gap of a row: the widened one is always the last of a full
        row, so a short row's gutters still line up with the row above."""
        return " " * (lay.gap + (1 if lay.extra and i == per_row - 2 else 0))

    def row(parts):
        out = indent + parts[0]
        for i, part in enumerate(parts[1:]):
            out += gutter(i) + part
        return out
    weekday = show_weekday(faces, now, day_when)
    chunks = chunk_count(len(faces), per_row)
    rows = [""] * lay.top
    for ci in range(chunks):
        chunk = faces[ci * per_row : (ci + 1) * per_row]
        if ci > 0:
            wide = 1 if lay.vextra and ci == chunks - 1 else 0
            rows.extend([""] * (lay.vgap + wide))
        times = [in_zone(now, z) for _, z in chunk]
        drawn = [face(t, color) for t in times]
        rows.extend(row(list(line)) for line in zip(*drawn))
        rows.append(
            row([center(truncate(label, COLS), COLS, lay.extra_left) for label, _ in chunk])
        )
        rows.append(
            row([center(digital(t, weekday), COLS, lay.extra_left) for t in times])
        )
    return rows


def modal_box(content):
    """Draw content inside a one-line border.

    Used for both the key list and the startup quit hint so the two read as
    the same kind of thing: a modal overlaid on the clocks, not part of the
    grid underneath it.
    """
    width = max(len(c) for c in content)
    box = [f"┌{'─' * (width + 2)}┐"]
    box.extend(f"│ {c:<{width}} │" for c in content)
    box.append(f"└{'─' * (width + 2)}┘")
    return box


def center_modal(box, term_cols, term_rows):
    """Where to place a modal in the middle of a term_cols x term_rows window.

    ok is False when it does not fit, the same trade the key list already made
    against a narrow window: no modal beats a wrapped or clipped one. A window
    that cannot be measured has nowhere settled to put one, so that is also a
    no.
    """
    width, height = len(box[0]), len(box)
    if term_cols <= 0 or term_rows <= 0 or width > term_cols or height > term_rows:
        return 0, 0, False
    return (term_rows - height) // 2, (term_cols - width) // 2, True


def overlay_modal(rows, box, top, left):
    """Stamp box onto rows at (top, left).

    Extends rows with blank lines so the box always lands intact regardless
    of what the grid drew there.
    """
    out = list(rows) + [""] * (top + len(box) - len(rows))
    for i, line in enumerate(box):
        r = top + i
        out[r] = splice_row(out[r], left, len(line), line)
    return out


def splice_row(row, col, width, insert):
    """Overwrite the visible columns [col, col + width) of row with insert.

    insert is plain text, never coloured itself, while whatever ANSI colour
    row carried outside that span is preserved and correctly resumed on the
    far side. row may already be full of colour escapes (a face's hand can
    pass under where a modal lands) or may have none at all (--color=never,
    or a redirected frame); either way nothing outside [col, col + width)
    changes.
    """
    before, after = [], []
    active = ""  # the last SGR escape seen so far, "" meaning none yet
    start_active = end_active = ""
    start_captured = end_captured = False
    visible = 0
    i, n = 0, len(row)
    while i < n:
        if row[i] == "\x1b":
            j = row.index("m", i) + 1
            seq = row[i:j]
            active = seq
            if visible < col:
                before.append(seq)
            elif visible >= col + width:
                after.append(seq)
            i = j
            continue
        if not start_captured and visible >= col:
            start_captured, start_active = True, active
        if not end_captured and visible >= col + width:
            end_captured, end_active = True, active
        if visible < col:
            before.append(row[i])
        elif visible >= col + width:
            after.append(row[i])
        visible += 1
        i += 1
    if not start_captured:
        start_active = active
    if not end_captured:
        end_active = active
    if visible < col:
        before.append(" " * (col - visible))
    result = "".join(before)
    if start_active and start_active != DEFAULT_FG:
        result += DEFAULT_FG
    result += insert
    if after:
        if end_active:
            result += end_active
        result += "".join(after)
    return result


def fold(text, width):
    """Break text onto lines of at most width, on spaces where it can be.

    A word with nowhere to break -- a window narrower than "--per-row" -- is
    cut instead, since the alternative is a line that wraps itself and scrolls
    the screen out from under the next repaint.
    """
    rows, line = [], ""
    for word in text.split(" "):
        while width > 0 and len(word) > width:
            if line:
                rows.append(line)
                line = ""
            rows.append(word[:width])
            word = word[width:]
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= width:
            line += " " + word
        else:
            rows.append(line)
            line = word
    if line:
        rows.append(line)
    return rows


def complaint(text, term_cols, term_rows, halign):
    """The frame that says why there are no clocks, when the window is too
    small to hold them.

    Folded to the window and cut to it, and placed the way the quit hint is:
    centred with the clocks, hard left under any other alignment.
    """
    width = term_cols if term_cols > 0 else len(text)
    rows = fold(text, width)
    if term_rows > 0:
        del rows[term_rows:]
    if halign == "center":
        rows = [" " * ((width - len(r)) // 2) + r for r in rows]
    if term_rows > 0:
        rows = [""] * ((term_rows - len(rows)) // 2) + rows
    return rows


def use_color(when):
    """Whether to colour the hands.

    auto colours a terminal and leaves a pipe or a file plain, so a redirected
    frame stays the plain text the difftest compares. NO_COLOR is the
    cross-tool convention for "never, from the environment"; an explicit
    --color=always overrules it, since that is the point of saying always.
    """
    if when != "auto":
        return when == "always"
    return sys.stdout.isatty() and os.environ.get("NO_COLOR", "") == ""


def env_count(name):
    """A positive count from the environment, 0 when unset or junk."""
    try:
        return parse_count(os.environ.get(name, ""), name, 100000)
    except ClockError:
        return 0


def term_size():
    """The terminal as (columns, rows); 0 means "could not tell".

    COLUMNS and LINES win when set, both because that is the shell convention
    and because it gives the diff harness a way to pin the layout. Deliberately
    not shutil.get_terminal_size, which invents 80x24 when it cannot tell -- a
    pipe has to stay distinguishable from an 80-column window.
    """
    cols, rows = env_count("COLUMNS"), env_count("LINES")
    if not cols or not rows:
        try:
            size = os.get_terminal_size(sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            return cols, rows
        cols = cols or size.columns
        rows = rows or size.lines
    return cols, rows


def fit_per_row(want, n, term_cols, gap):
    """Reduce the requested faces-per-row to what the window can hold.

    Wrapping is what actually breaks the display: a wrapped line desynchronises
    the cursor rewind and the frame smears.
    """
    want = min(want, n)
    if term_cols <= 0:
        return want  # not a terminal: honour what was asked for
    max_fit = (term_cols + gap) // (COLS + gap)
    if max_fit < 1:
        raise ClockError(
            f"terminal is {term_cols} columns wide and one clock face needs "
            f"{COLS}; widen the window, or lower --cell-ratio"
        )
    return min(want, max_fit)


def fit_height(chunks, term_rows, vgap):
    """Reject a grid taller than the window.

    Too tall scrolls, and scrolling desynchronises the rewind exactly as
    wrapping does -- but here the fix is to raise --per-row, not lower it.
    """
    height = frame_height(chunks, vgap)
    if term_rows > 0 and height > term_rows:
        raise ClockError(
            f"{chunks} rows of clocks need {height} lines and this terminal "
            f"has {term_rows}; raise --per-row, or name fewer zones"
        )


def auto_scale(want_per_row, num_faces, cols, lines, hpad, vpad):
    """What --scale auto resolves to every frame.

    The largest ROWS (and its matching COLS) that lets num_faces fit
    cols x lines at no more than want_per_row per row, using no more than
    hpad/vpad's own minimum gap on each axis -- the same floor fit_per_row
    and fit_height already enforce, so a maximised face never asks for less
    room than an explicit --hpad would once drawn. Larger ROWS can only ever
    need as much or more space (a wider face fits no more per row, and a
    taller one needs no fewer lines), so the first size that fits, searched
    from the top down, is the largest one that does.

    -n auto passes num_faces itself as want_per_row -- no cap at all, in
    effect, since fit_per_row already clamps want to num_faces on its own --
    rather than searching per-row counts separately. fit_per_row always uses
    the most faces a row can hold up to the cap, which is also the fewest
    chunks (and so the least height) any per-row choice at that ROWS could
    need, so an uncapped want already finds whichever per-row count each
    candidate ROWS fits best through, without a second search: capping lower
    could only ever force more chunks than that ROWS needed, never fewer.

    Sets the module-level ROWS and COLS to the winner; if nothing in range
    fits, it leaves them at MIN_ROWS_N so the fit_per_row/fit_height call
    right after this one reports why.

    An odd ROWS (or COLS) is preferred within SYMMETRY_WINDOW of the largest
    fit: an odd count centres the face's true axis exactly in the middle of a
    character cell, while an even one centres it exactly on the boundary
    between two cells, where no placement of a major tick's two dots can be
    symmetric -- see the axis-safe tick comment on spoke(), and
    ARCHITECTURE.md, for why that is otherwise unavoidable. Every smaller
    candidate already fits, by the same monotonicity argument above, so
    trading a handful of rows for one with both counts odd costs nothing but
    those few rows -- capped at SYMMETRY_WINDOW, so a face that never finds
    one does not shrink indefinitely looking.
    """
    global ROWS, COLS
    best = 0
    for n in range(MAX_ROWS_N, MIN_ROWS_N - 1, -1):
        ROWS = n
        COLS = math.floor(n * CELL_RATIO + 0.5)
        try:
            per_row = fit_per_row(want_per_row, num_faces, cols, gap_floor(cols, hpad, GAP))
        except ClockError:
            continue
        try:
            fit_height(chunk_count(num_faces, per_row), lines, gap_floor(lines, vpad, VGAP))
        except ClockError:
            continue
        best = n
        break
    if best == 0:
        ROWS = MIN_ROWS_N
        COLS = math.floor(MIN_ROWS_N * CELL_RATIO + 0.5)
        return

    row_fallback, col_fallback = -1, -1
    for n in range(best, max(best - SYMMETRY_WINDOW, MIN_ROWS_N - 1), -1):
        c = math.floor(n * CELL_RATIO + 0.5)
        if n % 2 == 1 and c % 2 == 1:
            ROWS, COLS = n, c
            return
        if n % 2 == 1 and row_fallback < 0:
            row_fallback = n
        if c % 2 == 1 and col_fallback < 0:
            col_fallback = n
    # No candidate had both odd: an odd ROWS keeps the 3/9 o'clock ticks
    # symmetric, which is the more noticeable pair, so it wins over an odd
    # COLS alone.
    if row_fallback >= 0:
        ROWS = row_fallback
    elif col_fallback >= 0:
        ROWS = col_fallback
    else:
        ROWS = best
    COLS = math.floor(ROWS * CELL_RATIO + 0.5)


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


def pending_keys():
    """Whatever is waiting on stdin, or b"" if nothing is.

    Never blocks, and assumes cbreak mode, where a key arrives without a
    Return behind it. Handed back one byte at a time rather than tested for a
    q, because two spaces in one read have to toggle the hold twice, exactly
    as the Go port's one-byte-per-channel-send loop does.
    """
    if not select.select([sys.stdin], [], [], 0)[0]:
        return b""
    return os.read(sys.stdin.fileno(), 64)


def _terminate(_signum, _frame):
    """Turn SIGTERM into an ordinary unwind, so the cursor comes back.

    Left to its default, SIGTERM kills the process outright: the finally below
    never runs, and the caller is handed a terminal with no cursor and echo
    still off. Go's port already selects on SIGTERM for the same reason.
    """
    raise SystemExit(0)


def run(argv):
    """Everything that can fail happens before the terminal is touched."""
    global ROWS, CELL_RATIO, COLS
    frozen = freeze()
    (
        want_per_row,
        zone_list,
        color_when,
        day_when,
        geometry,
        quiet,
        cell_ratio_flag,
        scale_flag,
        scale_auto,
        per_row_auto,
    ) = parse_args(argv)
    halign, valign, hpad, vpad = geometry
    zones = resolve_zones(zone_list, frozen or datetime.now(timezone.utc))

    color = use_color(color_when)

    # --cell-ratio wins over CLOCK_CELL_RATIO, which wins over the default.
    CELL_RATIO = cell_ratio_flag if cell_ratio_flag is not None else env_cell_ratio()

    # --scale resizes the whole face, keeping the same shape: ROWS moves and
    # COLS follows it, through the cell-ratio arithmetic above. --scale auto
    # instead re-solves both every frame, in the main loop, against whatever
    # the terminal measures to.
    if not scale_auto:
        scale = scale_flag if scale_flag is not None else 1.0
        ROWS = math.floor(DEFAULT_ROWS_N * scale + 0.5)
        if ROWS < MIN_ROWS_N or ROWS > MAX_ROWS_N:
            raise ClockError(
                f"--scale {scale:g} makes each face {ROWS} rows tall; want "
                f"{MIN_ROWS_N} to {MAX_ROWS_N} rows, roughly --scale "
                f"{MIN_ROWS_N / DEFAULT_ROWS_N:.2f} to --scale {MAX_ROWS_N / DEFAULT_ROWS_N:.2f}"
            )
        COLS = math.floor(ROWS * CELL_RATIO + 0.5)

    # -n auto's whole point is choosing whatever per-row count lets --scale
    # auto grow the face furthest; with a fixed --scale there is no face size
    # left for it to affect, so it falls back to the plain default cap.
    if per_row_auto and not scale_auto:
        want_per_row = DEFAULT_PER_ROW
        per_row_auto = False

    # A pinned clock is a still of one instant, and an undated still records
    # half of it, so the weekday goes under every face unless --day says
    # otherwise. Live, auto keeps it for the clocks that actually disagree.
    if not day_when:
        day_when = "always" if frozen is not None else "auto"
    # The instant the display is holding, or None when it runs live. Holding
    # repaints as usual rather than idling, so a resize still reflows the grid
    # -- it is the clock that stops, not the drawing.
    held = None
    help_on = False
    # Off the wall clock, not the frame's: a clock pinned with CLOCK_FREEZE
    # never advances, and the hint still has to give up after three seconds.
    flash_until = time.monotonic() + FLASH_SECONDS

    signal.signal(signal.SIGTERM, _terminate)

    # On a terminal, take the alternate screen and paint from its top corner.
    # Relative rewind cannot survive a resize: the terminal rewraps the frame
    # already on screen, so rows that were one physical line become two, the
    # ESC[nA lands inside the old frame, and its upper half is left behind --
    # CLEAR_BELOW only ever clears downwards. Homing to a screen we own makes
    # the frame's position independent of what happened to the last one. Piped
    # output keeps the rewind, which costs nothing there and keeps the byte
    # stream the difftest compares unchanged.
    full_screen = sys.stdout.isatty()

    # A pinned clock redirected to a file is the diff harness: one frame and
    # out. On a terminal there is someone watching, so it stays up instead --
    # quitting would restore the screen and take the frame with it.
    one_shot = frozen is not None and not full_screen
    # A pinned clock draws one frame unless CLOCK_FRAMES asks for a sequence;
    # `drawn` is which frame of it this is, and so how far the instant has
    # moved from the pinned one.
    frames, step = sequence(frozen)
    drawn = 0

    sys.stdout.write((ENTER_ALT if full_screen else "") + HIDE_CURSOR)
    height = 0
    # Which faces there are, and in what order, changes only when some zone's
    # offset changes -- and a tz transition always lands on a whole second, so
    # recomputing once a second cannot miss one. See the loop below.
    face_second, faces = None, None
    try:
        with quiet_terminal() as interactive:
            while True:
                now = frozen if frozen is not None else datetime.now(timezone.utc)
                if one_shot:
                    now += step * drawn
                if held is not None:
                    now = held

                # re-measure every frame rather than trapping SIGWINCH: one
                # ioctl per 19ms is nothing beside redrawing the faces, it also
                # picks up a changed COLUMNS, and under PEP 475 a signal
                # handler would interact with the sleep below and drift out of
                # step with the Go port's loop.
                cols, lines = term_size()

                # Unlike the size, this is not re-measured every frame. Merging
                # and ordering both turn on the zones' offsets at `now`, which
                # move only at a tz transition, and a transition happens on a
                # whole second -- so a second is the coarsest interval that
                # cannot skip one, and at 19ms frames that is ~50x less work.
                # Floored, not truncated: Go's Unix() floors, where int()
                # would truncate, so before 1970 the two would hold different
                # numbers here. Nothing drawn would differ -- the key decides
                # only when the recompute lands, and the one second the two
                # would disagree about, the one straddling the epoch, has no
                # zone changing offset inside it -- but a key that is the same
                # number in both needs no such argument to be trusted.
                second = math.floor(now.timestamp())
                if second != face_second:
                    face_second = second
                    faces = order_faces(merge_zones(zones, now), now)

                if scale_auto:
                    if per_row_auto:
                        want_per_row = DEFAULT_PER_ROW  # overridden below whenever there is a window to measure
                    if cols > 0 and lines > 0:
                        if per_row_auto:
                            want_per_row = len(faces)  # no real cap: see auto_scale's own comment
                        auto_scale(want_per_row, len(faces), cols, lines, hpad, vpad)
                    else:
                        # Nothing measurable to fill, so there is nothing to
                        # solve -- same as any other window that cannot be
                        # measured.
                        ROWS = DEFAULT_ROWS_N
                        COLS = math.floor(DEFAULT_ROWS_N * CELL_RATIO + 0.5)

                # The key list and the startup hint are the same kind of
                # thing -- a modal laid over the clocks -- so only one shows
                # at a time, and the key list, being asked for, wins over a
                # hint that is already redundant with the "q" line in it.
                fits = False
                try:
                    per_row = fit_per_row(
                        want_per_row, len(faces), cols, gap_floor(cols, hpad, GAP)
                    )
                    chunks = chunk_count(len(faces), per_row)
                    fit_height(chunks, lines, gap_floor(lines, vpad, VGAP))
                except ClockError as exc:
                    # A window dragged smaller than the clocks need is
                    # something the reader can undo, so say what is wrong and
                    # keep measuring: the next frame that fits draws itself.
                    # Redirected output has no window to resize and still
                    # fails outright, which is what the diff harness compares.
                    if not full_screen:
                        raise
                    rows = complaint(str(exc), cols, lines, halign)
                else:
                    fits = True

                content = None
                if fits and full_screen and help_on:
                    content = help_rows()
                elif not quiet and full_screen and time.monotonic() < flash_until:
                    content = [FLASH]
                show_modal = False
                if content is not None:
                    modal = modal_box(content)
                    modal_top, modal_left, show_modal = center_modal(modal, cols, lines)

                if fits:
                    # Any lean left over from the grid is answered by the
                    # labels leaning the other way, so the frame comes out no
                    # more than a column off centre -- and with a gutter to
                    # swallow the odd column, dead centre.
                    gap_n, extra, left, leaned = spread(
                        per_row, COLS, cols, GAP, hpad, halign
                    )
                    vgap, vextra, top, _ = spread(
                        chunks, ROWS + 2, lines, VGAP, vpad, valign
                    )
                    lay = Layout(gap_n, extra, left, vgap, vextra, top, leaned)
                    rows = frame(faces, now, per_row, color, day_when, lay)
                if show_modal:
                    rows = overlay_modal(rows, modal, modal_top, modal_left)

                # Repaint in one write. CLEAR_EOL wipes a longer previous
                # line, CLEAR_BELOW a taller previous frame, so the grid
                # reshapes itself when the window changes.
                if full_screen:
                    # no trailing newline: a frame exactly as tall as the
                    # window would otherwise scroll itself off by one line
                    body = "\n".join(r + CLEAR_EOL for r in rows)
                    sys.stdout.write(HOME + body + CLEAR_BELOW)
                else:
                    rewind = f"\x1b[{height}A" if height else ""
                    sys.stdout.write(
                        rewind
                        + "".join(r + CLEAR_EOL + "\n" for r in rows)
                        + CLEAR_BELOW
                    )
                sys.stdout.flush()
                height = len(rows)

                if one_shot:
                    drawn += 1
                    if drawn >= frames:
                        break
                    continue
                if interactive:
                    quitting = False
                    for key in pending_keys():
                        if key in b"qQ":
                            quitting = True
                        elif key == b" "[0]:
                            held = None if held is not None else now
                        elif key in b"hH?":
                            help_on = not help_on
                    if quitting:
                        break
                time.sleep(TICK - (time.monotonic() % TICK))
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(SHOW_CURSOR + (LEAVE_ALT if full_screen else ""))
        sys.stdout.flush()


def main():
    try:
        run(sys.argv[1:])
    except HelpRequested:
        sys.stdout.write(USAGE)
    except ClockError as exc:
        sys.stderr.write(f"clock: {exc}\n")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
