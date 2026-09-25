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
import tempfile
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

# The release this source belongs to. clock.go and pyproject.toml carry the
# same string, and difftest holds all three together: a clock that cannot say
# what it is turns every bug report into a round trip, and one that says the
# wrong thing is worse than one that says nothing at all.
VERSION = "0.4.2"

# What both ports exit with when the reader goes away -- `clock | head`. 128
# plus SIGPIPE, which is what a shell reports for a filter that died of it;
# see main(), and pipeStatus in clock.go.
PIPE_STATUS = 141

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
  --version          print the version and exit

A zone is an IANA name (Europe/Berlin), a city (Berlin, Seattle), a US state
(Arizona, or its code as US-AZ), a country (Germany, or its code as DE), a
regional abbreviation (ET CT MT PT AKT HT BST IST JST AET ...), or a US ZIP
code (94110). Each place is labelled with the zone it landed in, as
MST (Arizona) or CEST (DE); write a name with a space in quotes,
"New Mexico", or with underscores, New_Mexico. The bare two letters are a
country and never a state: CA is Canada. ET/CT/MT/PT follow daylight saving,
so they read EST or EDT depending on the date; EST/EDT/PST/PDT and the rest
are the fixed offsets, which never shift.

The hands are coloured on a terminal and plain when redirected; NO_COLOR
turns the colour off everywhere. Auto puts a weekday on the readouts only
when the clocks on screen disagree about the date. An even fill spreads the
clocks over the whole window; --hpad 10% sets the gaps instead, as a share of
the window, and then the alignment decides where the grid sits. CLOCK_CELL_RATIO
sets the same thing as --cell-ratio, for when it wants to be set once per
terminal rather than typed every time; the flag wins if both are given.

Space holds the frame still, for a screenshot, h or ? opens the key list, and
r resizes and aligns the clocks while they run. S saves those sizes and the
zones on screen to ~/.config/clock/config, which every clock reads at startup;
CLOCK_CONFIG points somewhere else, and set but empty means no file at all.
Press q or Ctrl+C to quit.

examples:
  clock
  clock ET,PT,UTC
  clock -n 2 ET,PT,UTC
  clock Berlin,Jakarta
  clock Arizona,Boise,"Salt Lake City"
  clock Europe/Berlin,Asia/Tokyo,94110 --per-row 2
