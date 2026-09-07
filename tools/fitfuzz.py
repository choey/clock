#!/usr/bin/env python3
"""Throw window sizes at the clock and check what comes back fits in them.

difftest compares the two implementations, and the goldens pin sixteen
renderings. Between them they say nothing about the seventeenth window size: a
grid that overflows its terminal overflows it in both implementations, agrees
with itself, and matches no golden because no golden has that size.

So this renders at many sizes and checks two things that must hold at every
one of them -- no line wider than the window, and no more lines than the
window has -- because a line that wraps or a frame that scrolls desynchronises
the repaint, which is the one thing the alternate screen cannot fix.

    tools/fitfuzz.py [rounds]        (default 400, deterministic)
"""

import random
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FROZEN = "2026-07-15T09:53:07.123456Z"

# Same instant every run, same sizes every run: a fuzz that cannot be repeated
# is a bug report nobody can act on. Change the seed to search elsewhere.
SEED = 20260715

ZONES = ["UTC", "ET,PT", "ET,PT,UTC", "ET,PT,UTC,JP", "ET,PT,UTC,JP,GB,NZ",
         "94110", "ET,PT,UTC,JP,GB,NZ,IN,CN,BR,ZA",
         # More faces than any window here can hold in one row, so the grid
         # wraps and the short last row's gutters have to line up with the
         # rows above it.
         ",".join(["UTC", "ET", "PT", "MT", "CT", "JST", "GMT", "IST", "NZT",
                   "AKT", "HT", "BST", "CET", "SGT", "KST", "AET", "UTC", "ET"])]

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
READOUT = re.compile(r"(?:[A-Z][a-z][a-z] )?\d\d:\d\d:\d\d\.\d\d\d")
SGR = re.compile(r"\x1b\[([0-9;]*)m")

# Every escape the clock is allowed to write. Anything else is either a bug or
# a deliberate addition that should be added here on purpose.
ALLOWED = {("", "J"), ("", "K"), ("31", "m"), ("33", "m"), ("36", "m"),
           ("39", "m"), ("?25", "h"), ("?25", "l")}


def widths(painted):
    """Each rendered line's width in terminal columns, escapes removed.

    Every character the clock draws is one column wide: braille, digits,
    letters, spaces. Nothing it emits is double width or combining, so
    counting characters is counting columns.
    """
    body = ANSI.sub("", painted)
    return [len(line) for line in body.split("\n") if line != ""]


def bleeds(painted):
    """Lines that end with a colour still in force.

    Each face's cells set a colour and put it back; a line that ends without
    putting it back paints the rest of the terminal's row, and the next line's
    margin, in whatever the last hand happened to be.
    """
    out = []
    for i, line in enumerate(painted.split("\n")):
        state = "39"
        for found in SGR.finditer(line):
            state = found.group(1) or "0"
        if state not in ("39", "0"):
            out.append(i)
    return out


def margins(painted, lines):
    """(left, right, top, bottom) blank space around what was drawn.

    The bottom is measured against the window rather than against the output:
    a clock leaves the rows under itself alone and lets clear-below deal with
    them, so a bottom margin is a thing that was never written. Blank rows
    above it are written, since something has to push the grid down.
    """
    rows = ANSI.sub("", painted).split("\n")
    while rows and rows[-1] == "":
        rows.pop()
    used = [i for i, line in enumerate(rows) if line.strip()]
    if not used:
        return None
    left = min(len(line) - len(line.lstrip(" ")) for line in rows if line.strip())
    right = max(len(line.rstrip(" ")) for line in rows)
    return left, right, used[0], lines - (used[-1] + 1)


