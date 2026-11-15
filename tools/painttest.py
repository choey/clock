#!/usr/bin/env python3
"""Prove the terminal repaint draws the same screen as repainting everything.

On a terminal neither clock writes a whole frame any more: it writes the rows
that differ from the ones already up and leaves the rest alone (see `painted`
in clock.go and pyclock.py). That is a shortcut nothing else here can see --
difftest redirects, so it compares the frames, not the painting -- and getting
it wrong leaves a stale row on screen that no later frame comes back for.

So this runs each clock under a pty, replays what it painted through a model
of a terminal, and compares that screen against the one the same frames would
have produced painted whole. Byte-for-byte on the screen, not on the stream:
what the shortcut is allowed to change is how the screen is reached.

It also holds the shortcut to being one. Given a sequence of identical frames,
a clock that repaints what has not changed is not failing the comparison above
-- so the no-op case is checked for silence directly, and a resize is checked
for the opposite: the screen has changed shape underneath, and the whole frame
has to go back down.

    tools/painttest.py [-v]
"""

import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from termscreen import (  # noqa: E402  (after the path above)
    CLEAR_BELOW,
    CLEAR_EOL,
    ENTER_ALT,
    HIDE_CURSOR,
    HOME,
    LEAVE_ALT,
    SHOW_CURSOR,
    Screen,
)

ROOT = Path(__file__).resolve().parent.parent

# No preferences file, ever: a clock reads one at startup, and whether the
# machine running this happens to have one is not something the comparison
# below may depend on. Empty means none at all.
os.environ["CLOCK_CONFIG"] = ""

# Never a terminal's own default either: iTerm2 starts the readout at seconds,
# found by TERM_PROGRAM, so a run of this from inside it would compare frames
# drawn to a different default from everyone else's. The cases that want it
# set it themselves.
os.environ["TERM_PROGRAM"] = ""

FROZEN = "2026-07-15T09:53:07.123456Z"

# -q, every case: on a terminal a clock flashes a hint over the frame for its
# first three seconds, and a sequence of frames drawn back to back is all
# inside them. Redirected there is no hint, so leaving it on would compare a
# screen with a modal on it against frames without one -- a difference in what
# was drawn, which is difftest's question, not this one.
QUIET = ["-q"]
# Colour has to be asked for rather than left to the clock: it colours a
# terminal and not a file, and the screen under a pty is compared against
# frames written to a file.
PLAIN = QUIET + ["--no-color"]
def frames_of(redirected):
    """The whole frames a redirected run wrote, as lists of rows.

    A redirected run brackets its frames with the cursor escapes and separates
    them with the rewind; neither is a row, and what is left after the last
    erase-below is nothing at all.
    """
    body = redirected.replace(HIDE_CURSOR, "").replace(SHOW_CURSOR, "")
    out = []
    for chunk in body.split(CLEAR_BELOW):
        chunk = re.sub(r"^\x1b\[\d+A", "", chunk)
        if not chunk:
            continue
        rows = [r[: -len(CLEAR_EOL)] if r.endswith(CLEAR_EOL) else r
                for r in chunk.split("\n")]
        if rows and rows[-1] == "":
            rows.pop()
        out.append(rows)
    return out


def painted_whole(frames):
    """What the painter this replaced would have written for those frames."""
    body = "".join(
        HOME + "\n".join(r + CLEAR_EOL for r in rows) + CLEAR_BELOW for rows in frames
    )
    return ENTER_ALT + body


def env_for(cols, lines, frames="", step="", **extra):
    """The environment for one run. A size of None leaves COLUMNS and LINES
    unset, which is the only way the window itself gets a say: both clocks
    take those over the ioctl, so a run that is to be resized cannot name
    them."""
    return dict(
        os.environ,
        CLOCK_FREEZE=FROZEN,
        CLOCK_FRAMES=frames,
        CLOCK_STEP=step,
        COLUMNS=str(cols) if cols else "",
        LINES=str(lines) if lines else "",
        **extra,
    )


