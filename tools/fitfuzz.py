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
         "94110", "ET,PT,UTC,JP,GB,NZ,IN,CN,BR,ZA"]

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def widths(painted):
    """Each rendered line's width in terminal columns, escapes removed.

    Every character the clock draws is one column wide: braille, digits,
    letters, spaces. Nothing it emits is double width or combining, so
    counting characters is counting columns.
    """
    body = ANSI.sub("", painted)
    return [len(line) for line in body.split("\n") if line != ""]


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
    binaries = ([str(ROOT / "clock-fitfuzz")], [sys.executable, "clock.py"])

    rng = random.Random(SEED)
    checked = failures = complained = 0
    try:
        for _ in range(rounds):
            cols = rng.choice([rng.randint(1, 40), rng.randint(40, 120), rng.randint(120, 400)])
            lines = rng.choice([rng.randint(1, 12), rng.randint(12, 40), rng.randint(40, 120)])
            zones = rng.choice(ZONES)
            extra = rng.choice([[], ["-n", "1"], ["-n", "2"], ["-n", "auto"],
                                ["--scale", "1"], ["--scale", "2"], ["--scale", "auto"],
                                ["--halign", "right"], ["--valign", "bottom"],
                                ["--hpad", "4"], ["--vpad", "2"]])
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
    finally:
        (ROOT / "clock-fitfuzz").unlink(missing_ok=True)

    print(f"\n{checked} renderings fit, {complained} windows refused, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
