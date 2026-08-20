#!/usr/bin/env python3
"""Prove the two clocks answer the keyboard alike.

The other half of tools/difftest.sh. Space, h, ? and q are read only from a
terminal in cbreak mode, so nothing redirected ever presses one: difftest
compares the table the key list is built from, not what pressing h does. This
runs each implementation under a pty, gives both the same keys at the same
points, and compares what they paint.

It is also the only harness that can put a file descriptor of a chosen kind
under a clock and then give up waiting: difftest redirects to regular files
and has no timeout, so a clock that mistook one for a terminal would hang it
rather than fail it. See the character-device case below.

A clock repaints every 19ms whether or not anything changed, so how many
repaints land between two keystrokes is timing, not behaviour. The streams are
therefore collapsed to their *distinct* frames before comparing -- frames are
delimited by the cursor-home each full-screen repaint starts with. Pinned with
CLOCK_FREEZE, every repaint draws the same bytes, so the collapsed sequence is
exactly the sequence of states the keys walked through.

    tools/keytest.py [-v]
"""

import contextlib
import fcntl
import os
import pty
import re
import select
import signal
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
HIDE_CURSOR, SHOW_CURSOR = b"\x1b[?25l", b"\x1b[?25h"

# The two keys the terminal driver answers rather than the clock: with ISIG on,
# the line discipline turns these into SIGINT and SIGTSTP for the foreground
# process group, and the byte itself never reaches the clock. They only do
# anything under shell_job below -- a pty no session has claimed has no
# foreground group to signal, and drops them on the floor instead.
CTRL_C, CTRL_Z = b"\x03", b"\x1a"

# Long enough for several repaints to land, short enough to keep the suite
# quick. Nothing depends on how many land -- only that at least one does.
SETTLE = 0.25

# What both ports exit with when the reader goes away; PIPE_STATUS in clock.py
# and pipeStatus in clock.go say the same thing, and this holds them to it.
PIPE_STATUS = 141


def pump(master, out, seconds):
    """Read whatever the clock painted for `seconds`, onto the end of `out`.

    False if the far end has closed, i.e. the clock has quit -- unless this
    harness is holding the slave open itself, in which case it never does and
    the caller is timing out on purpose.
    """
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


def open_pty(echo=False):
    r"""A pty sized and quietened the way most cases here want one.

    Echo off before the child starts. It turns echo off itself, in cbreak, but
    not until it has started -- and a key written into that window comes
    straight back out as text in the middle of the frames. That is a fault in
    the harness, not the clock, and it only shows on a slow or loaded machine,
    which is the worst way to find out. The job-control cases ask for it back,
    since a terminal that starts out the way the clock wants it is one where
    taking it and giving it back look the same as never touching it.

    Output post-processing off too, which is not about echo at all: with it on,
    the driver turns each \n into \r\n, and at a write boundary it can emit
    the \r twice for one newline. Whether that happens depends on how a 2.8KB
    frame gets split into write() calls, which differs between a Go string
    printed whole and a Python buffer flushed -- so it would be the terminal's
    chunking under comparison, not the clocks'. With OPOST off both write the
    same 17 newlines and no carriage returns at all.
    """
    master, slave = pty.openpty()
    # The clock reads COLUMNS/LINES first and the ioctl second; set both, so it
    # cannot matter which one it happens to believe.
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", LINES, COLS, 0, 0))
    mode = termios.tcgetattr(slave)
    if not echo:
        mode[3] &= ~(termios.ECHO | termios.ICANON)  # lflag
    mode[1] &= ~termios.OPOST  # oflag
    termios.tcsetattr(slave, termios.TCSANOW, mode)
    return master, slave


def run(argv, keys, freeze=FROZEN, settle=SETTLE, sizes=None):
    """One clock under a pty, fed `keys`, returning everything it painted.

    Each key is written after the frames from the previous one have had time to
    land, so the sequence of states is the sequence of keystrokes.

    `sizes` resizes the window instead, to each in turn. The clock reads
    COLUMNS and LINES ahead of the terminal itself, so those are left unset for
    a resize run -- otherwise it would keep drawing the old size at a window
    that had changed, which is the bug this would be trying to find.
    """
    master, slave = open_pty()
    env = dict(os.environ, COLUMNS=str(COLS), LINES=str(LINES), TERM="xterm-256color")
    if sizes is not None:
        env.pop("COLUMNS", None)
        env.pop("LINES", None)
    if freeze:
        env["CLOCK_FREEZE"] = freeze
    else:
        env.pop("CLOCK_FREEZE", None)

    proc = subprocess.Popen(
        argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=ROOT, close_fds=True
    )
    os.close(slave)

    out = bytearray()

    def drain(seconds):
        return pump(master, out, seconds)

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

