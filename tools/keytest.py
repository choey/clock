#!/usr/bin/env python3
"""Prove the two clocks answer the keyboard alike.

The other half of tools/difftest.sh. Space, h, ? and q are read only from a
terminal in cbreak mode, so nothing redirected ever presses one: difftest
compares the table the key list is built from, not what pressing h does. This
runs each implementation under a pty, gives both the same keys at the same
points, and compares what they paint.

A clock repaints every 19ms whether or not anything changed, so how many
repaints land between two keystrokes is timing, not behaviour. The streams are
therefore collapsed to their *distinct* frames before comparing -- frames are
delimited by the cursor-home each full-screen repaint starts with. Pinned with
CLOCK_FREEZE, every repaint draws the same bytes, so the collapsed sequence is
exactly the sequence of states the keys walked through.

    tools/keytest.py [-v]
"""

import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Pinned, so every repaint between two keystrokes is the same bytes and the
# collapse below leaves one frame per state rather than however many ticks fit.
FROZEN = "2026-07-15T09:53:07.123456Z"
COLS, LINES = 80, 24

HOME = b"\x1b[H"
ENTER_ALT, LEAVE_ALT = b"\x1b[?1049h", b"\x1b[?1049l"
SHOW_CURSOR = b"\x1b[?25h"

# Long enough for several repaints to land, short enough to keep the suite
# quick. Nothing depends on how many land -- only that at least one does.
SETTLE = 0.25