"""

# The key list h or ? puts up in a modal. In the order the keys are reached
# for rather than alphabetically, and kept in the same order as clock.go's
# table.
HOTKEYS = (
    ("space", "hold the frame"),
    ("h ?", "toggle this list"),
    ("r", "resize and align the clocks"),
    ("S", "save these sizes and zones"),
    ("q", "quit, or Ctrl+C"),
)
HOTKEY_COL = 8  # where the descriptions start, so the keys get a gutter

# What r puts up: the knobs that can be changed while the clock runs, in the
# order they are listed, and what each one takes -- the same words its flag's
# own error message offers, since a value typed here is read by exactly the
# parser that flag uses. Same order and wording as clock.go's table.
TUNABLES = (
    ("halign", "left, center or right"),
    ("valign", "top, center or bottom"),
    ("hpad", "even, or a share like 10%"),
    ("vpad", "even, or a share like 5%"),
    ("per-row", "auto, or a count like 3"),
    ("scale", "auto, or a number like 1.5"),
    ("cell-ratio", "a number like 2.1"),
)

# Which tunable is which. The frame loop holds the six as the text they were
# given in rather than as parsed values: what the tuner shows, what the
# parsers read and what a saved command line would say are then one string,
# and no number is ever formatted back out -- Go's %g and Python's :g do not
# agree past six significant digits, and a value the reader typed is not ours
# to round anyway.
(
    TUNE_HALIGN,
    TUNE_VALIGN,
    TUNE_HPAD,
    TUNE_VPAD,
    TUNE_PER_ROW,
    TUNE_SCALE,
    TUNE_CELL_RATIO,
) = range(7)
NUM_TUNES = len(TUNABLES)

TUNE_COL = 12  # where the values start, past the longest name plus a gutter

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
# DEFAULT_CELL_RATIO written out, for the tuner and for the flags it hands
# back. Written rather than formatted, so the two ports cannot disagree about
# how a float prints.
DEFAULT_CELL_RATIO_TEXT = "2.1"

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

# "02:53:07.123" -- what digital() writes under every face, and the narrowest a
# face's column can be however small the face itself gets. A face is drawn to
# whatever COLS the scale asks for, but the readout underneath is a fixed
# twelve characters and cannot be shrunk, so the *cell* a face occupies is the
# wider of the two. Without this a narrow enough window lays out by face width
# and then writes a readout straight past the right edge -- which wraps, and a
# wrapped line desynchronises the rewind exactly as fit_per_row exists to
# prevent. The weekday is the same problem solved the other way: at DAY_COLS it
# is dropped rather than widening every cell to hold it.
READOUT_COLS = 12
GAP = 3  # fewest blank columns between adjacent faces
VGAP = 1  # fewest blank rows between rows of faces


# The characters a number may be spelled with here, which is the intersection
# of what the two languages read rather than what either offers. float() takes
# surrounding whitespace and non-ASCII digits -- "\u0661" and "\uff11" are both
# one to it -- where Go's ParseFloat takes neither; ParseFloat takes a
# hexadecimal float, "0x1p2", where float() does not. Every one of those was a
# clock drawn by one implementation and a complaint printed by the other. What
# is left after this is read identically by both, underscores and exponents
# included, so the parse itself can still be each language's own.
NUMBER_CHARS = frozenset("0123456789+-._eE")

# A ceiling on the two knobs that scale a face, which is not about taste: the
# face's width is an integer derived from them, and Python's integers are
# unbounded where Go's are 64 bits. At --cell-ratio 1e19 one clock face needed
# 400000000000000000000 columns here and 9223372036854775807 there -- the same
# refusal, in two different numbers. A million is past any font's aspect ratio
# and any terminal's width, and leaves the arithmetic identical either side.
NUMBER_MAX = 1000000


def positive_number(val):
    """A positive, finite decimal out of a string, or None.

    --scale, --cell-ratio and CLOCK_CELL_RATIO are the same question asked
    three times; this is the one answer, so a value one of them takes cannot
    be a value another refuses.
    """
    if not val or not NUMBER_CHARS.issuperset(val):
        return None
    try:
        value = float(val)
    except ValueError:
        return None
    if math.isnan(value) or math.isinf(value) or value <= 0 or value > NUMBER_MAX:
        return None
    return value


def default_tunes():
    """The six knobs as an untouched clock has them.

    The flag defaults, with CLOCK_CELL_RATIO standing in for the ratio where
    it is usable. That ratio is the only knob that decides whether the face is
    round, and it varies by font and line spacing; raise it if the face looks
    squished, lower it if it bulges sideways. Unlike --cell-ratio, an
    environment variable might be stale or set for some other program, so a
    bad value there is not a user error -- it is simply ignored, the same way
    an unset one is.
    """
    vals = ["center", "center", "even", "even", "auto", "auto", DEFAULT_CELL_RATIO_TEXT]
    env = os.environ.get("CLOCK_CELL_RATIO", "")
    if env and positive_number(env) is not None:
        vals[TUNE_CELL_RATIO] = env
    return vals


def tune_index(name):
    """Which of the six a flag name is.

    Only ever asked about the six names TUNABLES holds, so there is no
    not-found to answer.
    """
    return [t[0] for t in TUNABLES].index(name)


def parse_ratio(val):
    """Read a positive, finite decimal for --cell-ratio."""
    value = positive_number(val)
    if value is None:
        raise ClockError(
            "--cell-ratio wants a positive number up to 1000000, "
            f'e.g. --cell-ratio 2.6, got "{val}"'
        )
    return value


def parse_scale(val):
    """Read a positive, finite decimal for --scale."""
    value = positive_number(val)
    if value is None:
        raise ClockError(
            "--scale wants auto or a positive number up to 1000000, "
            f'e.g. --scale 1.5, got "{val}"'
        )
    return value

# Where the grid sits when it does not fill the window, and what --halign and
# --valign accept. Same order as clock.go's tables, and the wording of the
# error they raise comes off these lists.
HALIGNS = ("left", "center", "right")
VALIGNS = ("top", "center", "bottom")

# A day inside datetime's range at each end. The pinned instant is converted
# into every zone on screen, and a zone can sit 14 hours from UTC, so an
# instant on datetime.min itself overflows the moment it is shown in Los
# Angeles -- where Go's time, which has no such bound, draws it without
# comment. A day of headroom is more than the 14 hours anywhere is away.
FREEZE_FIRST = datetime(1, 1, 2, tzinfo=timezone.utc)
FREEZE_LAST = datetime(9999, 12, 30, 23, 59, 59, 999999, tzinfo=timezone.utc)


class ClockError(Exception):
    """A startup failure to report before the terminal has been touched."""


def _freeze_digits(s):
    """s read as an unsigned decimal integer, or None if it is not one.

    Not str.isdigit(), which is true of "١" and "１" as well as
    "1": the two ports have to accept exactly the same strings, so every
    byte is checked against the ASCII range by hand, the same way
    parse_count and parse_pad already do.
    """
    if not s or not all("0" <= c <= "9" for c in s):
        return None
    return int(s)


def _parse_freeze_instant(value):
    """The strict ISO instant CLOCK_FREEZE accepts, or None if value is not
    one: a four-digit year, two-digit month and day, two-digit hour, minute
    and second, an optional one-to-six-digit fraction, and a literal Z.

    Hand-scanned rather than handed to strptime -- which takes fewer than six
    fractional digits, where Go's time.Parse takes a one-digit month -- so
    that the shape accepted is controlled entirely in this file, and
    clock.go's version of this function can be made to agree with it
    deliberately rather than by coincidence.

    Not range-checked field by field either: the parsed numbers are handed to
    datetime, whose constructor raises on a day like 30 in February, the same
    way Go's time.Date -- which does not raise, only normalizes such a day
    into March -- is made to catch it by reading the fields back and finding
    them changed.
    """
    if len(value) < 20 or value[-1] != "Z":
        return None
    core = value[:-1]
    frac_digits = ""
    if len(core) == 19:
        pass
    elif 21 <= len(core) <= 26 and core[19] == ".":
        frac_digits = core[20:]
        core = core[:19]
    else:
        return None
    if core[4] != "-" or core[7] != "-" or core[10] != "T" or core[13] != ":" or core[16] != ":":
        return None
    year = _freeze_digits(core[0:4])
    month = _freeze_digits(core[5:7])
    day = _freeze_digits(core[8:10])
    hour = _freeze_digits(core[11:13])
    minute = _freeze_digits(core[14:16])
    second = _freeze_digits(core[17:19])
    if None in (year, month, day, hour, minute, second):
        return None
    frac = _freeze_digits(frac_digits) if frac_digits else 0
    if frac is None:
        return None
    microsecond = frac * 10 ** (6 - len(frac_digits))
    try:
        return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_freeze_clock(s):
    """The HH[:MM[:SS]] half of _parse_freeze_clock_utc, or None if s is not
    one.
    """
    fields = s.split(":")
    if len(fields) > 3 or not (1 <= len(fields[0]) <= 2):
        return None
    hour = _freeze_digits(fields[0])
    if hour is None or hour > 23:
        return None
    minute = second = 0
    if len(fields) >= 2:
        if len(fields[1]) != 2:
            return None
        minute = _freeze_digits(fields[1])
        if minute is None or minute > 59:
            return None
    if len(fields) == 3:
        if len(fields[2]) != 2:
            return None
        second = _freeze_digits(fields[2])
        if second is None or second > 59:
            return None
    return hour, minute, second


def _parse_freeze_date(s):
    """The date ahead of a clock time -- "2026-07-22", "2026/07/22", "7/22"
    or "8/22/26" -- as (year, month, day, has_year), or None if s is not one.

    The two separators inside one date must be the same character; mixing
    them, as in "2026-07/22", falls out of the two-field case rather than
    being caught on purpose, since the year then reads as a two-digit month
    and is refused for being neither.

    Three fields read year-month-day when the first is four digits -- the
    only length a year is ever spelled with here -- and month-day-year
    otherwise, with the year itself two digits (2000 added) or four. A first
    field of three digits is neither and is refused either way.
    """
    sep = next((c for c in s if c in "-/"), None)
    if sep is None:
        return None
    fields = s.split(sep)
    if len(fields) == 2:
        if not (1 <= len(fields[0]) <= 2) or not (1 <= len(fields[1]) <= 2):
            return None
        month = _freeze_digits(fields[0])
        day = _freeze_digits(fields[1])
        if month is None or day is None:
            return None
        return None, month, day, False
    if len(fields) != 3:
        return None
    if len(fields[0]) == 4:
        if not (1 <= len(fields[1]) <= 2) or not (1 <= len(fields[2]) <= 2):
            return None
        year = _freeze_digits(fields[0])
        month = _freeze_digits(fields[1])
        day = _freeze_digits(fields[2])
        if None in (year, month, day):
            return None
        return year, month, day, True
    if len(fields[0]) in (1, 2):
        if not (1 <= len(fields[1]) <= 2) or len(fields[2]) not in (2, 4):
            return None
        month = _freeze_digits(fields[0])
        day = _freeze_digits(fields[1])
        year = _freeze_digits(fields[2])
        if None in (month, day, year):
            return None
        if len(fields[2]) == 2:
            year += 2000
        return year, month, day, True
    return None


def _freeze_valid_calendar_date(year, month, day):
    """Whether year-month-day is a real date, independent of any zone -- day
    30 of February is not, whatever clock time or zone rides along with it.
    """
    try:
        datetime(year, month, day)
        return True
    except ValueError:
        return False


def _freeze_reproduces(t, zone, year, month, day, hour, minute, second):
    """Whether t, read back in zone, is exactly the wall clock
    _freeze_local_to_utc was asked to convert.
    """
    lt = in_zone(t, zone)
    return (lt.year, lt.month, lt.day, lt.hour, lt.minute, lt.second) == (
        year,
        month,
        day,
        hour,
        minute,
        second,
    )


def _freeze_local_to_utc(zone, year, month, day, hour, minute, second):
    """A wall-clock reading -- already known to be a real calendar date --
    in zone, converted to the UTC instant it names, deciding a
    daylight-saving edge case by hand rather than leaning on datetime's own
    default: zoneinfo's fold and Go's time.Date do not agree with each other
    on a reading a spring-forward skips entirely, so this is the one place
    that disagreement could reach the frame drawn, and both ports implement
    this same explicit rule instead of either default.

    The offset a full day before and a full day after settle it. Equal,
    there is no transition anywhere near this reading and the obvious
    instant is the answer -- true on all but at most two calls a year, per
    zone. Unequal, one transition sits somewhere in that two-day window;
    both candidate instants, one built from each offset, are checked by
    converting back into zone and comparing against what was asked for. Both
    matching is a fall-back reading that happened twice, resolved to the
    earlier of the two -- the offset still in effect right up to the
    transition. Neither matching is a spring-forward reading that never
    happened at all, resolved as though the spring-forward had already gone
    -- the later offset.
    """
    naive = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    off_prev = utc_offset(naive - timedelta(days=1), zone)
    off_next = utc_offset(naive + timedelta(days=1), zone)
    if off_prev == off_next:
        return naive - timedelta(seconds=off_prev)
    cand_prev = naive - timedelta(seconds=off_prev)
    cand_next = naive - timedelta(seconds=off_next)
    valid_prev = _freeze_reproduces(cand_prev, zone, year, month, day, hour, minute, second)
    valid_next = _freeze_reproduces(cand_next, zone, year, month, day, hour, minute, second)
    if valid_prev and valid_next:
        return cand_prev if cand_prev < cand_next else cand_next
    if valid_prev:
        return cand_prev
    return cand_next


def _freeze_date(zone, year, month, day, hour, minute, second):
    """One specific instant in zone, or None if the day does not exist in
    that month -- day 30 of February, say -- checked before asking what UTC
    instant it names in that zone.
    """
    if not _freeze_valid_calendar_date(year, month, day):
        return None
    return _freeze_local_to_utc(zone, year, month, day, hour, minute, second)


def _freeze_closest_day(now, zone, hour, minute, second):
    """An undated clock time in zone, resolved to whichever of yesterday,
    today or tomorrow -- by zone's own calendar, not UTC's -- lands closest
    to now. A tie favors today: today is checked first and only a strictly
    closer candidate replaces it.
    """
    local_now = in_zone(now, zone)
    y, m, d = local_now.year, local_now.month, local_now.day
    best = _freeze_local_to_utc(zone, y, m, d, hour, minute, second)
    best_diff = abs(best - now)
    base_date = datetime(y, m, d, tzinfo=timezone.utc)  # a pure calendar calculator
    for days in (-1, 1):
        cd = base_date + timedelta(days=days)
        candidate = _freeze_local_to_utc(zone, cd.year, cd.month, cd.day, hour, minute, second)
        diff = abs(candidate - now)
        if diff < best_diff:
            best, best_diff = candidate, diff
    return best


# How far from now's year _freeze_closest_year looks for a year the given
# month and day exist in. Only February 29 can be missing from a year at
# all, and the longest it is ever missing for is eight years -- 1900 was not
# a leap year, between 1896 and 1904, which both were -- so searching this
# far always finds a February 29 if the true closest one lies outside the
# plain +-1 year that every other date is already found within.
_FREEZE_YEAR_DELTAS = (0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5, -6, 6, -7, 7, -8, 8)


def _freeze_closest_year(now, zone, month, day, hour, minute, second):
    """A clock time in zone on a month and day with no year, resolved to
    whichever year, among _FREEZE_YEAR_DELTAS away from now's year in
    zone's own calendar, lands closest to now -- skipping a year the day
    does not exist in, which for any month and day but February 29 is none
    of them. None only if no year in range has the day, which for every
    month and day but February 29 means the date does not exist regardless
    of year.
    """
    y = in_zone(now, zone).year
    best = best_diff = None
    for delta in _FREEZE_YEAR_DELTAS:
        cy = y + delta
        if not _freeze_valid_calendar_date(cy, month, day):
            continue
        candidate = _freeze_local_to_utc(zone, cy, month, day, hour, minute, second)
        diff = abs(candidate - now)
        if best is None or diff < best_diff:
            best, best_diff = candidate, diff
    return best


def _parse_freeze_clock_zone(value, now):
    """A clock time in some zone, with an optional date ahead of it --
    "15:30 UTC", "15:30 PT", "2026-07-22 15:30 UTC", "2026/07/22 15:30 UTC"
    or "7/22 15:30 PT" -- or None if value is not shaped like this format at
    all. The zone is anything resolve_zone accepts: an alias, an IANA name,
    a fixed offset abbreviation, a country code, a US state, a city, with
    underscores for any space in its name, since a space is what separates
    the date, the clock and the zone -- and resolve_zone
    raises its own ClockError, which is left to propagate rather than
    caught, once a date and a clock have already parsed and the trailing
    word is clearly meant as a zone.

    Fills in whatever the date left out: no date at all leaves the day
    itself open, and a date with no year leaves the year open. What is left
    open resolves to whichever candidate, by the wall clock right now, lands
    closest to this instant -- the reading needs no date, or no year, typed
    at all for the moment that is happening soon, whichever side of midnight
    or new year's it falls on, in that zone's own calendar.
    """
    tokens = value.split(" ")
    if len(tokens) == 2:
        date_part, clock_part, zone_part = "", tokens[0], tokens[1]
    elif len(tokens) == 3:
        date_part, clock_part, zone_part = tokens
    else:
        return None
    clock = _parse_freeze_clock(clock_part)
    if clock is None:
        return None
    hour, minute, second = clock
    zone, _ = resolve_zone(zone_part, now)
    if not date_part:
        return _freeze_closest_day(now, zone, hour, minute, second)
    date = _parse_freeze_date(date_part)
    if date is None:
        return None
    year, month, day, has_year = date
    if has_year:
        return _freeze_date(zone, year, month, day, hour, minute, second)
    return _freeze_closest_year(now, zone, month, day, hour, minute, second)


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
    frozen = _parse_freeze_instant(value)
    if frozen is None:
        # A trailing word that reads as an attempted zone, and fails to
        # resolve as one, is worth resolve_zone's own reason rather than the
        # generic message below: _parse_freeze_clock_zone only raises once a
        # date and a clock have already parsed, so the word really was meant
        # as a zone.
        try:
            frozen = _parse_freeze_clock_zone(value, datetime.now(timezone.utc))
        except ClockError as zerr:
            raise ClockError(f"CLOCK_FREEZE: {zerr}") from None
    # Year 0 is a spelling rather than a range: Go's time has one and
    # datetime does not, so datetime cannot construct it at all -- caught
    # already, inside _parse_freeze_instant's try/except -- and this is the
    # message it gets.
    if frozen is None:
        raise ClockError(
            "CLOCK_FREEZE wants an instant like 2026-07-15T09:53:07.123456Z "
            "(the fraction and its digit count are optional, down to none), "
            "a clock time like 15:30 UTC or 15:30 PT (nearest day filled in), or a "
            "dated one like 2026-07-22 15:30 UTC or 7/22 15:30 PT (nearest year "
            f'filled in when it\'s left out), got "{value}"'
        ) from None
    if not FREEZE_FIRST <= frozen <= FREEZE_LAST:
        raise ClockError(
            "CLOCK_FREEZE wants an instant from 0001-01-02 to 9999-12-30, "
            f'got "{value}"'
        )
    return frozen


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


def ascii_lower(s):
    """Lowercase A-Z and leave everything else exactly as it is.

    Zone names, country codes and the abbreviations are all ASCII, so none of
    the matching here wants Unicode's rules -- which is as well, since the two
    languages do not have the same ones. Python's str.lower() applies the full
    mappings, where Go's applies the simple ones, and they part company on
    U+0130, the Turkish dotted capital I: Python gives it an i and a combining
    dot, Go a plain i. So "Istanbul" spelt with one drew a clock under Go and
    was refused as an unknown zone under Python.
    """
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in s)


def ascii_upper(s):
    """Uppercase a-z and leave everything else exactly as it is; see ascii_lower."""
    return "".join(chr(ord(c) - 32) if "a" <= c <= "z" else c for c in s)


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


class VersionRequested(Exception):
    """--version: print the version and stop, successfully."""


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


# The six tunables parsed: what the frame loop actually lays out with. Read
# back from the text with read_tunes whenever the text changes -- at startup,
# and after every value the tuner takes.
Settings = collections.namedtuple(
    "Settings", "halign valign hpad vpad cell_ratio scale_auto rows per_row per_row_auto"
)


def rows_for_scale(scale):
    """Turn a --scale into a face height, refusing one nothing can draw.

    Too small and the numerals have nowhere to sit, too large and it is a
    canvas the size of a wall. The same answer at startup and under the
    tuner: a scale typed into the tuner is refused in the words the flag
    would have used.
    """
    rows = math.floor(DEFAULT_ROWS_N * scale + 0.5)
    if rows < MIN_ROWS_N or rows > MAX_ROWS_N:
        raise ClockError(
            f"--scale {scale:g} makes each face {rows} rows tall; want "
            f"{MIN_ROWS_N} to {MAX_ROWS_N} rows, roughly --scale "
            f"{MIN_ROWS_N / DEFAULT_ROWS_N:.2f} to --scale {MAX_ROWS_N / DEFAULT_ROWS_N:.2f}"
        )
    return rows


def check_tune(i, val):
    """Read one tunable's text exactly as its flag reads it.

    The only gate the tuner has: what survives this is stored as text and read
    back by read_tunes, which therefore cannot fail.
    """
    if i == TUNE_HALIGN:
        parse_choice("halign", val, HALIGNS)
    elif i == TUNE_VALIGN:
        parse_choice("valign", val, VALIGNS)
    elif i in (TUNE_HPAD, TUNE_VPAD):
        parse_pad(TUNABLES[i][0], val)
    elif i == TUNE_PER_ROW:
        if val != "auto":
            parse_count(val, "--per-row", MAX_PER_ROW)
    elif i == TUNE_SCALE:
        if val != "auto":
            rows_for_scale(parse_scale(val))
    else:
        parse_ratio(val)


def canon_tune(i, val):
    """How a value is written down once it has been accepted.

    A pad typed as "5" and one typed as "5%" are the same share, and a count
    typed as "007" is three faces a row, so the tuner, the saved file and the
    line it prints all say the one spelling. The decimals are left exactly as
    typed -- rewriting 2.15 as 2.2 would be rounding a value nobody asked to
    round.
    """
    if i in (TUNE_HPAD, TUNE_VPAD):
        try:
            n = parse_pad(TUNABLES[i][0], val)
        except ClockError:
            return val
        return val if n is None else f"{n}%"
    if i == TUNE_PER_ROW:
        if val == "auto":
            return val
        try:
            return str(parse_count(val, "--per-row", MAX_PER_ROW))
        except ClockError:
            return val
    return val


def read_tunes(vals):
    """Parse the six back into a Settings.

    Every value has been through check_tune, so nothing here can fail;
    anything that did would be a value stored without being checked, which is
    a bug rather than a bad input.
    """
    cell_ratio = positive_number(vals[TUNE_CELL_RATIO])
    scale_auto, rows = True, 0
    if vals[TUNE_SCALE] != "auto":
        scale_auto = False
        rows = rows_for_scale(positive_number(vals[TUNE_SCALE]))
    per_row, per_row_auto = DEFAULT_PER_ROW, True
    if vals[TUNE_PER_ROW] != "auto":
        per_row = parse_count(vals[TUNE_PER_ROW], "--per-row", MAX_PER_ROW)
        per_row_auto = False
    return Settings(
        vals[TUNE_HALIGN],
        vals[TUNE_VALIGN],
        parse_pad("hpad", vals[TUNE_HPAD]),
        parse_pad("vpad", vals[TUNE_VPAD]),
        DEFAULT_CELL_RATIO if cell_ratio is None else cell_ratio,
        scale_auto,
        rows,
        per_row,
        per_row_auto,
    )


def apply_settings(settings):
    """Put a Settings into the globals the drawing code reads.

    A fixed scale sizes the face here and for good; --scale auto leaves ROWS
    to the per-frame search, which reads CELL_RATIO itself.
    """
    global ROWS, COLS, CELL_RATIO
    CELL_RATIO = settings.cell_ratio
    if not settings.scale_auto:
        ROWS = settings.rows
        COLS = math.floor(ROWS * CELL_RATIO + 0.5)


class Options:
    """Everything a command line sets, and so everything the file can set too.

    The preferences file is read by the same parser, into the same object,
    before the command line is read into it on top. That is the whole of "the
    file is the front of your command line" -- last wins, with no second set
    of rules to keep in step with the first.
    """

    def __init__(self):
        self.zone_list = ""
        self.color_when = "auto"
        self.day_when = ""  # unset: run() picks it, since a pinned clock differs
        self.quiet = False
        self.vals = default_tunes()  # the six knobs, as text


def config_path():
    """Where S saves and startup reads.

    CLOCK_CONFIG names it outright -- set but empty means no preferences file
    at all, which is what every harness here runs with and what a script
    wanting the plain defaults can set. Otherwise the usual place, and nowhere
    at all if even HOME is unset.
    """
    if "CLOCK_CONFIG" in os.environ:
        return os.environ["CLOCK_CONFIG"]
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg:
        return os.path.join(xdg, "clock", "config")
    home = os.environ.get("HOME", "")
    if home:
        return os.path.join(home, ".config", "clock", "config")
    return ""


# What the file says about itself to whoever opens it, and that editing it is
# allowed -- it is a command line, and everything the command line takes it
# takes.
CONFIG_HEADER = (
    "# clock: written by S, read at startup. One argument per line.\n"
    "# Delete this file to forget it; CLOCK_CONFIG= ignores it.\n"
)


def load_config(path):
    """Read the file into the argument list it is.

    One token per line, blank lines and # lines skipped. One token per line
    rather than a line of arguments is what keeps a zone list with a space in
    it -- Salt Lake City -- from needing quoting rules that the two ports
    would then have to agree about.

    A file that is not there is not an error: it is a clock that has never
    been asked to save. One that cannot be read is, and says so in words of
    its own rather than the language's, since Go and Python spell that
    complaint differently and difftest compares the two.
    """
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = fh.read()
    except FileNotFoundError:
        return []
    except OSError:
        raise ClockError(f"cannot read {path}")
    except UnicodeDecodeError:
        raise ClockError(f"cannot read {path}")
    return [line for line in data.split("\n") if line and not line.startswith("#")]


def save_config(path, tokens):
    """Write the tokens back, through a temporary file in the same directory.

    So a save that fails part way leaves the old file rather than half of a
    new one.
    """
    if not path:
        raise ClockError("CLOCK_CONFIG is empty: nowhere to save")
    fail = ClockError(f"cannot write {path}")
    directory = os.path.dirname(path) or "."
    body = CONFIG_HEADER + "".join(token + "\n" for token in tokens)
    tmp = None
    try:
        os.makedirs(directory, exist_ok=True)
        handle, tmp = tempfile.mkstemp(prefix="config-", dir=directory)
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except OSError:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise fail from None


def parse_args(argv, opt):
    """Read the command line: one optional zone list, and the flags anywhere.

    Hand-rolled rather than argparse, which prints its own usage block, exits
    with status 2, and abbreviates long options -- none of which the Go port
    can reproduce. Both implementations run this algorithm verbatim.

    Reads into `opt` and hands it back, so the preferences file and then the
    command line can go through it in turn, the second writing over the first.
    """
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
                # Kept as text alongside the layout knobs, which is where the
                # tuner reads it from; the count is parsed here all the same,
                # so the complaint is --per-row's own.
                if val != "auto":
                    parse_count(val, "--per-row", MAX_PER_ROW)
                opt.vals[TUNE_PER_ROW] = canon_tune(TUNE_PER_ROW, val)
            elif name == "color":
                # Bare --color means always, and takes no separate argument:
                # "clock --color ET" names a zone list, exactly as ls and git
                # read the same flag. The value only ever follows an "=".
                opt.color_when = parse_choice("color", val, COLOR_WHENS) if sep else "always"
            elif name == "no-color":
                if sep:
                    raise ClockError("--no-color takes no value")
                opt.color_when = "never"
            elif name == "version":
                # Read where it is found, like --help: everything before it on
                # the command line still has to parse, everything after it is
                # never looked at.
                if sep:
                    raise ClockError("--version takes no value")
                raise VersionRequested
            elif name == "day":
                opt.day_when = parse_choice("day", val, WHENS) if sep else "always"
            elif name == "no-day":
                if sep:
                    raise ClockError("--no-day takes no value")
                opt.day_when = "never"
            elif name == "quiet":
                if sep:
                    raise ClockError("--quiet takes no value")
                opt.quiet = True
            elif name in ("halign", "valign", "hpad", "vpad", "cell-ratio", "scale"):
                # These six want a value, and take it either way round, as
                # --per-row does: there is no bare form to be ambiguous with.
                if not sep:
                    i += 1
                    if i >= len(argv):
                        raise ClockError(f"--{name} needs a value, e.g. {NEEDS[name]}")
                    val = argv[i]
                # Checked by the flag's own parser and then kept as text, so
                # the tuner and a saved command line say what was typed.
                k = tune_index(name)
                check_tune(k, val)
                opt.vals[k] = canon_tune(k, val)
            else:
                raise ClockError(f"unknown option: --{name}")
        elif a == "-q":
            opt.quiet = True
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
            if rest != "auto":
                parse_count(rest, "-n", MAX_PER_ROW)
            opt.vals[TUNE_PER_ROW] = canon_tune(TUNE_PER_ROW, rest)
        else:
            positional.append(a)
        i += 1

    if len(positional) > 1:
        raise ClockError(
            f"expected one comma-separated zone list, got {len(positional)}: "
            + " ".join(positional)
        )
    if positional:
        opt.zone_list = positional[0]
    return opt


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

# The fifty states, DC, and the territories with ZIP codes, each by the zone
# its capital keeps, so a state that spans two means the capital's -- and the
# face says which it landed on, MST (Arizona), so a reader in the other part
# is told rather than misled. Washington is the state; the city is Washington
# DC. Two-letter codes are not taken, because CA, IN, DE and GA are countries
# already. New York, Puerto Rico and Guam are not in the table only because
# the tz database names zones after them, and the database is always asked
# first. Sorted, same order as clock.go's table.
US_STATES = (
    ("Alabama", "America/Chicago"),
    ("Alaska", "America/Juneau"),
    ("American Samoa", "Pacific/Pago_Pago"),
    ("Arizona", "America/Phoenix"),
    ("Arkansas", "America/Chicago"),
    ("California", "America/Los_Angeles"),
    ("Colorado", "America/Denver"),
    ("Connecticut", "America/New_York"),
    ("Delaware", "America/New_York"),
    ("District of Columbia", "America/New_York"),
    ("Florida", "America/New_York"),
    ("Georgia", "America/New_York"),
    ("Hawaii", "Pacific/Honolulu"),
    ("Idaho", "America/Boise"),
    ("Illinois", "America/Chicago"),
    ("Indiana", "America/Indiana/Indianapolis"),
    ("Iowa", "America/Chicago"),
    ("Kansas", "America/Chicago"),
    ("Kentucky", "America/New_York"),
    ("Louisiana", "America/Chicago"),
    ("Maine", "America/New_York"),
    ("Maryland", "America/New_York"),
    ("Massachusetts", "America/New_York"),
    ("Michigan", "America/Detroit"),
    ("Minnesota", "America/Chicago"),
    ("Mississippi", "America/Chicago"),
    ("Missouri", "America/Chicago"),
    ("Montana", "America/Denver"),
    ("Nebraska", "America/Chicago"),
    ("Nevada", "America/Los_Angeles"),
    ("New Hampshire", "America/New_York"),
    ("New Jersey", "America/New_York"),
    ("New Mexico", "America/Denver"),
    ("North Carolina", "America/New_York"),
    ("North Dakota", "America/Chicago"),
    ("Northern Mariana Islands", "Pacific/Saipan"),
    ("Ohio", "America/New_York"),
    ("Oklahoma", "America/Chicago"),
    ("Oregon", "America/Los_Angeles"),
    ("Pennsylvania", "America/New_York"),
    ("Rhode Island", "America/New_York"),
    ("South Carolina", "America/New_York"),
    ("South Dakota", "America/Chicago"),
    ("Tennessee", "America/Chicago"),
    ("Texas", "America/Chicago"),
    ("U.S. Virgin Islands", "America/St_Thomas"),
    ("US Virgin Islands", "America/St_Thomas"),
    ("Utah", "America/Denver"),
    ("Vermont", "America/New_York"),
    ("Virginia", "America/New_York"),
    ("Washington", "America/Los_Angeles"),
    ("Washington D.C.", "America/New_York"),
    ("Washington DC", "America/New_York"),
    ("West Virginia", "America/New_York"),
    ("Wisconsin", "America/Chicago"),
    ("Wyoming", "America/Denver"),
)

# Places people want a clock for that the tz database does not name a zone
# after -- Seattle, Mumbai, Munich. Each was checked against GeoNames, and a
# name shared by cities on different clocks is kept only when the largest on
# this clock is three times the size of any namesake on another: so Portland
# is Oregon, while San Jose, St. Louis, Barcelona and Venice are left out.
# Cities the tz database does name, Los Angeles and Hong Kong among them, are
# left to it. Sorted, same order as clock.go's table.
COMMON_CITIES = (
    ("Abu Dhabi", "Asia/Dubai"),
    ("Abuja", "Africa/Lagos"),
    ("Albuquerque", "America/Denver"),
    ("Ankara", "Europe/Istanbul"),
    ("Atlanta", "America/New_York"),
    ("Austin", "America/Chicago"),
    ("Baltimore", "America/New_York"),
    ("Bangalore", "Asia/Kolkata"),
    ("Beijing", "Asia/Shanghai"),
    ("Bengaluru", "Asia/Kolkata"),
    ("Boston", "America/New_York"),
    ("Brasilia", "America/Sao_Paulo"),
    ("Busan", "Asia/Seoul"),
    ("Calgary", "America/Edmonton"),
    ("Canberra", "Australia/Sydney"),
    ("Cape Town", "Africa/Johannesburg"),
    ("Charlotte", "America/New_York"),
    ("Chengdu", "Asia/Shanghai"),
    ("Chennai", "Asia/Kolkata"),
    ("Christchurch", "Pacific/Auckland"),
    ("Cincinnati", "America/New_York"),
    ("Cleveland", "America/New_York"),
    ("Cologne", "Europe/Berlin"),
    ("Columbus", "America/New_York"),
    ("Dallas", "America/Chicago"),
    ("Delhi", "Asia/Kolkata"),
    ("Durban", "Africa/Johannesburg"),
    ("Edinburgh", "Europe/London"),
    ("El Paso", "America/Denver"),
    ("Florence", "Europe/Rome"),
    ("Fort Worth", "America/Chicago"),
    ("Frankfurt", "Europe/Berlin"),
    ("Geneva", "Europe/Zurich"),
    ("Glasgow", "Europe/London"),
    ("Guadalajara", "America/Mexico_City"),
    ("Guangzhou", "Asia/Shanghai"),
    ("Hamburg", "Europe/Berlin"),
    ("Hanoi", "Asia/Ho_Chi_Minh"),
    ("Houston", "America/Chicago"),
    ("Hyderabad", "Asia/Kolkata"),
    ("Islamabad", "Asia/Karachi"),
    ("Jacksonville", "America/New_York"),
    ("Jeddah", "Asia/Riyadh"),
    ("Kansas City", "America/Chicago"),
    ("Krakow", "Europe/Warsaw"),
    ("Kyoto", "Asia/Tokyo"),
    ("Lahore", "Asia/Karachi"),
    ("Las Vegas", "America/Los_Angeles"),
    ("Lyon", "Europe/Paris"),
    ("Manchester", "Europe/London"),
    ("Marseille", "Europe/Paris"),
    ("Mecca", "Asia/Riyadh"),
    ("Memphis", "America/Chicago"),
    ("Miami", "America/New_York"),
    ("Milan", "Europe/Rome"),
    ("Milwaukee", "America/Chicago"),
    ("Minneapolis", "America/Chicago"),
    ("Montreal", "America/Toronto"),
    ("Mumbai", "Asia/Kolkata"),
    ("Munich", "Europe/Berlin"),
    ("Naples", "Europe/Rome"),
    ("Nashville", "America/Chicago"),
    ("New Delhi", "Asia/Kolkata"),
    ("New Orleans", "America/Chicago"),
    ("New York City", "America/New_York"),
    ("Oklahoma City", "America/Chicago"),
    ("Omaha", "America/Chicago"),
    ("Orlando", "America/New_York"),
    ("Osaka", "Asia/Tokyo"),
    ("Ottawa", "America/Toronto"),
    ("Philadelphia", "America/New_York"),
    ("Pittsburgh", "America/New_York"),
    ("Portland", "America/Los_Angeles"),
    ("Pretoria", "Africa/Johannesburg"),
    ("Pune", "Asia/Kolkata"),
    ("Quebec City", "America/Toronto"),
    ("Raleigh", "America/New_York"),
    ("Rio de Janeiro", "America/Sao_Paulo"),
    ("Rotterdam", "Europe/Amsterdam"),
    ("Sacramento", "America/Los_Angeles"),
    ("Saint Petersburg", "Europe/Moscow"),
    ("Salt Lake City", "America/Denver"),
    ("San Antonio", "America/Chicago"),
    ("San Diego", "America/Los_Angeles"),
    ("San Francisco", "America/Los_Angeles"),
    ("Seattle", "America/Los_Angeles"),
    ("Seville", "Europe/Madrid"),
    ("Shenzhen", "Asia/Shanghai"),
    ("St. Petersburg", "Europe/Moscow"),
    ("Tampa", "America/New_York"),
    ("Tel Aviv", "Asia/Jerusalem"),
    ("The Hague", "Europe/Amsterdam"),
    ("Tucson", "America/Phoenix"),
    ("Wellington", "Pacific/Auckland"),
    ("Yokohama", "Asia/Tokyo"),
)

# The ISO 3166-2 codes for the same places -- US-CA, US-NY -- and the only
# short form taken. The bare two letters cannot be: most already mean something
# else here, and most of those mean a different clock, since CA is Canada, IN
# India, DE Germany, and CT and MT are this clock's own Central and Mountain.
# Each code names a row in the table above, or a name the tz database answers
# to itself (US-NY, US-PR, US-GU), and the face is labelled with that full name
# rather than the code. Sorted, same order as clock.go's table.
US_CODES = (
    ("US-AK", "Alaska"),
    ("US-AL", "Alabama"),
    ("US-AR", "Arkansas"),
    ("US-AS", "American Samoa"),
    ("US-AZ", "Arizona"),
    ("US-CA", "California"),
    ("US-CO", "Colorado"),
    ("US-CT", "Connecticut"),
    ("US-DC", "District of Columbia"),
    ("US-DE", "Delaware"),
    ("US-FL", "Florida"),
    ("US-GA", "Georgia"),
    ("US-GU", "Guam"),
    ("US-HI", "Hawaii"),
    ("US-IA", "Iowa"),
    ("US-ID", "Idaho"),
    ("US-IL", "Illinois"),
    ("US-IN", "Indiana"),
    ("US-KS", "Kansas"),
    ("US-KY", "Kentucky"),
    ("US-LA", "Louisiana"),
    ("US-MA", "Massachusetts"),
    ("US-MD", "Maryland"),
    ("US-ME", "Maine"),
    ("US-MI", "Michigan"),
    ("US-MN", "Minnesota"),
    ("US-MO", "Missouri"),
    ("US-MP", "Northern Mariana Islands"),
    ("US-MS", "Mississippi"),
    ("US-MT", "Montana"),
    ("US-NC", "North Carolina"),
    ("US-ND", "North Dakota"),
    ("US-NE", "Nebraska"),
    ("US-NH", "New Hampshire"),
    ("US-NJ", "New Jersey"),
    ("US-NM", "New Mexico"),
    ("US-NV", "Nevada"),
    ("US-NY", "New York"),
    ("US-OH", "Ohio"),
    ("US-OK", "Oklahoma"),
    ("US-OR", "Oregon"),
    ("US-PA", "Pennsylvania"),
    ("US-PR", "Puerto Rico"),
    ("US-RI", "Rhode Island"),
    ("US-SC", "South Carolina"),
    ("US-SD", "South Dakota"),
    ("US-TN", "Tennessee"),
    ("US-TX", "Texas"),
    ("US-UT", "Utah"),
    ("US-VA", "Virginia"),
    ("US-VI", "US Virgin Islands"),
    ("US-VT", "Vermont"),
    ("US-WA", "Washington"),
    ("US-WI", "Wisconsin"),
    ("US-WV", "West Virginia"),
    ("US-WY", "Wyoming"),
)


# The names people write where iso3166.tab writes another: it says Britain (UK),
# Czech Republic, Myanmar (Burma), and spells four names with a character
# outside ASCII that has to be typed exactly. Everything else a person is
# likely to write is reached by rule in country_spellings, so this stays a list
# of exceptions rather than a second copy of the database. It is consulted
# before the file, and so still answers on a machine that has no iso3166.tab at
# all. Sorted, same order as clock.go's table.
COUNTRY_SYNONYMS = (
    ("Aland Islands", "AX"),
    ("Burma", "MM"),
    ("Cote d'Ivoire", "CI"),
    ("Czechia", "CZ"),
    ("Great Britain", "GB"),
    ("Ivory Coast", "CI"),
    ("Myanmar", "MM"),
    ("USA", "US"),
    ("United Kingdom", "GB"),
    ("United States of America", "US"),
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
            ' module: "pip install ziptz-us", or copy ziptz.py next to this'
            " file. Every other kind of zone works without it"
        )
    try:
        return ziptz.location(token)
    except ziptz.ZipError as exc:
        raise ClockError(str(exc)) from None


def tz_file(name):
    """One of the tz database's own tables, and whether it was found at all.

    Absent on stripped-down systems, so never fatal: what reads it says so
    instead.
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
            with open(directory + "/" + name, encoding="utf-8") as handle:
                return handle.read(), True
        except OSError:
            continue
    return "", False


