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
             [ZONES]

  ZONES              comma-separated zone names, described below; default is
                      your local zone
  -n, --per-row N    clocks per row before wrapping (default 3, reduced to fit)
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
the window, and then the alignment decides where the grid sits.

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
GAP = 3  # fewest blank columns between adjacent faces
VGAP = 1  # fewest blank rows between rows of faces

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
        share to a single dot at r1, instead of ending in a flat, two-dot-wide
        butt.
        """
        sin_a, cos_a = math.sin(angle), math.cos(angle)
        x0, y0 = cx + rx * r0 * sin_a, cy - ry * r0 * cos_a
        x1, y1 = cx + rx * r1 * sin_a, cy - ry * r1 * cos_a
        tip = r1 - (r1 - r0) * HAND_TAPER if thick and point else r1
        tx, ty = cx + rx * tip * sin_a, cy - ry * tip * cos_a
        # two dots thick straddles the axis, so the spoke centres on it
        for off in (-0.5, 0.5) if thick else (0.0,):
            dx, dy = off * cos_a, off * sin_a
            # the offset shrinks to nothing at the tip, not the base: that is
            # what tapers the two edges together into a point
            canvas.line(x0 + dx, y0 + dy, tx, ty, layer)
        if thick and point:
            canvas.line(tx, ty, x1, y1, layer)

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
    per_row = DEFAULT_PER_ROW
    color_when = "auto"
    day_when = ""  # unset: run() picks it, since a pinned clock differs
    halign, valign = "center", "center"
    hpad, vpad = None, None  # None is the even fill
    quiet = False
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
                per_row = parse_count(val, "--per-row", MAX_PER_ROW)
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
            elif name in ("halign", "valign", "hpad", "vpad"):
                # These four want a value, and take it either way round, as
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
                else:
                    vpad = parse_pad(name, val)
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
            per_row = parse_count(rest, "-n", MAX_PER_ROW)
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

# US ZIP prefixes to time zones, run-length encoded as fixed four-byte records
# "NNNc": the 3-digit prefix a run starts at, then a zone letter from ZIP_ZONES.
# A run reaches to the next record's prefix, and the last run to 999; "-" marks
# prefixes the Postal Service has not assigned.
#
# Derived from US Census ZCTA centroids (public domain) via
# timezone-boundary-builder (ODbL). Generated by tools/genzips.py into both
# clock.py and clock.go in one pass -- never edit it by hand, or the two
# implementations will drift.
ZIP_RUNS = "000-006R010E055-056E090-100E192-193E213-214E269-270E311-312E324C326E332-333E340-341E343-344E345-346E348-349E350C353-354C374E375-376E380C398E399-400E419-420C425E427C428-430E459-460E463C465E476C478E500C509-510C517-520C529-530C533-534C536-537C552-553C555-556C568-570C576M578-580C586M587C589-590M600C621-622C632-633C642-644C649-650C659-660C663-664C682-683C691M692C693M694-700C702-703C709-710C715-716C732-734C742-743C771-772C799M817-820M835P836M838P839-840M842-843M848-850Z854-855Z858-859Z861-863Z865M866-870M872-873M876-877M885-890P892-893P896-897P899-900P901-902P909-910P929-930P938-939P942-943P962-967H969G970P979M980P987-988P995A"  # zip-runs: generated by tools/genzips.py

# Phoenix is separate from Denver because Arizona does not observe daylight
# saving, and Adak from Anchorage because the western Aleutians run an hour
# further behind.
ZIP_ZONES = {
    "A": "America/Anchorage",
    "C": "America/Chicago",
    "D": "America/Adak",
    "E": "America/New_York",
    "G": "Pacific/Guam",
    "H": "Pacific/Honolulu",
    "M": "America/Denver",
    "P": "America/Los_Angeles",
    "R": "America/Puerto_Rico",
    "S": "Pacific/Pago_Pago",
    "Z": "America/Phoenix",
}

# The ZIPs a 3-digit prefix gets wrong. A prefix that straddles a zone boundary
# has to round to one side, so every ZIP on the losing side is an hour or more
# out; these records name each of them exactly. Consulted only when all five
# digits were given, since three cannot say which ZIP is meant.
#
# A record is "PPPcNN" -- prefix, zone letter from ZIP_ZONES, and how many
# two-digit suffixes follow -- then that many suffixes, ascending:
#
#     373C38 01 02 07 ...        (spaces for clarity only)
#
# Writing the prefix once per group rather than once per ZIP is what keeps this
# smaller than a flat table of five-digit records. The stride varies, so this is
# a short forward scan rather than a binary search -- there are only thirty-odd
# groups, and it runs once per zone at startup, never per frame.
#
# Sourced from the same Census ZCTA centroids as ZIP_RUNS, so it covers every
# ZIP that has a ZCTA; PO-box and single-building ZIPs have none, and still
# fall back to their prefix. Generated by tools/genzips.py into both clock.py
# and clock.go in one pass -- never edit it by hand.
ZIP_EXCEPTIONS = "324E0156368E06546367697077373E380203070809101112151617212223252629313233363741435051535461626369707379818591374C0119377C0123401C1311151940434445465270717678426C03022942427E12011618243233404858768488465C03313234475C1614152023253137505152747677798688479C082243485163647778498C230102121521313447485258637073747677818687929396499C12020311152027353847596869575M1032374347515253677477576C0431324648585M052933626469586C023138588M0138677M06333541586162678M0436577879690M082123273033374145691C18012023303235384243515763656667697071692M0411161819798M1021353637383947495153835M0422424749860M1303162031333435404445475354865Z0102967S0199979P03040507995D024647"  # zip-exceptions: generated by tools/genzips.py


def alias_names():
    return " ".join(name for name, _ in ZONE_ALIASES)


def zip_lookup(p3):
    """The zone for a 3-digit ZIP prefix, or "" if unassigned.

    Binary search over fixed-width records; the string comparison is exact
    because zero-padded 3-digit decimals sort lexicographically the way they
    sort numerically, so no integer parsing is involved on either side.
    """
    lo, hi, hit = 0, len(ZIP_RUNS) // 4 - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if ZIP_RUNS[mid * 4 : mid * 4 + 3] <= p3:
            hit = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if hit < 0:
        return ""
    return ZIP_ZONES.get(ZIP_RUNS[hit * 4 + 3], "")


def zip_exception(zip5):
    """The zone for one exact 5-digit ZIP, or "" if the prefix gets it right."""
    prefix, suffix = zip5[:3], zip5[3:]
    i = 0
    while i < len(ZIP_EXCEPTIONS):
        group = ZIP_EXCEPTIONS[i : i + 3]
        letter = ZIP_EXCEPTIONS[i + 3]
        count = int(ZIP_EXCEPTIONS[i + 4 : i + 6])
        body = i + 6
        if group == prefix:
            for k in range(count):
                if ZIP_EXCEPTIONS[body + k * 2 : body + k * 2 + 2] == suffix:
                    return ZIP_ZONES.get(letter, "")
            # fall through: a prefix may own more than one group
        elif group > prefix:
            break  # records are sorted, so no later group can match
        i = body + count * 2
    return ""


def zip_zone(token):
    if len(token) == 5:
        prefix = token[:3]
    elif len(token) == 3:
        prefix = token
    else:
        raise ClockError(
            f'"{token}" is not a US ZIP code; give all five digits, or the first three'
        )
    # an exact ZIP beats its prefix's majority; three digits have only the
    # majority to go on
    name = (zip_exception(token) if len(token) == 5 else "") or zip_lookup(prefix)
    if not name:
        raise ClockError(f"no US time zone is recorded for ZIP codes starting {prefix}")
    try:
        return ZoneInfo(name)
    except Exception:
        raise ClockError(
            f"ZIP {prefix} means {name}, which this system's time zone database lacks"
        ) from None


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
    so this regroups every frame rather than once at startup, and a grid
    crossing a daylight-saving boundary splits itself as it happens.
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

    Redone every frame, like the merging: an offset is a property of the
    instant, so a zone entering daylight saving slides a place along.
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

    Extends rows with blank lines and pads short ones with spaces so the box
    always lands intact regardless of what the grid drew there. rows must be
    plain text -- no ANSI -- which run guarantees by turning colour off for
    any frame a modal is going to be stamped onto, so a modal never has to
    reason about resuming a hand's colour on the far side of it.
    """
    out = list(rows) + [""] * (top + len(box) - len(rows))
    for i, line in enumerate(box):
        r = top + i
        existing = out[r]
        if len(existing) < left:
            existing += " " * (left - len(existing))
        tail = existing[left + len(line):]
        out[r] = existing[:left] + line + tail
    return out


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
            f"{COLS}; widen the window, or lower CLOCK_CELL_RATIO"
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
    frozen = freeze()
    want_per_row, zone_list, color_when, day_when, geometry, quiet = parse_args(argv)
    halign, valign, hpad, vpad = geometry
    zones = resolve_zones(zone_list, frozen or datetime.now(timezone.utc))

    color = use_color(color_when)

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

    sys.stdout.write((ENTER_ALT if full_screen else "") + HIDE_CURSOR)
    height = 0
    try:
        with quiet_terminal() as interactive:
            while True:
                now = frozen if frozen is not None else datetime.now(timezone.utc)
                if held is not None:
                    now = held

                # re-measure every frame rather than trapping SIGWINCH: one
                # ioctl per 19ms is nothing beside redrawing the faces, it also
                # picks up a changed COLUMNS, and under PEP 475 a signal
                # handler would interact with the sleep below and drift out of
                # step with the Go port's loop.
                cols, lines = term_size()
                faces = order_faces(merge_zones(zones, now), now)
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
                    # Colour comes off whenever a modal is about to be
                    # stamped on top: overlay_modal works in plain text, so
                    # nothing under the modal is left carrying a hand's
                    # colour past it.
                    rows = frame(faces, now, per_row, color and not show_modal, day_when, lay)
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
                    break
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