def run(argv, keys, freeze=FROZEN, settle=SETTLE, sizes=None):
    """One clock under a pty, fed `keys`, returning everything it painted.

    Each key is written after the frames from the previous one have had time to
    land, so the sequence of states is the sequence of keystrokes.

    `sizes` resizes the window instead, to each in turn. The clock reads
    COLUMNS and LINES ahead of the terminal itself, so those are left unset for
    a resize run -- otherwise it would keep drawing the old size at a window
    that had changed, which is the bug this would be trying to find.
    """
    master, slave = pty.openpty()
    # The clock reads COLUMNS/LINES first and the ioctl second; set both, so it
    # cannot matter which one it happens to believe.
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", LINES, COLS, 0, 0))
    env = dict(os.environ, COLUMNS=str(COLS), LINES=str(LINES), TERM="xterm-256color")
    if sizes is not None:
        env.pop("COLUMNS", None)
        env.pop("LINES", None)
    if freeze:
        env["CLOCK_FREEZE"] = freeze
    else:
        env.pop("CLOCK_FREEZE", None)

    # Echo off before the child starts. It turns echo off itself, in cbreak,
    # but not until it has started -- and a key written into that window comes
    # straight back out as text in the middle of the frames. That is a fault in
    # the harness, not the clock, and it only shows on a slow or loaded
    # machine, which is the worst way to find out.
    mode = termios.tcgetattr(slave)
    mode[3] &= ~(termios.ECHO | termios.ICANON)  # lflag
    # Output post-processing off too, which is not about echo at all: with it
    # on, the driver turns each \n into \r\n, and at a write boundary it can
    # emit the \r twice for one newline. Whether that happens depends on how a
    # 2.8KB frame gets split into write() calls, which differs between a Go
    # string printed whole and a Python buffer flushed -- so it would be the
    # terminal's chunking under comparison, not the clocks'. With OPOST off
    # both write the same 17 newlines and no carriage returns at all.
    mode[1] &= ~termios.OPOST  # oflag
    termios.tcsetattr(slave, termios.TCSANOW, mode)

    proc = subprocess.Popen(
        argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=ROOT, close_fds=True
    )
    os.close(slave)

    out = bytearray()

    def drain(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([master], [], [], 0.02)
            if not ready:
                continue
            try:
                chunk = os.read(master, 1 << 16)
            except OSError:  # the child closed it: it has quit
                return False
            if not chunk:
                return False
            out.extend(chunk)
        return True

    # Wait for the clock to be up rather than assuming it is: a key pressed
    # before the first frame is a key pressed at a program that is not
    # listening yet.
    ready = time.monotonic() + 5
    while time.monotonic() < ready:
        if ENTER_ALT in out and HOME in out:
            break
        if not drain(0.05):
            break
    drain(settle)

    for cols, lines in sizes or ():
        if proc.poll() is not None:
            break
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", lines, cols, 0, 0))
        if not drain(settle):
            break

    for key in keys:
        if proc.poll() is not None:
            break
        try:
            os.write(master, key)
        except OSError:  # it quit between the poll above and this write
            break
        if not drain(settle):
            break

    if proc.poll() is None:
        try:
            os.write(master, b"q")  # every case has to end with the clock gone
        except OSError:
            pass
        drain(settle)
    try:
        status = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        status = "hung"
    drain(0.05)
    os.close(master)
    return bytes(out), status


def states(painted):
    """The distinct frames in what was painted, in order.

    Consecutive repeats collapse: a pinned clock paints the same frame every
    19ms, and how many of those landed is the machine's speed, not the clock's
    behaviour.
    """
    head, *frames = painted.split(HOME)
    out = [head]
    for frame in frames:
        if not out or frame != out[-1]:
            out.append(frame)
    return out


# A line only the key list carries, so its presence in a frame says the modal
# is up. Taken from the table both implementations build it from.
HELP_MARK = b"hold the frame"


def helped(state_list):
    """Which of the painted states had the key list up."""
    return [HELP_MARK in s for s in state_list[1:]]


def readouts(painted):
    """Every digital readout painted, in order, as they appear on the faces."""
    import re

    return re.findall(rb"\d\d:\d\d:\d\d\.\d\d\d", painted)


IMPLS = (("go", ["./clock-keytest"]), ("py", [sys.executable, "clock.py"]))

passed = failed = 0
verbose = "-v" in sys.argv[1:]


def report(ok, name, detail=""):
    global passed, failed
    if ok:
        passed += 1
        if verbose:
            print(f"ok   {name}")
    else:
        failed += 1
        print(f"FAIL {name}{': ' + detail if detail else ''}")


def compare(name, keys, expect=None, freeze=FROZEN, loose=False):
    """Both implementations, same keys: same states, same exit status.

    `expect` is the key list's state through the run -- one flag per distinct
    frame painted, which is what the keys were pressed to change.

    `loose` drops the frame-by-frame comparison and keeps the rest. A keystroke
    is read at a known point in the loop, so the frames either side of it are
    the same every run; a signal is not, and can land before or after a repaint
    that was already on its way. Comparing counts there would be comparing the
    scheduler.
    """
    seen = {}
    for impl, argv in IMPLS:
        painted, status = run(argv + ["-q", "UTC"], keys, freeze=freeze)
        seen[impl] = (states(painted), status, painted)

    (go_states, go_status, go_painted) = seen["go"]
    (py_states, py_status, py_painted) = seen["py"]

    if go_status != py_status:
        report(False, name, f"exit status go={go_status} py={py_status}")
        return
    if not loose:
        if len(go_states) != len(py_states):
            report(False, name, f"{len(go_states)} states from go, {len(py_states)} from py")
            return
        for i, (g, p) in enumerate(zip(go_states, py_states)):
            if g != p:
                report(False, name, f"state {i} differs\n  go: {g[:120]!r}\n  py: {p[:120]!r}")
                return
    elif set(helped(go_states)) != set(helped(py_states)):
        report(False, name, "the key list ended up in different states")
        return
    if expect is not None:
        want = [bool(f) for f in expect]
        got = helped(go_states)
        if (set(got) != set(want)) if loose else (got != want):
            report(False, name, f"key list went {got}, expected {want}")
            return
    # However it ended, the screen has to be given back.
    for impl, (_, _, painted) in seen.items():
        if ENTER_ALT in painted and LEAVE_ALT not in painted:
            report(False, name, f"{impl} left the alternate screen up")
            return
        if SHOW_CURSOR not in painted:
            report(False, name, f"{impl} left the cursor hidden")
            return
    report(True, name)


# A frame with no braille on it drew no clock face, which on a terminal means
# the window was too small and the clock said so instead -- a complaint it
# clears again when the window grows, where redirected output would have
# exited. Nothing but a resize reaches that path.
#
# Recognised by what is missing rather than by the words: the complaint wraps
# to the window, and at twelve columns "widen the window" is spread over three
# lines with nothing to match against.
BRAILLE = (b"\xe2\xa0", b"\xe2\xa1", b"\xe2\xa2", b"\xe2\xa3")


def complaining(frame):
    return not any(prefix in frame for prefix in BRAILLE)


def compare_resize(name, extra, sizes, expect_complaint):
    """Both implementations, same window sizes: same frames, same complaints.

    The clock re-measures every frame rather than trapping SIGWINCH, so a
    window dragged smaller reflows on the next tick -- and dragged smaller than
    the clocks can fit, says so and keeps measuring until it fits again.
    """
    seen = {}
    for impl, argv in IMPLS:
        painted, status = run(argv + ["-q"] + extra, [], sizes=sizes)
        seen[impl] = (states(painted), status)

    (go_states, go_status), (py_states, py_status) = seen["go"], seen["py"]
    if go_status != py_status:
        report(False, name, f"exit status go={go_status} py={py_status}")
        return
    if len(go_states) != len(py_states):
        report(False, name, f"{len(go_states)} states from go, {len(py_states)} from py")
        return
    for i, (g, p) in enumerate(zip(go_states, py_states)):
        if g != p:
            report(False, name, f"state {i} differs\n  go: {g[:160]!r}\n  py: {p[:160]!r}")
            return
    complained = [complaining(s) for s in go_states[1:]]
    if complained != [bool(f) for f in expect_complaint]:
        report(False, name, f"complaints went {complained}, expected {expect_complaint}")
        return
    report(True, name)


def compare_hold(name):
    """Space holds the clock, and pressing it again lets go.

    A live clock is the only one with anything to hold, so this cannot compare
    bytes between two runs at two different instants. It compares what each
    implementation *did*: how many distinct readouts it painted while held, and
    while running.
    """
    result = {}
    for impl, argv in IMPLS:
        painted, status = run(argv + ["-q", "UTC"], [b" ", b" "], freeze=None, settle=0.6)
        chunks = painted.split(HOME)
        stamps = [readouts(c) for c in chunks]
        flat = [s[0] for s in stamps if s]
        result[impl] = (painted, status, flat)

    for impl, (painted, status, flat) in result.items():
        held_runs = 1
        longest = 1
        for a, b in zip(flat, flat[1:]):
            held_runs = held_runs + 1 if a == b else 1
            longest = max(longest, held_runs)
        distinct = len(set(flat))
        # Held for 0.6s at 19ms a frame is ~30 identical readouts; running, a
        # readout repeats at most a couple of times. Ten is far above the one
        # and far below the other.
        if longest < 10:
            report(False, name, f"{impl}: space did not hold ({longest} repeats at most)")
            return
        if distinct < 5:
            report(False, name, f"{impl}: the clock never ran ({distinct} distinct readouts)")
            return
        if status != 0:
            report(False, name, f"{impl}: exit status {status}")
            return
    report(True, name)


def main():
    print("== building ==")
    subprocess.run(["go", "build", "-o", "clock-keytest", "."], cwd=ROOT, check=True)
    try:
        print("== keys ==")
        # The flags are the key list through the run, one per distinct frame:
        # the clock, then whatever the keys did to it, then the frame it went
        # out on. A run that ends with the list up ends on a frame with it up.
        compare("h opens the key list, h closes it", [b"h", b"h"], [0, 1, 0, 0])
        compare("? opens it too", [b"?", b"?"], [0, 1, 0, 0])
        compare("h opens, ? closes", [b"h", b"?"], [0, 1, 0, 0])
        compare("quitting with the list up", [b"h", b"q"], [0, 1, 1])
        compare("keys it does not know paint nothing new", [b"x", b"z"], [0, 0])
        compare("q quits", [b"q"], [0, 0])
        compare("Q quits too", [b"Q"], [0, 0])
        compare("Ctrl+C quits", [b"\x03"], [0], loose=True)
        compare("space on a pinned clock has nothing to hold", [b" ", b" "], [0, 0])
        # The clock re-measures every frame rather than trapping SIGWINCH, so
        # this is the only thing that can drag a window: difftest pins one size
        # per run and never changes it.
        print("== resize ==")
        compare_resize("shrink, then grow", ["ET,PT,UTC"], [(40, 12), (200, 50)], [0, 0, 0, 0])
        compare_resize("a nudge that changes nothing", ["ET,PT,UTC"], [(80, 24)], [0, 0])
        compare_resize("auto scale follows the window down", ["-n", "auto", "ET,PT,UTC"],
                       [(200, 50), (60, 20), (200, 50)], [0, 0, 0, 0, 0])
        # At a fixed scale the faces cannot shrink, so a small enough window is
        # one they do not fit -- which on a terminal complains and keeps
        # measuring, where redirected it would have exited. Nothing else
        # reaches that path.
        compare_resize("too narrow to fit, then wide enough again", ["--scale", "1", "ET,PT,UTC"],
                       [(12, 20), (120, 40)], [0, 1, 0, 0])
        compare_resize("too short to fit, then tall enough again", ["--scale", "1", "ET,PT,UTC"],
                       [(120, 6), (120, 40)], [0, 1, 0, 0])

        print("== hold ==")
        compare_hold("space holds a running clock, and again lets go")
    finally:
        (ROOT / "clock-keytest").unlink(missing_ok=True)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