def zone_tab():
    """The table of countries and their zones."""
    return tz_file("zone.tab")


def country_by_name(token):
    """The code a country's name stands for and the spelling to label it with,
    out of iso3166.tab -- the database's own list of countries -- and whether
    it has one.

    An "&" may be written "and", since the tab writes Antigua & Barbuda.
    Nothing else is forgiven, so the four names holding a character outside
    ASCII -- Curacao, Reunion, Cote d'Ivoire and the Aland Islands, as the tab
    does not spell them -- have to be typed the way it does, for the reason in
    ARCHITECTURE's Case folding.
    """
    key = place_key(token)
    for name, code in COUNTRY_SYNONYMS:
        if place_key(name) == key:
            return code, name, True
    data, found = tz_file("iso3166.tab")
    if not found:
        return "", "", False
    for line in data.split("\n"):
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        for spelling in country_spellings(fields[1]):
            if place_key(spelling) == key:
                # The spelling that matched, not the file's: someone who wrote
                # South Korea is told South Korea, and is not asked to read
                # Korea (South) inside a pair of parentheses of its own.
                return fields[0], spelling, True
    return "", "", False


def country_spellings(name):
    """Every way one of iso3166.tab's names might be written.

    Its own, an "&" written out, an "St" written "Saint", and a trailing
    qualifier moved to the front, since the file writes Korea (South) where a
    person writes South Korea. The rules are ASCII and mechanical, so the two
    ports cannot drift over them, and one that invents a spelling nobody types
    -- "Burma Myanmar" -- costs a comparison and reaches nothing.
    """
    out = [name]

    def add(s):
        if s not in out:
            out.append(s)

    if " & " in name:
        add(name.replace(" & ", " and "))
    for v in list(out):
        if v.startswith("St "):
            add("Saint " + v[len("St "):])
    for v in list(out):
        i = v.find(" (")
        if i > 0 and v.endswith(")"):
            add(v[i + 2:-1] + " " + v[:i])
    return out


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