def render(binary, cols, lines, zones, extra):
    proc = subprocess.run(
        [*binary, *extra, zones],
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "CLOCK_FREEZE": FROZEN,
             "COLUMNS": str(cols), "LINES": str(lines)},
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    subprocess.run(["go", "build", "-o", "clock-fitfuzz", "."], cwd=ROOT, check=True)
    # Both, not one: the invariant is each implementation's to keep, and a
    # window that overflows in one and refuses in the other is difftest's to
    # catch, not this.
    binaries = ([str(ROOT / "clock-fitfuzz")], [sys.executable, "pyclock.py"])

    rng = random.Random(SEED)
    checked = failures = complained = 0
    try:
        for _ in range(rounds):
            cols = rng.choice([rng.randint(1, 40), rng.randint(40, 120), rng.randint(120, 400)])
            lines = rng.choice([rng.randint(1, 12), rng.randint(12, 40), rng.randint(40, 120)])
            zones = rng.choice(ZONES)
            align = rng.choice([[], ["--halign", "left"], ["--halign", "center"],
                                ["--halign", "right"], ["--valign", "top"],
                                ["--valign", "center"], ["--valign", "bottom"]])
            extra = rng.choice([[], ["-n", "1"], ["-n", "2"], ["-n", "auto"],
                                ["--scale", "1"], ["--scale", "2"], ["--scale", "auto"],
                                ["--hpad", "10%"], ["--vpad", "20%"],
                                ["--hpad", "even"], ["--vpad", "even"],
                                ["--color=always"], ["--color=always", "--day=always"]]) + align
            impl, binary = rng.choice(list(zip(("go", "py"), binaries)))
            status, painted, complaint = render(binary, cols, lines, zones, extra)
            what = f"{impl} {cols}x{lines} {' '.join(extra)} {zones}"

            if status != 0:
                # Refusing is allowed -- a window can be too small for any
                # arrangement -- but it has to say so rather than die.
                if not complaint.startswith("clock: "):
                    print(f"FAIL {what}: exit {status} with no complaint: {complaint[:80]!r}")
                    failures += 1
                complained += 1
                continue

            checked += 1
            over = [w for w in widths(painted) if w > cols]
            if over:
                print(f"FAIL {what}: {len(over)} lines wider than the window, "
                      f"worst {max(over)} columns")
                failures += 1
            tall = len(widths(painted))
            if tall > lines:
                print(f"FAIL {what}: {tall} lines drawn into a {lines}-line window")
                failures += 1
            left = bleeds(painted)
            if left:
                print(f"FAIL {what}: {len(left)} lines end with a colour still set")
                failures += 1
            stray = set(re.findall(r"\x1b\[([0-9;?]*)([A-Za-z])", painted))
            if stray - ALLOWED:
                print(f"FAIL {what}: escapes the clock should not write: {sorted(stray - ALLOWED)}")
                failures += 1

            # One readout per face, so counting them per line counts the
            # faces in that row. -n is a cap on that count, and the only way
            # a row can hold more than it is if the wrap is not working.
            plain = ANSI.sub("", painted)
            per_line = [len(READOUT.findall(line)) for line in plain.split("\n")]
            if "-n" in extra:
                asked = extra[extra.index("-n") + 1]
                if asked != "auto" and max(per_line, default=0) > int(asked):
                    print(f"FAIL {what}: a row holds {max(per_line)} faces, -n said {asked}")
                    failures += 1

            # --day=always puts a weekday on every readout, or on none of them
            # if the faces are too narrow to hold one -- never on some.
            if "--day=always" in extra:
                dated = [len(re.findall(r"[A-Z][a-z][a-z] \d\d:", line)) for line in plain.split("\n")]
                if any(0 < d < n for d, n in zip(dated, per_line)):
                    print(f"FAIL {what}: some readouts in a row carry a weekday and some do not")
                    failures += 1

            # Where the grid sits, when it was told where to sit. Only the
            # edge it was pushed against is checked: the other one is
            # whatever is left over, and centring is checked as balance
            # rather than as a number.
            edges = margins(painted, lines)
            if edges and align:
                left, right, top, bottom = edges
                axis, where = align
                complaint = None
                if axis == "--valign":
                    where = {"top": "top", "center": "vcenter", "bottom": "bottom"}[where]
                if where == "left" and left != 0:
                    complaint = f"left-aligned but {left} columns of margin"
                elif where == "right" and right != cols:
                    complaint = f"right-aligned but ends at column {right} of {cols}"
                elif where == "center" and abs(left - (cols - right)) > 1:
                    complaint = f"centred but margins are {left} and {cols - right}"
                elif where == "vcenter" and abs(top - bottom) > 1:
                    complaint = f"centred but margins are {top} and {bottom} rows"
                elif where == "top" and top != 0:
                    complaint = f"top-aligned but {top} rows of margin"
                elif where == "bottom" and bottom != 0:
                    complaint = f"bottom-aligned but {bottom} rows of margin"
                if complaint:
                    print(f"FAIL {what}: {complaint}")
                    failures += 1
    finally:
        (ROOT / "clock-fitfuzz").unlink(missing_ok=True)

    print(f"\n{checked} renderings fit, {complained} windows refused, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