def redirected(argv, env):
    """One run with stdout a file: whole frames, the way difftest sees them."""
    p = subprocess.run(argv, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p.stdout.decode("utf-8", "replace")


def winsize(fd, cols, lines):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", lines, cols, 0, 0))


def on_pty(argv, env, cols, lines, resize=None, settle=0.4, limit=20.0):
    """One run with stdout a pty: everything it painted, as one string.

    With `resize` the clock is left running, resized to it, and left running
    again -- which is the only way to reach the path that throws the last
    frame away, since a window cannot change shape between two frames that are
    drawn back to back. Returns what was painted and how much of it had been
    painted when the window moved, so the model can wipe its screen at the
    same point in the stream.
    """
    master, slave = pty.openpty()
    winsize(slave, cols, lines)
    proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdin=slave, stdout=slave,
                            stderr=subprocess.DEVNULL, close_fds=True)
    os.close(slave)
    out = bytearray()
    cut = 0
    started = time.monotonic()

    def drain(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            r, _, _ = select.select([master], [], [], max(0.0, end - time.monotonic()))
            if not r:
                continue
            try:
                chunk = os.read(master, 1 << 16)
            except OSError:
                return False
            if not chunk:
                return False
            out.extend(chunk)
        return True

    try:
        if resize is None:
            # Until the pty itself says there is no more -- not until the
            # child has gone. A short sequence paints and exits in one breath,
            # and what it painted is still in the buffer after it has.
            while time.monotonic() - started < limit:
                if not drain(0.2):
                    break
        else:
            drain(settle)
            cut = len(out)
            winsize(master, *resize)
            drain(settle)
            proc.send_signal(signal.SIGTERM)
            drain(0.2)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        os.close(master)
    # The split is counted in characters, not bytes: everything downstream
    # works in the decoded text.
    return out.decode("utf-8", "replace"), len(out[:cut].decode("utf-8", "replace"))


def differing(want, got):
    """Where two screens part company, for a failure to say something."""
    if want == got:
        return ""
    row = lambda s, i: (s[i : i + 1] or [""])[0]
    bad = [i for i in range(max(len(want), len(got))) if row(want, i) != row(got, i)]
    return (f"  {len(want)} rows wanted, {len(got)} painted; "
            f"differing: {bad[:8]}\n"
            f"  want[{bad[0]}]: {row(want, bad[0])!r}\n"
            f"  got [{bad[0]}]: {row(got, bad[0])!r}")


def main():
    verbose = "-v" in sys.argv[1:]
    built = tempfile.TemporaryDirectory(prefix="clock-painttest.")
    binary = str(Path(built.name) / "clock")
    subprocess.run(["go", "build", "-o", binary, "."], cwd=ROOT, check=True)
    impls = (("go", [binary]), ("py", [sys.executable, "pyclock.py"]))
    passed = failed = 0

    def report(ok, label, detail=""):
        nonlocal passed, failed
        if ok:
            passed += 1
            if verbose:
                print(f"ok   {label}")
        else:
            failed += 1
            print(f"FAIL {label}")
            if detail:
                print(detail)

    # The screen a sequence paints, against the screen painting it whole would
    # have left. Sizes and zone counts because what changes between two frames
    # does not scale with either, and both counts of frames because the first
    # one is the whole frame and every one after it is the shortcut.
    cases = []
    for cols, lines in ((200, 60), (80, 24), (60, 20)):
        for zones in ("UTC", "ET,PT,UTC,JP", "ET,PT,UTC,JP,GB,NZ,IST,AET"):
            for flags in (["--no-color"], ["--color"]):  # never "auto": see PLAIN
                for n in (1, 2, 3, 7, 40):
                    cases.append((cols, lines, zones, flags, n))

    for cols, lines, zones, flags, n in cases:
        env = env_for(cols, lines, frames=str(n), step="19")
        label = f"[{cols}x{lines} x{n} {flags[0]}] {zones}"
        for name, argv in impls:
            whole = painted_whole(frames_of(redirected(argv + QUIET + flags + [zones], env)))
            stream, _ = on_pty(argv + QUIET + flags + [zones], env, cols, lines)
            try:
                want = Screen().feed(whole).shown()
                got = Screen().feed(stream).shown()
            except AssertionError as e:
                report(False, f"{name} screen {label}", f"  unreadable paint: {e}")
                continue
            report(want == got, f"{name} screen {label}", differing(want, got))

    # A sequence that does not move: CLOCK_STEP=0 draws the same instant every
    # time, so every frame after the first is the frame already on screen and
    # the clock has nothing to say. This is the one case the comparison above
    # cannot fail -- repainting identical frames lands on an identical screen
    # -- and the whole point of the shortcut.
    for cols, lines, zones in ((200, 60, "UTC"), (80, 24, "ET,PT,UTC,JP")):
        env = env_for(cols, lines, frames="25", step="0")
        for name, argv in impls:
            stream, _ = on_pty(argv + QUIET + [zones], env, cols, lines)
            body = stream.split(ENTER_ALT, 1)[-1].split(LEAVE_ALT, 1)[0]
            body = body.replace(HIDE_CURSOR, "").replace(SHOW_CURSOR, "")
            first = Screen().feed(ENTER_ALT + body).shown()
            # One frame's worth of rows, and nothing after it: the tail past
            # the first CLEAR_BELOW is what the other twenty-four frames said.
            after = body.split(CLEAR_BELOW, 1)[1] if CLEAR_BELOW in body else body
            report(after == "", f"{name} silence [{cols}x{lines}] {zones}",
                   f"  {len(after)} bytes painted for 24 unchanged frames")
            report(bool(first), f"{name} silence drew something [{cols}x{lines}] {zones}")

    # A resize throws the screen away: the terminal has reflowed or cleared
    # whatever it was holding, so the rows the clock remembers painting are no
    # longer a promise about what is up there. Pinned and left running, so the
    # only thing that changes between two frames is the window.
    # The last two are the ones with teeth: a window one line taller or
    # shorter draws a frame all but identical to the one before it, so a clock
    # that kept its last frame has almost nothing to say -- and leaves a wiped
    # screen wiped.
    for before, after, zones in (((200, 60), (80, 24), "ET,PT,UTC,JP"),
                                 ((80, 24), (200, 60), "ET,PT,UTC,JP"),
                                 ((120, 40), (120, 20), "UTC"),
                                 ((120, 40), (120, 41), "UTC"),
                                 ((120, 41), (120, 40), "ET,PT,UTC,JP")):
        for name, argv in impls:
            stream, cut = on_pty(argv + PLAIN + [zones], env_for(None, None),
                                 *before, resize=after)
            want = Screen().feed(painted_whole(
                frames_of(redirected(argv + PLAIN + [zones], env_for(*after))))).shown()
            got = Screen().feed(stream[:cut]).cleared().feed(stream[cut:]).shown()
            report(want == got, f"{name} resize {before} -> {after} {zones}",
                   differing(want, got))

    built.cleanup()
    print(f"\npainttest: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