def country_zone(cc, label, at):
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
    # All of them, however many: the list is what the reader has to choose
    # from, and a count of the ones it withheld helps nobody choose.
    raise ClockError(
        f"{label} spans {len(kept)} time zones; name one: " + ", ".join(kept)
    )


def suffix_zones(token):
    """The zones whose name ends with the token as a whole path segment.

    Europe/Berlin for "Berlin", and America/Indiana/Indianapolis for either
    "Indianapolis" or "Indiana/Indianapolis". Whole segments only, so "Berl"
    finds nothing and "York" does not answer for "New_York". A space reads as
    the underscore it stands for, so "New York" finds it all the same.

    Read out of zone.tab, the same file the country codes come from, which
    lists the canonical zones and leaves out the backward-compatibility links
    -- so "Eastern" is not a name here, and US/Eastern still resolves the
    ordinary way, in full.
    """
    data, found = zone_tab()
    if not found:
        return [], False
    want = "/" + ascii_lower(token).replace(" ", "_")
    out = []
    for line in data.split("\n"):
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 3 and ascii_lower(fields[2]).endswith(want):
            out.append(fields[2])
    return out, True


def suffix_zone(token):
    """One zone named by its tail alone, and the name to label it with --
    (None, "") when nothing matches.

    Ambiguity is refused rather than guessed at. Every city in the tz database
    is unique today, but nothing promises it stays that way, and two clocks an
    ocean apart is not a choice to make on the reader's behalf. The name is
    place_name's: the part of the zone that was typed.
    """
    names, found = suffix_zones(token)
    if not found or not names:
        return None, ""
    if len(names) > 1:
        raise ClockError(
            f"{token} names {len(names)} zones; name one in full: " + ", ".join(names)
        )
    try:
        return ZoneInfo(names[0]), place_name(names[0], token)
    except Exception:
        return None, ""


