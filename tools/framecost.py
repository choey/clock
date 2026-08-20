#!/usr/bin/env python3
"""What one frame costs, against the 19ms the clock has to draw it in.

Not a check and not in `make test`: the answer is a property of the machine it
runs on, so there is no threshold to fail that would not fail on someone's
laptop under load. It is here because the question comes up -- a clock that
repaints fifty times a second, in two languages, one of them Python -- and
guessing at it is worse than spending a minute measuring.

The method is CLOCK_FRAMES, which draws that many frames from a pinned instant
and exits: one frame is startup, and the difference between one and many is
the frames themselves, divided out. Best of three, since the slow runs are the
machine's other work and the fast ones are the clock's.

    tools/framecost.py [-v]
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FROZEN = "2026-07-15T09:53:07.123456Z"
TICK_MS = 19  # what the clock has between repaints; see ARCHITECTURE.md
COLS, LINES = 200, 60
FRAMES = 201  # 200 frames of work, plus the one that is startup
RUNS = 3

# One face, a plausible grid, and enough faces that the window is full: the
# per-frame cost is mostly the faces, so the interesting number is how it grows.
CASES = ("UTC", "ET,PT,UTC,JP", "ET,PT,UTC,JP,GB,NZ,IST,AET,BST,KST,HKT,SGT")


def timed(argv, zones, frames):
    """The best wall time of RUNS runs drawing `frames` frames, in seconds."""
    env = dict(
        os.environ,
        CLOCK_FREEZE=FROZEN,
        CLOCK_FRAMES=str(frames),
        CLOCK_STEP=str(TICK_MS),
        COLUMNS=str(COLS),
        LINES=str(LINES),
    )
    best = None
    for _ in range(RUNS):
        started = time.monotonic()
        subprocess.run(
            argv + [zones],
            env=env,
            cwd=ROOT,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        spent = time.monotonic() - started
        best = spent if best is None else min(best, spent)
    return best


def main():
    verbose = "-v" in sys.argv[1:]
    print("== building ==")
    # Into a directory of its own rather than the tree, so nothing is left
    # behind by a Ctrl+C and two runs cannot collide over one name.
    built = tempfile.TemporaryDirectory(prefix="clock-framecost.")
    binary = str(Path(built.name) / "clock")
    subprocess.run(["go", "build", "-o", binary, "."], cwd=ROOT, check=True)
    impls = (("go", [binary]), ("py", [sys.executable, "clock.py"]))
    try:
        print(f"\n{COLS}x{LINES}, {FRAMES - 1} frames, best of {RUNS}\n")
        print(f"{'':4} {'faces':>5} {'startup':>9} {'per frame':>10} {'of a tick':>10}")
        for zones in CASES:
            for impl, argv in impls:
                one = timed(argv, zones, 1)
                many = timed(argv, zones, FRAMES)
                per = (many - one) / (FRAMES - 1)
                if verbose:
                    print(f"     {zones}")
                print(
                    f"{impl:4} {zones.count(',') + 1:5} {one * 1000:8.1f}ms "
                    f"{per * 1000:9.3f}ms {per * 1000 / TICK_MS:9.1%}"
                )
    finally:
        built.cleanup()

    # Neither clock sleeps for a tick after the work, which would make the
    # period the tick plus the frame: clock.py sleeps to the next multiple of
    # the tick on a monotonic clock, and clock.go takes a ticker, whose channel
    # holds one tick and drops the rest. So a frame that costs more than it has
    # does not push the next one late -- it loses it. A share over 100% is a
    # clock that has stopped sweeping smoothly, not one running behind.
    print(f"\n(the tick is {TICK_MS}ms; over 100% is a dropped frame, not a late one)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