# The hint the clock puts up for its first three seconds, unless -q. Every
# other case here passes -q, so without this the hint and the modal path it
# takes would go untested on a terminal -- and it cannot be tested anywhere
# else, since a redirected clock never shows it.
FLASH_MARK = b"Press q or Ctrl+C to quit"


def helped(state_list):
    """Which of the painted states had the key list up."""
    return [HELP_MARK in s for s in state_list[1:]]


def readouts(painted):
    """Every digital readout painted, in order, as they appear on the faces."""
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


def compare(name, keys, expect=None, freeze=FROZEN, loose=False, args=("-q", "UTC")):
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
        painted, status = run(argv + list(args), keys, freeze=freeze)
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


def not_a_terminal(name):
    """A character device that is not a terminal is still redirected output.

    The clock decides full-screen or one-frame-and-exit from stdout, and
    /dev/null is a character device -- so the obvious test, "is this a
    character device", says terminal and is wrong. Under it the Go clock took
    the alternate screen and ran forever where clock.py drew its frame and
    exited, with every difftest case redirecting to a regular file and seeing
    nothing.

    Pinned, so both have exactly one frame to draw: whichever takes longer
    than that has decided it is talking to a terminal.
    """
    env = dict(os.environ, CLOCK_FREEZE=FROZEN, COLUMNS=str(COLS), LINES=str(LINES))
    for impl, argv in IMPLS:
        with open(os.devnull, "wb") as sink:
            proc = subprocess.Popen(
                argv + ["-q", "UTC"], stdin=subprocess.DEVNULL, stdout=sink,
                stderr=subprocess.PIPE, env=env, cwd=ROOT,
            )
            try:
                _, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                report(False, name, f"{impl} never exited: it took /dev/null for a terminal")
                return
        if proc.returncode != 0:
            report(False, name, f"{impl}: exit status {proc.returncode}, {err!r}")
            return
    report(True, name)


def reader_leaves(name):
    """`clock | head`: the reader goes away and the clock is still writing.

    Both ports have to end this the same way, and the way is: no traceback, no
    complaint, exit 141 -- and the terminal handed back. stdin here is a
    terminal even though stdout is a pipe, which is the ordinary shape of a
    pipeline typed at a prompt, and it means the clock has put that terminal
    into cbreak and owes it back. Nothing in difftest can reach this: it needs
    a reader that leaves, a terminal that is not stdout, and the willingness
    to stop waiting for a clock that never notices.
    """
    for impl, argv in IMPLS:
        master, slave = pty.openpty()
        read_fd, write_fd = os.pipe()
        env = dict(os.environ, COLUMNS=str(COLS), LINES=str(LINES), TERM="xterm-256color")
        env.pop("CLOCK_FREEZE", None)  # live: a pinned clock would be gone already
        proc = subprocess.Popen(
            argv + ["-q", "UTC"], stdin=slave, stdout=write_fd,
            stderr=subprocess.PIPE, env=env, cwd=ROOT,
        )
        os.close(write_fd)
        os.read(read_fd, 1 << 16)  # one frame, then stop listening
        os.close(read_fd)
        try:
            status = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            status = "hung"
        err = proc.stderr.read()
        proc.stderr.close()
        mode = termios.tcgetattr(slave)
        os.close(master)
        os.close(slave)

        if status != PIPE_STATUS:
            report(False, name, f"{impl}: exit status {status}, wanted {PIPE_STATUS}")
            return
        if err:
            report(False, name, f"{impl} said {err[:120]!r} about an ordinary pipeline")
            return
        if not (mode[3] & termios.ECHO) or not (mode[3] & termios.ICANON):
            report(False, name, f"{impl} left the terminal in cbreak")
            return
    report(True, name)


