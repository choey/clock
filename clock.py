#!/usr/bin/env python3
"""Live terminal clock: one analog face per time zone, digital underneath.

Faces are drawn on a braille canvas (2x4 dots per cell). Refreshes every 19ms,
so the second hand sweeps smoothly rather than stepping. Press q (or Ctrl+C)
to quit.
"""

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

USAGE = """clock - analog terminal clocks

usage: clock [-n N | --per-row N] [ZONES]

  ZONES              comma-separated; default is your local zone
  -n, --per-row N    clocks per row before wrapping (default 3, reduced to fit)
  -h, --help         this message

A zone is an IANA name (Europe/Berlin), a regional abbreviation (ET CT MT PT
AKT HT BST IST JST AET ...), a 2-letter country code (JP, GB), or a US ZIP
code (94110). ET/CT/MT/PT follow daylight saving, so they read EST or EDT
depending on the date; EST/EDT/PST/PDT and the rest are the fixed offsets,
which never shift.

Press q or Ctrl+C to quit.

examples:
  clock
  clock ET,PT,UTC
  clock -n 2 ET,PT,UTC
  clock Europe/Berlin,Asia/Tokyo,94110 --per-row 2
"""

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
GAP = 3  # blank columns between adjacent faces

# The one instant format CLOCK_FREEZE accepts. Exactly six fractional digits,
# exactly UTC: datetime stops at microseconds, and pinning the format keeps both
# implementations rejecting the same strings.
FREEZE_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class ClockError(Exception):
    """A startup failure to report before the terminal has been touched."""