def place_name(zone, token):
    """The part of zone a token matched, written the way a person writes it.

    The zone's own capitals, and spaces for its underscores. Both "new_york"
    and "New York" show as New York, and Indiana/Indianapolis keeps both
    parts, since that is how much of the name was typed.
    """
    parts = zone.split("/")
    n = min(token.count("/") + 1, len(parts))
    return "/".join(parts[-n:]).replace("_", " ")


def place_key(s):
    """How a state or city is matched: ASCII case folded, and an underscore
    read as the space it stands for, so New_Mexico and "new mexico" both find
    New Mexico. Nothing else is forgiven -- not runs of spaces, since Python
    and Go do not agree about which characters are spaces."""
    return ascii_lower(s).replace("_", " ")


def place_zone(token):
    """A US state, its ISO 3166-2 code, or a common city, and the name to label
    it with -- the table's own spelling whatever case the token was typed in,
    and for a code the full name rather than the code. (None, "") when the
    token is none of them."""
    key = place_key(token)
    named = ""
    for code, full in US_CODES:
        if place_key(code) == key:
            named, key = full, place_key(full)
            break
    for table in (US_STATES, COMMON_CITIES):
        for name, target in table:
            if place_key(name) != key:
                continue
            try:
                return ZoneInfo(target), name
            except Exception:
                raise ClockError(
                    f"{name} means {target}, which this system's time zone database lacks"
                ) from None
    if named:
        # US-NY, US-PR and US-GU name the three places the tz database answers
        # to itself, which is why they are not rows above.
        return suffix_zone(named)
    return None, ""


def unknown_zone(token):
    return ClockError(
        f'unknown zone "{token}"; use an IANA name (Europe/Berlin), a city '
        f"(Berlin, Seattle), a US state (Arizona, US-AZ), an abbreviation "
        f"({alias_names()}), a country (Germany, JP), or a US ZIP code"
    )


def local_zone():
    """None, meaning the platform's local zone -- once TZ is one both ports read.

    Go asks the tz database for whatever TZ names and falls back to UTC when it
    has no such file; Python leaves the question to the C library, which also
    reads the POSIX rule form -- "PST8PDT,M3.2.0,M11.1.0", "<+07>-7", "GMT+5".
    So a POSIX rule makes one clock read Pacific and the other UTC, seven hours
    apart, both of them sure. There is no fixing that from here without writing
    a tzset the Go standard library does not export, so say so instead: this is
    the Windows message's argument, one environment variable down.

    Only a TZ that has to be *looked up* is checked. Unset, empty (which POSIX
    reads as UTC) and an absolute path all mean the same thing to both.
    """
    tz = os.environ.get("TZ")
    if tz is None:
        return None
    if tz.startswith(":"):
        tz = tz[1:]
    if tz == "" or tz.startswith("/"):
        return None
    try:
        ZoneInfo(tz)
    except Exception:
        raise ClockError(
            f'TZ="{tz}" is not a zone name, and a POSIX TZ rule is not something '
            f"both clocks read alike; name a zone as an argument instead"
        ) from None
    return None