@contextlib.contextmanager
def shell_job(argv, freeze=FROZEN):
    """One clock started the way a shell starts one, and stoppable like one.

    Every other case here starts the clock as a plain child of the harness, on
    a pty no session has claimed. That is enough for keys, and enough for
    signals sent with kill(2), but it is not a job: the pty has no foreground
    process group, so Ctrl+C and Ctrl+Z are swallowed by the line discipline
    before the clock could see either, and the process group the clock lands in
    is orphaned -- no member's parent watches it from elsewhere in the same
    session -- which is a group the kernel refuses to stop at all, on the
    grounds that nothing would be left to resume it.

    So this puts a shim in between, doing the two things a shell does: it takes
    a session of its own with the pty for a controlling terminal, and starts
    the clock in a process group of its own, in the foreground. Staying around
    as the clock's parent is what makes that group stoppable. It reports what
    becomes of the clock -- "stopped", "exit N" -- one line at a time down a
    pipe, since only a parent can wait for it.

    Yields (master, slave, pid, news). The slave stays open here on purpose:
    it is the only way to ask what state the clock left the terminal in.
    """
    master, slave = open_pty(echo=True)
    env = dict(os.environ, COLUMNS=str(COLS), LINES=str(LINES), TERM="xterm-256color")
    if freeze:
        env["CLOCK_FREEZE"] = freeze
    else:
        env.pop("CLOCK_FREEZE", None)

    heard, tell = os.pipe()
    shim = os.fork()
    if shim == 0:  # the shim; nothing below here returns
        try:
            os.close(heard)
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            clock = os.fork()
            if clock == 0:
                os.close(tell)
                os.setpgid(0, 0)
                for fd in (0, 1, 2):
                    os.dup2(slave, fd)
                if slave > 2:
                    os.close(slave)
                os.chdir(ROOT)
                try:
                    os.execvpe(argv[0], argv, env)
                except OSError:
                    os._exit(127)  # nothing here may come back and be a shim
            # Set on both sides of the fork, since either order can happen and
            # the foreground group has to be set from a pid that exists.
            os.setpgid(clock, clock)
            os.tcsetpgrp(slave, clock)
            report = os.fdopen(tell, "w", buffering=1)
            report.write("pid %d\n" % clock)
            while True:
                # Stops and endings only. A resume is not waited for here: not
                # every kernel reports one, and the case reads it off the
                # screen instead, which is where the clock puts it back.
                _, status = os.waitpid(clock, os.WUNTRACED)
                if os.WIFSTOPPED(status):
                    report.write("stopped\n")
                elif os.WIFSIGNALED(status):
                    report.write("killed %d\n" % os.WTERMSIG(status))
                    break
                else:
                    report.write("exit %d\n" % os.WEXITSTATUS(status))
                    break
            # Then stay alive until the harness has finished looking. On the
            # BSDs the session leader's exit revokes its controlling terminal,
            # and every descriptor onto it goes with it -- including the one
            # the case wants to ask what state the clock left the terminal in.
            while True:
                signal.pause()
        finally:
            os._exit(0)

    os.close(tell)
    news = os.fdopen(heard, "r")
    pid = int(news.readline().split()[1])
    try:
        yield master, slave, pid, news
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGCONT)
            os.kill(pid, signal.SIGKILL)
        news.close()
        os.close(master)
        os.close(slave)
        os.kill(shim, signal.SIGKILL)
        os.waitpid(shim, 0)


def told(news, master, out, seconds=5):
    """The next thing the shim says about the clock, or "nothing" in `seconds`.

    Whatever it says arrives in order, so waiting for one line and getting
    another is the answer to the question: a clock that exited where it should
    have stopped says so here rather than in a timeout.

    Keeps reading the pty while it waits, and has to: a clock repaints every
    19ms, so a harness that stops reading fills the terminal's output buffer in
    a moment and blocks the clock inside a write. Both ports do the work a
    signal asks for in the frame loop rather than in the handler, so a clock
    blocked in a write is a clock that has not noticed the signal yet -- it
    would sit there until the wait gave up, and the case would fail with the
    harness's own hand over its mouth.
    """
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pump(master, out, 0.02)
        if select.select([news], [], [], 0)[0]:
            return news.readline().strip() or "nothing"
    return "nothing"


def cbreak(slave):
    """Whether the terminal is as the clock wants it: echo off, unbuffered."""
    mode = termios.tcgetattr(slave)
    return not mode[3] & termios.ECHO and not mode[3] & termios.ICANON


def started(master, out):
    """Wait for a clock to be up and painting, as run() does."""
    ready = time.monotonic() + 5
    while time.monotonic() < ready:
        if ENTER_ALT in out and HOME in out:
            break
        if not pump(master, out, 0.05):
            break
    pump(master, out, SETTLE)


def screen_events(painted):
    """The alternate-screen and cursor escapes, in the order they were sent.

    What the clock did with the terminal, with the frames in between dropped:
    how many of those landed is timing, and around a signal it is the
    scheduler's timing rather than the clock's.
    """
    return re.findall(rb"\x1b\[\?(?:1049|25)[hl]", painted)