def freeze():
    """The instant to pin the clock to, or None to run live.

    CLOCK_FREEZE draws exactly one frame at a fixed instant, so the Go and
    Python renders can be diffed byte for byte. Dev hook, not in --help.
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

# (length as a fraction of the radius, two dots thick?), drawn shortest-first
HANDS = ((0.50, True), (0.75, True), (0.88, False))

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
    """A dot canvas that renders to braille cells, 2 dots wide by 4 tall each."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.cols = (w + 1) // 2
        self.cells = [[0] * self.cols for _ in range((h + 3) // 4)]

    def set(self, x, y):
        # floor(v + 0.5), not round(): round() is half-to-even here but
        # half-away-from-zero in the Go port, which would split the renders
        x, y = math.floor(snap(x) + 0.5), math.floor(snap(y) + 0.5)
        if 0 <= x < self.w and 0 <= y < self.h:
            self.cells[y // 4][x // 2] |= DOT_BITS[x % 2][y % 4]

    def line(self, x0, y0, x1, y1):
        # snap the endpoints too: steps comes off a rounded difference, and a
        # one-ulp wobble there changes the whole dot sequence, not just one dot
        x0, y0, x1, y1 = snap(x0), snap(y0), snap(x1), snap(y1)
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
        x = snap(cx + rx * MARKER_R * math.sin(a))
        y = snap(cy - ry * MARKER_R * math.cos(a))
        # centre an n-char string on x: it spans 2n dots, so its left edge
        # wants to sit at x - n, snapped to the nearest cell boundary
        col = math.floor((x - len(text) + 0.5) / 2 + 0.5)
        row = math.floor(y + 0.5) // 4
        if 0 <= row < len(rows) and 0 <= col <= canvas.cols - len(text):
            rows[row] = rows[row][:col] + text + rows[row][col + len(text) :]

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


def parse_args(argv):
    """Read the command line: one optional zone list, and -n/--per-row anywhere.

    Hand-rolled rather than argparse, which prints its own usage block, exits
    with status 2, and abbreviates long options -- none of which the Go port
    can reproduce. Both implementations run this algorithm verbatim.
    """
    per_row = DEFAULT_PER_ROW
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
            if name != "per-row":
                raise ClockError(f"unknown option: --{name}")
            if not sep:
                i += 1
                if i >= len(argv):
                    raise ClockError("--per-row needs a number, e.g. --per-row 2")
                val = argv[i]
            per_row = parse_count(val, "--per-row", MAX_PER_ROW)
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
    return per_row, positional[0] if positional else ""


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
# out; these records name each of them exactly, as fixed six-byte "NNNNNc" --
# the full five digits, then a zone letter from ZIP_ZONES. Consulted only when
# all five digits were given, since three cannot say which ZIP is meant.
#
# Sourced from the same Census ZCTA centroids as ZIP_RUNS, so it covers every
# ZIP that has a ZCTA; PO-box and single-building ZIPs have none, and still
# fall back to their prefix. Generated by tools/genzips.py into both clock.py
# and clock.go in one pass -- never edit it by hand.
ZIP_EXCEPTIONS = "32456E36854E36863E36867E36869E36870E36877E37302E37303E37307E37308E37309E37310E37311E37312E37315E37316E37317E37321E37322E37323E37325E37326E37329E37331E37332E37333E37336E37337E37341E37343E37350E37351E37353E37354E37361E37362E37363E37369E37370E37373E37379E37381E37385E37391E37419C37723C40111C40115C40119C40140C40143C40144C40145C40146C40152C40170C40171C40176C40178C42602C42629C42642C42701E42716E42718E42724E42732E42733E42740E42748E42758E42776E42784E42788E46531C46532C46534C47514C47515C47520C47523C47525C47531C47537C47550C47551C47552C47574C47576C47577C47579C47586C47588C47922C47943C47948C47951C47963C47964C47977C47978C49801C49802C49812C49815C49821C49831C49834C49847C49848C49852C49858C49863C49870C49873C49874C49876C49877C49881C49886C49887C49892C49893C49896C49902C49903C49911C49915C49920C49927C49935C49938C49947C49959C49968C49969C57532M57537M57543M57547M57551M57552M57553M57567M57574M57577M57631C57632C57646C57648C58529M58533M58562M58564M58569M58631C58638C58838M67733M67735M67741M67758M67761M67762M67836M67857M67878M67879M69021M69023M69027M69030M69033M69037M69041M69045M69101C69120C69123C69130C69132C69135C69138C69142C69143C69151C69157C69163C69165C69166C69167C69169C69170C69171C69211M69216M69218M69219M79821M79835M79836M79837M79838M79839M79847M79849M79851M79853M83522M83542M83547M83549M86003M86016M86020M86031M86033M86034M86035M86040M86044M86045M86047M86053M86054M86502Z96799S97904P97905P97907P99546D99547D"  # zip-exceptions: generated by tools/genzips.py


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
    """The zone for one exact 5-digit ZIP, or "" if the prefix gets it right.

    The same fixed-width binary search as zip_lookup, over six-byte records.
    """
    lo, hi = 0, len(ZIP_EXCEPTIONS) // 6 - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        key = ZIP_EXCEPTIONS[mid * 6 : mid * 6 + 5]
        if key == zip5:
            return ZIP_ZONES.get(ZIP_EXCEPTIONS[mid * 6 + 5], "")
        if key < zip5:
            lo = mid + 1
        else:
            hi = mid - 1
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


def unknown_zone(token):
    return ClockError(
        f'unknown zone "{token}"; use an IANA name (Europe/Berlin), '
        f"an abbreviation ({alias_names()}), a 2-letter country code (JP), "
        "or a US ZIP code"
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


def truncate(s, n):
    """Cut s to n characters."""
    n = max(0, n)
    return s if len(s) <= n else s[:n]


def digital(t):
    """The clock under one face, always 12 characters."""
    return f"{t:%H:%M:%S}.{t.microsecond // 1000:03d}"


def chunk_count(n, per_row):
    """How many rows of faces per_row produces."""
    return -(-n // per_row)


def frame_height(chunks):
    """The rows a grid occupies: each chunk is a face, its zone name and its
    digital line, and chunks are separated by one blank row."""
    return chunks * (ROWS + 2) + (chunks - 1)


def frame(faces, now, per_row):
    """Draw the whole grid: faces left to right, wrapping every per_row.

    A short last row is left-aligned so the column gutters stay lined up.
    """
    gap = " " * GAP
    rows = []
    for ci in range(chunk_count(len(faces), per_row)):
        chunk = faces[ci * per_row : (ci + 1) * per_row]
        if ci > 0:
            rows.append("")
        times = [in_zone(now, z) for _, z in chunk]
        drawn = [face(t) for t in times]
        rows.extend(gap.join(row) for row in zip(*drawn))
        rows.append(
            gap.join(f"{truncate(label, COLS):^{COLS}}" for label, _ in chunk)
        )
        rows.append(gap.join(f"{digital(t):^{COLS}}" for t in times))
    return rows


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


def fit_per_row(want, n, term_cols):
    """Reduce the requested faces-per-row to what the window can hold.

    Wrapping is what actually breaks the display: a wrapped line desynchronises
    the cursor rewind and the frame smears.
    """
    want = min(want, n)
    if term_cols <= 0:
        return want  # not a terminal: honour what was asked for
    max_fit = (term_cols + GAP) // (COLS + GAP)
    if max_fit < 1:
        raise ClockError(
            f"terminal is {term_cols} columns wide and one clock face needs "
            f"{COLS}; widen the window, or lower CLOCK_CELL_RATIO"
        )
    return min(want, max_fit)


def fit_height(chunks, term_rows):
    """Reject a grid taller than the window.

    Too tall scrolls, and scrolling desynchronises the rewind exactly as
    wrapping does -- but here the fix is to raise --per-row, not lower it.
    """
    height = frame_height(chunks)
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


def wants_quit():
    """True if a q is waiting on stdin. Never blocks; assumes cbreak mode."""
    if not select.select([sys.stdin], [], [], 0)[0]:
        return False
    return b"q" in os.read(sys.stdin.fileno(), 64).lower()


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
    want_per_row, zone_list = parse_args(argv)
    zones = resolve_zones(zone_list, frozen or datetime.now(timezone.utc))

    signal.signal(signal.SIGTERM, _terminate)
    sys.stdout.write(HIDE_CURSOR)
    height = 0
    try:
        with quiet_terminal() as interactive:
            while True:
                now = frozen if frozen is not None else datetime.now(timezone.utc)

                # re-measure every frame rather than trapping SIGWINCH: one
                # ioctl per 19ms is nothing beside redrawing the faces, it also
                # picks up a changed COLUMNS, and under PEP 475 a signal
                # handler would interact with the sleep below and drift out of
                # step with the Go port's loop.
                cols, lines = term_size()
                faces = merge_zones(zones, now)
                per_row = fit_per_row(want_per_row, len(faces), cols)
                fit_height(chunk_count(len(faces), per_row), lines)
                rows = frame(faces, now, per_row)

                # rewind over the rows drawn last time, repaint in one write.
                # CLEAR_EOL wipes a longer previous line, CLEAR_BELOW a taller
                # previous frame -- the grid reshapes itself on a resize.
                rewind = f"\x1b[{height}A" if height else ""
                sys.stdout.write(
                    rewind
                    + "".join(r + CLEAR_EOL + "\n" for r in rows)
                    + CLEAR_BELOW
                )
                sys.stdout.flush()
                height = len(rows)

                if frozen is not None:
                    break
                if interactive and wants_quit():
                    break
                time.sleep(TICK - (time.monotonic() % TICK))
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(SHOW_CURSOR)
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