def resolve_zone(token, at):
    """Turn one token into a tzinfo, and the place it named if it named one.

    The tzinfo is None for the system's local zone. The place is a US state, a
    city from COMMON_CITIES, or a city off the end of an IANA zone -- and "" for
    every other kind of zone.

    Order matters: the alias table is consulted before the tz database only for
    names the database lacks, the fixed-offset table only after it so that real
    zones win, and "local" and "" are intercepted because Python
    and Go disagree about both -- ZoneInfo("Local") raises where
    LoadLocation("Local") works, and ZoneInfo("") raises where LoadLocation("")
    quietly returns UTC. Places come last of all, the database's own tails
    before the tables here.
    """
    if token.startswith("/") or ".." in token:
        raise ClockError(f'"{token}" is not a zone name')
    if ascii_lower(token) == "local":
        return local_zone(), ""
    if token.isascii() and token.isdigit():
        # A ZIP is a place like any other, and the one whose zone is least
        # guessable of all: 94110 says nothing about Los Angeles by itself.
        return zip_zone(token), token
    up = ascii_upper(token)
    for name, target in ZONE_ALIASES:
        if name == up:
            try:
                return ZoneInfo(target), ""
            except Exception:
                raise ClockError(
                    f"{up} means {target}, which this system's time zone database lacks"
                ) from None
    try:
        zone = ZoneInfo(token)
    except Exception:
        pass
    else:
        # A country the database also keeps a zone or a compatibility link
        # under -- Japan, Cuba, Singapore -- resolves there, as it always did,
        # and is labelled with the country all the same. GB and NZ are links
        # as well as codes, and are labelled like every other code rather than
        # being the two that are not.
        _, name, ok = country_by_name(token)
        if ok:
            return zone, name
        if len(up) == 2 and "A" <= up[0] <= "Z" and "A" <= up[1] <= "Z":
            names, found = country_zones(up)
            if found and names:
                return zone, up
        return zone, ""
    for name, offset in ZONE_FIXED:
        if name == up:
            return timezone(timedelta(seconds=offset), name), ""
    if len(up) == 2 and "A" <= up[0] <= "Z" and "A" <= up[1] <= "Z":
        found = country_zone(up, up, at)
        if found is not None:
            return found, up
    # Last, so a city can never shadow a name the database itself answers to:
    # the database's own tails first, then the states and cities written here.
    for lookup in (suffix_zone, place_zone):
        zone, place = lookup(token)
        if zone is not None:
            return zone, place
    # A country by name, last of all: Georgia is the state, and the country is
    # GE, because the tables above are asked first.
    code, name, ok = country_by_name(token)
    if ok:
        found = country_zone(code, name, at)
        if found is not None:
            return found, name
    raise unknown_zone(token)


def resolve_zones(zone_list, at):
    """The zone list as (token, zone, place) triples, left to right.

    The token is carried along because merge_zones labels a face with the
    spellings that asked for it, not just the zone it landed on -- and the
    place, when the token named one, for the parentheses after that label.
    """
    if not zone_list:
        return [("", local_zone(), "")]
    out = []
    for token in zone_list.split(","):
        token = token.strip(" \t")
        if not token:
            raise ClockError(f'empty zone in "{zone_list}"')
        out.append((token, *resolve_zone(token, at)))
    return out


def zone_label(abbr, tokens, plain, places):
    """The name written over one face.

    Just the abbreviation, unless more than one spelling collapsed onto this
    face -- then each spelling that reads differently is named too, because
    that is the only place the ambiguity is visible. PDT,PDT asked the same
    question twice and gets one plain answer.

    A state or city follows in parentheses whatever else collapsed with it,
    since the zone it landed in is written nowhere else: Boise alone is
    MDT (Boise), and MT,Boise is MDT/MT (Boise). tokens is every spelling that
    asked for this face, plain the ones that named no place, and places the
    names of the ones that did.
    """
    label = abbr
    if len(tokens) >= 2:
        label = "/".join([abbr] + [t for t in plain if ascii_upper(t) != ascii_upper(abbr)])
    if places:
        label += " (" + ", ".join(places) + ")"
    return label


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
    for token, zone, place in zones:
        t = in_zone(now, zone)
        key = (f"{t:%Z}", int(t.utcoffset().total_seconds()))
        if key not in index:
            index[key] = len(out)
            out.append((key[0], [], [], [], zone))
        _, tokens, plain, places, _ = out[index[key]]
        if not token or contains_fold(tokens, token):
            continue
        tokens.append(token)
        if not place:
            plain.append(token)
        elif not contains_fold(places, place):
            places.append(place)
    return [
        (zone_label(abbr, tokens, plain, places), zone)
        for abbr, tokens, plain, places, zone in out
    ]


def contains_fold(items, s):
    """Whether items holds s, ASCII case folded."""
    return any(ascii_upper(item) == ascii_upper(s) for item in items)


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


def fit_label(label, n):
    """Cut a face's label to n characters the way truncate does -- except that
    a label ending in a list of places keeps its closing parenthesis.

    In a narrow cell "MDT (Utah, Colorado, New Mexico)" ends "...)" instead of
    stopping partway through a name with the list left open. A cut that would
    keep none of the list is a plain truncation instead, since "MDT (...)" says
    less than the first letters of the place would.
    """
    if len(label) <= n:
        return label
    at = label.find(" (")
    if at < 0 or not label.endswith(")"):
        return truncate(label, n)
    keep = n - len("...)")
    if keep <= at + len(" ("):
        return truncate(label, n)
    return label[:keep].rstrip(" ,") + "...)"


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


class Tuner:
    """What r puts up: the six knobs, one picked, and what is being typed.

    It holds no values of its own -- the text it shows is the same text the
    frame loop lays out from, so what is on screen is always what is in
    effect.
    """

    def __init__(self):
        self.open = False
        self.sel = 0  # which of the six is picked
        self.edit = ""  # what has been typed into it, once typing has started
        self.editing = False
        self.err = ""  # the last refusal, in the flag's own words


def tune_options(i):
    """The words a knob takes, for the two that take words rather than numbers.

    The lists the flags themselves are checked against, so the tuner offers
    exactly what --halign and --valign accept.
    """
    if i == TUNE_HALIGN:
        return HALIGNS
    if i == TUNE_VALIGN:
        return VALIGNS
    return None


def tune_word(i):
    """The word a numeric knob takes instead of a number.

    It sits one step below the knob's smallest value: even for a pad, auto for
    the scale. The cell ratio has none -- there is no "work it out for me" for
    a font.
    """
    if i in (TUNE_HPAD, TUNE_VPAD):
        return "even"
    if i in (TUNE_SCALE, TUNE_PER_ROW):
        return "auto"
    return ""


def tenths_text(tenths):
    """A count of tenths as a decimal, written by hand: 21 is "2.1", 20 is "2".

    Every number the tuner steps to is built this way rather than formatted
    from a float, which is what keeps the two ports spelling the same value
    the same -- see the note on TUNABLES.
    """
    if tenths % 10 == 0:
        return str(tenths // 10)
    return f"{tenths // 10}.{tenths % 10}"


def tune_step(i, val, delta, wrap):
    """Move a knob one step, returning the text it lands on.

    Or the text it started from, where there is nowhere to go. A word list
    walks, and wraps if asked (which is what space does, and the arrows do
    not). A pad moves a whole percent, since that is what it counts; the scale
    and the cell ratio move a tenth, counted as integer tenths so no float is
    formatted back into text. Below the smallest number is the knob's word,
    where it has one, and above the largest is nothing at all.

    Anything it lands on goes through check_tune before it is handed back, so
    stepping cannot reach a value typing would be refused for -- a scale one
    step past what the face may be simply does not move.
    """
    options = tune_options(i)
    if options is not None:
        at = options.index(val) if val in options else 0
        following = at + delta
        if wrap:
            following %= len(options)
        if not 0 <= following < len(options):
            return val
        return options[following]

    word = tune_word(i)
    if val == word:
        if delta < 0:
            return val  # already below the smallest number there is
        # Back into numbers at the plain default, which is the size, the
        # spacing and the row an untouched clock has.
        if i == TUNE_SCALE:
            return "1"
        if i == TUNE_PER_ROW:
            return str(DEFAULT_PER_ROW)
        return "0%"

    if i == TUNE_PER_ROW:
        try:
            n = parse_count(val, "--per-row", MAX_PER_ROW)
        except ClockError:
            return val
        if n + delta < 1:
            return word
        following = str(n + delta)
    elif i in (TUNE_HPAD, TUNE_VPAD):
        try:
            n = parse_pad(TUNABLES[i][0], val)
        except ClockError:
            return val
        n = 0 if n is None else n
        if n + delta < 0:
            return word
        following = f"{n + delta}%"
    else:
        value = positive_number(val)
        if value is None:
            return val
        tenths = math.floor(value * 10 + 0.5) + delta
        if tenths <= 0:
            return word or val
        following = tenths_text(tenths)
    try:
        check_tune(i, following)
    except ClockError:
        # Off the end of what this knob may be. The scale has somewhere to go
        # at the bottom -- auto -- and nowhere at the top.
        return word if delta < 0 and word else val
    return following


def tune_show(i, val):
    """A knob's value as the box says it.

    The words it takes with the one it holds in brackets, or -- for a number
    -- the steps either side of it, so the size of a step is on screen rather
    than something to find out by pressing a key.
    """
    options = tune_options(i)
    if options is not None:
        return " ".join(f"[{o}]" if o == val else o for o in options)
    out = f"[{val}]"
    previous = tune_step(i, val, -1, False)
    if previous != val:
        out = f"{previous} {out}"
    following = tune_step(i, val, 1, False)
    if following != val:
        out = f"{out} {following}"
    return out


def tune_rows(tune, vals):
    """The tuner's modal, one row per knob and a footer.

    The value column is the text each knob holds, except the one being typed
    into, which shows the buffer and a cursor -- so an empty buffer still
    reads as a field waiting for something rather than as a value of nothing.

    The face height goes beside the scale, since that is the number --scale
    auto is choosing and the one a reader wanting to pin it down needs.
    """
    rows = []
    for i, (name, _) in enumerate(TUNABLES):
        mark = "> " if i == tune.sel else "  "
        val = tune.edit + "_" if i == tune.sel and tune.editing else tune_show(i, vals[i])
        if i == TUNE_SCALE:
            val += f"   ({ROWS} rows)"
        rows.append(f"{mark}{name:<{TUNE_COL}}{val}")
    say = tune.err or TUNABLES[tune.sel][1]
    return rows + [
        "",
        say,
        "up down pick   left right or space change",
        "or type a value and enter   esc done",
    ]


def tune_flags(vals):
    """What the tuned layout would take on a command line.

    The knobs that differ from an untouched clock's, in the tuner's own order.
    Nothing is formatted here -- these are the strings that were typed.
    """
    default = default_tunes()
    out = []
    for i, (name, _) in enumerate(TUNABLES):
        if vals[i] != default[i]:
            out += [f"--{name}", vals[i]]
    return out


def config_tokens(vals, zone_list):
    """The clock on screen written as an argument list.

    The knobs that differ from an untouched clock's -- the per-row count among
    them -- and the zone list as it was typed. What S saves, one token per line, and
    what the line the tuner leaves behind is made of.
    """
    tokens = tune_flags(vals)
    if zone_list:
        tokens.append(zone_list)
    return tokens


def tune_command(vals, zone_list):
    """The same list said out loud: quoted for a shell.

    Empty unless some knob was actually moved -- a clock still at its defaults
    has nothing to tell anyone. No program name in front of it, since the two
    ports are installed under different ones and the flags are the part worth
    copying either way.
    """
    if not tune_flags(vals):
        return ""
    return " ".join(shell_quote(t) for t in config_tokens(vals, zone_list))


# What a shell reads as one word, and so needs no quoting.
SHELL_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_,./:+-%="
)