def suspends(name):
    """Ctrl+Z hands the terminal back, and resuming takes it again.

    The clock owns the alternate screen and holds the terminal in cbreak, and a
    stop can last as long as the reader likes -- so it owes both back before it
    goes, and has to take them again on the way in. Nothing else here can test
    it: it needs a job control can stop, and a stopped clock paints nothing, so
    a harness that only knows how to wait for frames would wait forever.
    """
    seen = {}
    for impl, argv in IMPLS:
        with shell_job(argv + ["-q", "UTC"]) as (master, slave, pid, news):
            painted = bytearray()
            started(master, painted)
            if not cbreak(slave):
                report(False, name, f"{impl} never took the terminal")
                return
            up_to = len(painted)

            os.write(master, CTRL_Z)
            said = told(news, master, painted)
            if said != "stopped":
                report(False, name, f"{impl} did not stop on Ctrl+Z: {said}")
                return
            # The stop is reported by a different route than the screen it was
            # painted on, so give what the clock wrote on the way down time to
            # arrive before reading it.
            pump(master, painted, 0.05)
            # Given back, in the order it was taken: the cursor, then the
            # screen, then the terminal modes.
            if not bytes(painted[up_to:]).endswith(SHOW_CURSOR + LEAVE_ALT):
                report(False, name, f"{impl} stopped without giving the screen back")
                return
            if cbreak(slave):
                report(False, name, f"{impl} stopped with the terminal still in cbreak")
                return
            up_to = len(painted)
            pump(master, painted, SETTLE)
            if len(painted) > up_to:
                report(False, name, f"{impl} painted {len(painted) - up_to} bytes while stopped")
                return

            os.kill(pid, signal.SIGCONT)
            end = time.monotonic() + 5
            while time.monotonic() < end and HOME not in bytes(painted[up_to:]):
                pump(master, painted, 0.05)
            pump(master, painted, SETTLE)
            resumed = bytes(painted[up_to:])
            if not resumed.startswith(ENTER_ALT + HIDE_CURSOR):
                report(False, name, f"{impl} resumed without taking the screen back")
                return
            if HOME not in resumed:
                report(False, name, f"{impl} resumed without painting a frame")
                return
            if not cbreak(slave):
                report(False, name, f"{impl} resumed without taking the terminal back")
                return

            os.write(master, b"q")
            said = told(news, master, painted)
            if said != "exit 0":
                report(False, name, f"{impl} did not quit after being resumed: {said}")
                return
            pump(master, painted, 0.05)
            if not bytes(painted).endswith(SHOW_CURSOR + LEAVE_ALT):
                report(False, name, f"{impl} quit without giving the screen back")
                return
            if cbreak(slave):
                report(False, name, f"{impl} quit with the terminal still in cbreak")
                return
            seen[impl] = screen_events(painted)

    if seen["go"] != seen["py"]:
        report(False, name, f"go did {seen['go']} where py did {seen['py']}")
        return
    report(True, name)


def interrupts(name):
    """Ctrl+C really is a signal, and ends the clock as quietly as q does.

    Only a clock with a terminal of its own is sent one -- every other case
    here presses Ctrl+C at a pty with no foreground process group, where the
    line discipline drops the byte and the clock ends on the q the harness
    types afterwards, whatever it does with SIGINT.
    """
    for impl, argv in IMPLS:
        with shell_job(argv + ["-q", "UTC"]) as (master, slave, _, news):
            painted = bytearray()
            started(master, painted)
            os.write(master, CTRL_C)
            said = told(news, master, painted)
            if said != "exit 0":
                report(False, name, f"{impl} did not quit quietly on Ctrl+C: {said}")
                return
            pump(master, painted, 0.05)
            if not bytes(painted).endswith(SHOW_CURSOR + LEAVE_ALT):
                report(False, name, f"{impl} left the screen it took")
                return
            if cbreak(slave):
                report(False, name, f"{impl} left the terminal in cbreak")
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
        compare("space on a pinned clock has nothing to hold", [b" ", b" "], [0, 0])

        print("== the startup hint ==")
        compare("without -q, the hint is up", [], args=("UTC",))
        compare("the key list replaces the hint, and gives it back",
                [b"h", b"h"], args=("UTC",))
        for impl, argv in IMPLS:
            painted, _ = run(argv + ["UTC"], [])
            report(FLASH_MARK in painted, f"{impl} shows the hint without -q")
            painted, _ = run(argv + ["-q", "UTC"], [])
            report(FLASH_MARK not in painted, f"{impl} shows no hint with -q")
        # Job control, which needs a clock that is a job: its own process
        # group, in the foreground of a terminal some session owns. Nothing
        # above is one, so nothing above can press either of these keys.
        print("== signals from the terminal ==")
        interrupts("Ctrl+C ends it, quietly, with the screen given back")
        suspends("Ctrl+Z gives the terminal back, and resuming takes it again")

        print("== stdout that is not a terminal ==")
        not_a_terminal("/dev/null is a character device, not a terminal")
        reader_leaves("the reader goes away mid-frame")

        # The clock re-measures every frame rather than trapping SIGWINCH, so
        # this is the only thing that can drag a window: difftest pins one size
        # per run and never changes it.
        print("== resize ==")
        # 60 columns, not 40: three faces need three cells of at least the
        # readout's twelve, plus gaps, and 40 is not enough for that -- which
        # is a complaint, and complaints are what the two cases below are for.
        compare_resize("shrink, then grow", ["ET,PT,UTC"], [(60, 20), (200, 50)], [0, 0, 0, 0])
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