def shell_quote(s):
    """Wrap a zone list a shell would read as more than one word.

    Single quotes, and a name holding one of those is quoted the long way
    round, which is the one spelling every POSIX shell agrees on.
    """
    if s and SHELL_SAFE.issuperset(s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"


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
    # The face is COLS wide and its cell may be wider, so the face rows are
    # padded into it. Plain spaces on either side of already-coloured rows,
    # rather than centring them: centring counts characters, and a coloured row
    # is mostly escape bytes.
    cell = cell_cols()
    pad_left = " " * ((cell - COLS) // 2)
    pad_right = " " * (cell - COLS - len(pad_left))

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
        rows.extend(
            row([pad_left + part + pad_right for part in line]) for line in zip(*drawn)
        )
        rows.append(
            row([center(fit_label(label, cell), cell, lay.extra_left) for label, _ in chunk])
        )
        rows.append(
            row([center(digital(t, weekday), cell, lay.extra_left) for t in times])
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


def cell_cols():
    """How wide one face's column is: the face, or its readout if that is wider."""
    return max(COLS, READOUT_COLS)


def fit_per_row(want, n, term_cols, gap):
    """Reduce the requested faces-per-row to what the window can hold.

    Wrapping is what actually breaks the display: a wrapped line desynchronises
    the cursor rewind and the frame smears.
    """
    want = min(want, n)
    if term_cols <= 0:
        return want  # not a terminal: honour what was asked for
    cell = cell_cols()
    max_fit = (term_cols + gap) // (cell + gap)
    if max_fit < 1:
        raise ClockError(
            f"terminal is {term_cols} columns wide and one clock face needs "
            f"{cell}; widen the window, or lower --cell-ratio"
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

    Yields (interactive, restore, requiet): whether stdin is a terminal, i.e.
    whether keys can be read at all, and the two moves the clock can make with
    it -- restore puts back what the shell handed over, requiet takes it again.
    Quitting needs the first, Ctrl+Z needs both, either side of the stop.
    """

    def nothing():
        pass

    try:
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except (AttributeError, ValueError, termios.error):
        # not a terminal (piped or redirected): nothing to quieten
        yield False, nothing, nothing
        return

    quiet = list(saved)
    quiet[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON)  # lflag
    try:
        termios.tcsetattr(fd, termios.TCSANOW, quiet)
        yield (
            True,
            lambda: termios.tcsetattr(fd, termios.TCSAFLUSH, saved),
            lambda: termios.tcsetattr(fd, termios.TCSANOW, quiet),
        )
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
    return os.read(sys.stdin.fileno(), KEYS_PER_FRAME)


# How many bytes of typing a frame answers before drawing again. The Go port
# reads the terminal a byte at a time and drains up to this many before
# painting, which is the same rule written the other way round: a keystroke is
# more than one byte, and a frame that landed in the middle of one would be a
# frame the other port never painted.
KEYS_PER_FRAME = 64

# How many frames a half-read keystroke is given to finish -- 38ms, which is
# nothing to wait for an Esc and more than enough for bytes the terminal has
# already written.
ESC_GRACE = 2

# Keys the tuner reads that are not one character: what KeyDecoder hands back
# instead of a byte.
KEY_UP = "\x01up"
KEY_DOWN = "\x01down"
KEY_LEFT = "\x01left"
KEY_RIGHT = "\x01right"
KEY_ENTER = "\x01enter"
KEY_BACK = "\x01back"
KEY_ESC = "\x01esc"


class KeyDecoder:
    """Turn the byte stream into keys.

    Arrows arrive as an escape sequence -- ESC [ A, or ESC O A from a terminal
    in application cursor mode -- so ESC cannot be answered the moment it
    lands: it is either a key of its own or the first byte of one. It is held
    until the byte after it says which, and settle() is what decides a lone
    one, at the end of the batch of keys a frame reads. Both ports settle at
    that same point rather than on a timeout: a timer would make which frame
    an Esc lands in a question about the machine's speed, and keytest compares
    the frames.

    A batch boundary is not the end of a keystroke, though, which is what a
    held-down arrow shows: the terminal sends ESC [ C fifty times a second and
    a read can end anywhere in that, so an ESC left over at the end of one
    frame is as likely to be half an arrow as it is a whole Esc. Answering it
    there closed the tuner mid-keypress. So a pending sequence has to sit out
    a whole frame with nothing following it before it is called: `waited`
    counts the frames it has survived, and one is enough, since the rest of a
    sequence the terminal has already sent is never more than a read away.
    """

    def __init__(self):
        self.state = 0  # 0 nothing pending, 1 an ESC, 2 inside a sequence
        self.waited = 0  # frames the pending thing has sat through

    def feed(self, b):
        """Read one byte, returning the keys it completes.

        None, one, or -- an ESC followed by an ordinary key -- two.
        """
        self.waited = 0
        if self.state == 1:
            if b in (0x5B, 0x4F):  # [ or O
                self.state = 2
                return []
            self.state = 0
            return [KEY_ESC] + self.feed(b)
        if self.state == 2:
            # Parameter bytes first, then one final byte in @ to ~: a modified
            # arrow is ESC [ 1 ; 5 A, and anything else in that shape is read
            # to its end and dropped rather than leaking its letters into a
            # value.
            if not 0x40 <= b <= 0x7E:
                return []
            self.state = 0
            if b == 0x41:  # A
                return [KEY_UP]
            if b == 0x42:  # B
                return [KEY_DOWN]
            if b == 0x43:  # C
                return [KEY_RIGHT]
            if b == 0x44:  # D
                return [KEY_LEFT]
            return []
        if b == 0x1B:
            self.state = 1
            return []
        if b in (0x0D, 0x0A):
            return [KEY_ENTER]
        if b in (0x7F, 0x08):
            return [KEY_BACK]
        if 0x20 <= b < 0x7F:
            return [chr(b)]
        return []

    def settle(self):
        """End a frame's batch of keys.

        Nothing pending, nothing to do; and anything pending gets one frame's
        grace, in case it is a keystroke the read cut in half. What is still
        there after that was all there was: a bare ESC is the key, and a
        sequence that never finished is dropped rather than left to swallow
        the next key that arrives.
        """
        if self.state == 0:
            return []
        if self.waited < ESC_GRACE:
            self.waited += 1
            return []
        state = self.state
        self.state, self.waited = 0, 0
        return [KEY_ESC] if state == 1 else []


def _terminate(_signum, _frame):
    """Turn SIGTERM into an ordinary unwind, so the cursor comes back.

    Left to its default, SIGTERM kills the process outright: the finally below
    never runs, and the caller is handed a terminal with no cursor and echo
    still off. Go's port already selects on SIGTERM for the same reason.
    """
    raise SystemExit(0)


# Raised by the Ctrl+Z handler, read and cleared by the frame loop. The handler
# does none of the work itself: Python runs handlers between bytecodes, and the
# bytecode it interrupts can be one in the middle of a sys.stdout.write --
# where writing to the same buffer again is a reentrant call the io module
# refuses outright. So the loop does it, at a point where nothing is
# half-written, which is also where clock.go does it: its handler is a channel
# send and the select at the end of the loop is what reads it.
_suspend_asked = False


def _suspend(_signum, _frame):
    global _suspend_asked
    _suspend_asked = True


def suspend(full_screen, restore, requiet):
    """Ctrl+Z: give the terminal back, stop for real, and take it again after.

    kill(2) delivers before it returns, so everything after it runs on resume
    -- cbreak again, the alternate screen again, and a repaint, since what
    SIGCONT comes back to is the screen the shell left rather than the one the
    clock was drawing on.

    The stop is SIGSTOP rather than the usual move, which is to put SIGTSTP's
    default disposition back and raise that at yourself. That move works here
    and does not in clock.go, whose runtime keeps its own SIGTSTP handler
    installed through the reset and then swallows the signal -- see the longer
    note there. Two ports that stopped by different signals would not stop
    alike: what a shell prints for a job differs between the two, "Stopped"
    against bash's "Stopped(SIGSTOP)". The one thing lost is that SIGTSTP is
    discarded when the process group is orphaned, where SIGSTOP is not: a clock
    sent `kill -TSTP` from outside such a group stops where it would once have
    been left running, and wants a `kill -CONT` to come back. Ctrl+Z cannot
    reach it there in the first place -- the terminal driver discards
    job-control signals for an orphaned group too, before any of this is
    reached.
    """
    global _suspend_asked
    _suspend_asked = False

    sys.stdout.write(SHOW_CURSOR + (LEAVE_ALT if full_screen else ""))
    sys.stdout.flush()
    restore()

    os.kill(os.getpid(), signal.SIGSTOP)
    # Raising a stop is not the same as having stopped, and nothing here can
    # ask whether it has: the answer is only ever observed by running again.
    # So the wait is a tick, which cannot finish early and which a clock that
    # really stopped is not running for. clock.go needs it -- a Go process has
    # threads, and the one that takes the signal need not be the one that
    # raised it, which on Linux left it taking the terminal back before the
    # stop landed -- and this one keeps it so the two resume alike.
    time.sleep(TICK)

    requiet()
    sys.stdout.write((ENTER_ALT if full_screen else "") + HIDE_CURSOR)
    sys.stdout.flush()


# The command line the clock ended up reading as, once the tuner has been at
# it: printed by main after the screen has gone back to the shell, since a
# layout worked out inside the alternate screen is lost with it. Empty when
# nothing was changed, which is every redirected run.
LEFT_WITH = ""


def run(argv):
    """Everything that can fail happens before the terminal is touched."""
    global ROWS, CELL_RATIO, COLS, LEFT_WITH
    frozen = freeze()
    # The preferences file first, then the command line on top of it, both
    # through the same parser: a flag typed here beats the same flag saved
    # there because it is read second, which is the only rule either of them
    # needs. -h and --version are refused in a file, where a clock that
    # printed its usage and stopped would be a bad afternoon.
    conf = config_path()
    opt = Options()
    tokens = load_config(conf)  # its own complaint names the file already
    try:
        opt = parse_args(tokens, opt)
    except (HelpRequested, VersionRequested):
        raise ClockError(
            f"{conf}: -h and --version are not settings; delete that line"
        ) from None
    except ClockError as exc:
        raise ClockError(f"{conf}: {exc}") from None
    opt = parse_args(argv, opt)

    vals = opt.vals
    zone_list, quiet = opt.zone_list, opt.quiet
    zones = resolve_zones(zone_list, frozen or datetime.now(timezone.utc))

    color = use_color(opt.color_when)

    # The six knobs, as text, are the whole of the layout state: --cell-ratio
    # has already beaten CLOCK_CELL_RATIO in default_tunes, and a fixed
    # --scale sizes the face here and for good, where --scale auto leaves ROWS
    # to the per-frame search. r re-runs exactly this, which is why it can
    # change any of them without the loop knowing where a value came from.
    settings = read_tunes(vals)
    apply_settings(settings)

    # A pinned clock is a still of one instant, and an undated still records
    # half of it, so the weekday goes under every face unless --day says
    # otherwise. Live, auto keeps it for the clocks that actually disagree.
    day_when = opt.day_when
    if not day_when:
        day_when = "always" if frozen is not None else "auto"
    # The instant the display is holding, or None when it runs live. Holding
    # repaints as usual rather than idling, so a resize still reflows the grid
    # -- it is the clock that stops, not the drawing.
    held = None
    help_on = False
    # The tuner, and the keys feeding it.
    tune = Tuner()
    decoder = KeyDecoder()
    # Said once the tuner closes, in the same modal it used, and dropped at
    # the next key: the flags the layout on screen would need.
    note = ""
    # Off the wall clock, not the frame's: a clock pinned with CLOCK_FREEZE
    # never advances, and the hint still has to give up after three seconds.
    flash_until = time.monotonic() + FLASH_SECONDS

    def save():
        """What S does, and what it says afterwards.

        The clock on screen written back to the preferences file, or why it
        could not be.
        """
        try:
            save_config(conf, config_tokens(vals, zone_list))
        except ClockError as exc:
            return str(exc)
        return f"saved to {conf}"

    def pressed(key, now):
        """Answer one key, and say whether it was the one that quits.

        With the tuner open every key belongs to it -- the values it takes are
        words like "left", "even" and "auto", so q, space and h are letters
        there rather than the keys they are everywhere else. Ctrl+C still
        quits, being the terminal driver's rather than the clock's.

        Nested for the state it changes, which clock.go passes in as pointers
        and this reaches through nonlocal; the two run the same algorithm.
        """
        nonlocal held, help_on, note, settings
        if note:
            # Any key clears the note the tuner left behind; the key still
            # counts, so a reader who went straight for q gets it.
            note = ""
        if tune.open:
            if key in (KEY_UP, KEY_DOWN):
                step = NUM_TUNES - 1 if key == KEY_UP else 1
                tune.sel = (tune.sel + step) % NUM_TUNES
                tune.edit, tune.editing, tune.err = "", False, ""
            elif key in (KEY_LEFT, KEY_RIGHT, " "):
                # Straight onto the clocks: every value stepping can reach has
                # already been through check_tune, so there is nothing to
                # confirm and nothing that can be refused. Space walks the
                # words round their list, where the arrows stop at the ends.
                delta = -1 if key == KEY_LEFT else 1
                following = tune_step(tune.sel, vals[tune.sel], delta, key == " ")
                if following != vals[tune.sel]:
                    vals[tune.sel] = following
                    settings = read_tunes(vals)
                    apply_settings(settings)
                tune.edit, tune.editing, tune.err = "", False, ""
            elif key == KEY_ENTER:
                if tune.editing:
                    try:
                        check_tune(tune.sel, tune.edit)
                    except ClockError as exc:
                        # The value is gone along with the refusal: what is
                        # left of a value the clock will not have is not a
                        # head start on the next one, and leaving it would
                        # make the next character typed land on the end of it.
                        tune.edit, tune.editing, tune.err = "", False, str(exc)
                    else:
                        vals[tune.sel] = canon_tune(tune.sel, tune.edit)
                        settings = read_tunes(vals)
                        apply_settings(settings)
                        tune.edit, tune.editing, tune.err = "", False, ""
            elif key == KEY_BACK:
                if tune.editing:
                    tune.edit = tune.edit[:-1]
                    if not tune.edit:
                        tune.editing = False
            elif key == KEY_ESC:
                # Two things to back out of, innermost first: whatever is
                # being typed, and then the tuner itself.
                if tune.editing:
                    tune.edit, tune.editing, tune.err = "", False, ""
                else:
                    tune.open = False
            elif len(key) == 1:
                tune.edit, tune.editing, tune.err = tune.edit + key, True, ""
            return False
        if key in ("q", "Q"):
            return True
        if key == " ":
            held = None if held is not None else now
        elif key in ("h", "H", "?"):
            help_on = not help_on
        elif key in ("r", "R"):
            tune.open, tune.sel = True, 0
            tune.edit, tune.editing, tune.err = "", False, ""
            help_on = False
        elif key == "S":
            # Capitalised on purpose: it overwrites a file, and a shift is
            # enough to keep an elbow from doing it. Lowercase s is not taken,
            # so a miss does nothing at all.
            note = save()
            help_on = False
        return False

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGTSTP, _suspend)

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
        with quiet_terminal() as (interactive, restore, requiet):
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

                # -n auto's whole point is choosing whatever per-row count
                # lets --scale auto grow the face furthest; with a fixed
                # --scale there is no face size left for it to affect, so it
                # falls back to the plain default cap. Settled per frame
                # rather than once, since the tuner can move the scale
                # between auto and fixed while the clock runs.
                want_per_row = (
                    DEFAULT_PER_ROW if settings.per_row_auto else settings.per_row
                )

                if settings.scale_auto:
                    if cols > 0 and lines > 0:
                        if settings.per_row_auto:
                            want_per_row = len(faces)  # no real cap: see auto_scale's own comment
                        auto_scale(
                            want_per_row, len(faces), cols, lines,
                            settings.hpad, settings.vpad,
                        )
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
                        want_per_row, len(faces), cols,
                        gap_floor(cols, settings.hpad, GAP),
                    )
                    chunks = chunk_count(len(faces), per_row)
                    fit_height(
                        chunks, lines, gap_floor(lines, settings.vpad, VGAP)
                    )
                except ClockError as exc:
                    # A window dragged smaller than the clocks need is
                    # something the reader can undo, so say what is wrong and
                    # keep measuring: the next frame that fits draws itself.
                    # Redirected output has no window to resize and still
                    # fails outright, which is what the diff harness compares.
                    if not full_screen:
                        raise
                    rows = complaint(str(exc), cols, lines, settings.halign)
                else:
                    fits = True

                content = None
                if full_screen and tune.open:
                    # The one modal that shows over a window too small for
                    # the clocks: a scale typed too large is undone from
                    # here, and hiding it would leave nothing to undo it
                    # with.
                    content = tune_rows(tune, vals)
                elif full_screen and note:
                    # Folded, not one line: a note names a file, and a path is
                    # easily longer than a window -- where a modal that does
                    # not fit is a modal not shown at all, which for "cannot
                    # write" would mean a failed save that said nothing.
                    content = fold(note, cols - 4)
                elif fits and full_screen and help_on:
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
                        per_row, cell_cols(), cols, GAP, settings.hpad, settings.halign
                    )
                    vgap, vextra, top, _ = spread(
                        chunks, ROWS + 2, lines, VGAP, settings.vpad, settings.valign
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
                    was_tuning, prev_vals = tune.open, list(vals)
                    keys = []
                    for byte in pending_keys():
                        keys += decoder.feed(byte)
                    # The batch of keys this frame read is over; a keystroke
                    # left half-read waits a frame to be finished before it is
                    # taken for something else. Decided here rather than on a
                    # timer: see KeyDecoder.
                    keys += decoder.settle()
                    quitting = False
                    for key in keys:
                        if pressed(key, now):
                            quitting = True
                    if vals != prev_vals:
                        LEFT_WITH = tune_command(vals, zone_list)
                    if was_tuning and not tune.open:
                        # Closing the tuner says what it would take to start
                        # the clock this way, since the screen it was tuned on
                        # is about to be given back to the shell and take the
                        # answer with it.
                        note = LEFT_WITH
                    if quitting:
                        break
                if _suspend_asked:
                    suspend(full_screen, restore, requiet)
                    # Don't wait out a tick that was interrupted by a stop of
                    # unknown length: the screen resumes blank, and the reader
                    # should not have to watch it stay that way.
                    continue
                time.sleep(TICK - (time.monotonic() % TICK))
    except KeyboardInterrupt:
        pass
    finally:
        # The reader may already be gone, in which case these have nowhere to
        # go and it does not matter: what has to happen on the way out is
        # quiet_terminal's restore, which is an ioctl on stdin and has
        # happened by now. See main() for the rest of that path.
        with contextlib.suppress(BrokenPipeError):
            sys.stdout.write(SHOW_CURSOR + (LEAVE_ALT if full_screen else ""))
            sys.stdout.flush()


def main():
    try:
        run(sys.argv[1:])
        # After run's finally: the alternate screen is gone, and this lands in
        # the shell's own scrollback where it can be copied.
        if LEFT_WITH:
            sys.stdout.write(LEFT_WITH + "\n")
    except HelpRequested:
        sys.stdout.write(USAGE)
    except VersionRequested:
        sys.stdout.write(f"clock {VERSION}\n")
    except ClockError as exc:
        sys.stderr.write(f"clock: {exc}\n")
        raise SystemExit(1)
    except BrokenPipeError:
        # `clock | head`: the reader went away. Not news, and not a traceback
        # -- which is what Python prints for it by default, twenty-odd lines
        # about a normal way for a filter to end, after the terminal has
        # already been given back.
        #
        # stdout is pointed at /dev/null first because the interpreter flushes
        # it once more on the way out, where the same error would surface
        # again as "Exception ignored" and turn the status into 120.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(PIPE_STATUS)


if __name__ == "__main__":
    main()
