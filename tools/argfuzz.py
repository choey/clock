#!/usr/bin/env python3
"""Throw badly spelt arguments at both clocks and compare what comes back.

tools/difftest.sh runs a list someone wrote down. This generates the list
instead, by mutating good invocations into bad ones -- a digit swapped for the
Arabic-Indic digit that means the same thing, a space in front of a number, a
zone name in a case nobody types, a value handed to the flag next door.

That is where the two implementations part company, because it is where the
two standard libraries do. Python's float() reads " 1" and Go's ParseFloat
does not; Go's reads "0x1p2" and Python's does not. Python's str.lower() gives
U+0130 an i and a combining dot where Go's gives it a plain i. Every one of
those was a clock drawn by one implementation and a complaint printed by the
other, and every one was found this way before it was written down as a case.

Both are pinned with CLOCK_FREEZE and redirected, so each run draws one frame
and exits; stdout, stderr and status all have to match. A case that fails
prints as an environment and an argument list, ready to paste.

    tools/argfuzz.py [-v] [--cases N] [--seed N]
"""

import os
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FROZEN = "2026-07-15T09:53:07.123456Z"
CASES = 500  # about twenty seconds, against difftest's seventy
SEED = 20260819  # fixed, so a failure is reproducible and CI does not flap
LIMIT = 20  # seconds; a pinned, redirected clock draws one frame and exits

# Argument lists worth mutating: each is ordinary, and none of them is what is
# being tested. What is being tested is what a mutation of one turns into.
BASES = (
    ["UTC"],
    ["ET,PT,UTC"],
    ["Berlin,Jakarta"],
    ["94110"],
    ["JP,GB"],
    ["local"],
    ["-n", "2", "ET,PT,UTC"],
    ["--per-row", "3", "ET,PT,UTC,JP"],
    ["--scale", "1.5", "UTC"],
    ["--scale", "auto", "ET,PT"],
    ["--cell-ratio", "2.6", "UTC"],
    ["--hpad", "10%", "ET,PT"],
    ["--vpad", "5%", "ET,PT"],
    ["--halign", "left", "--valign", "top", "UTC"],
    ["--color=always", "UTC"],
    ["--day=auto", "ET,JP"],
    ["-q", "UTC"],
    ["--help"],
    ["--version"],
)

# Strings that mean a number, or nearly, in one language and not the other --
# plus a few that mean nothing anywhere. The Unicode digits are the point: both
# str.isdigit() and float() take them, and nothing in Go does.
NUMBERISH = (
    "1", "2", "0", "-1", "1.5", ".5", "5.", "1e1", "1_0", "0x1", "0x1p2",
    "inf", "nan", "auto", "", " 1", "1 ", "\t1", "1\n", "١", "１",
    "١.٥", "٠١", "1,5", "1%", "10%", "100%", "101%", "%",
    "99999999999999999999", "+1", "١٢٣",
)

# Zone tokens: real ones, real ones spelt oddly, and the case foldings the two
# languages disagree about.
ZONEISH = (
    "UTC", "ET", "et", "Berlin", "BERLIN", "berlin", "İstanbul", "Istanbul",
    "local", "LOCAL", "94110", "941", "9411o", "JP", "jp", "ZZ", "", ",", "UTC,",
    ",UTC", "UTC,,PT", " UTC ", "Europe/Berlin", "Europe/", "/etc/passwd",
    "../Europe/Berlin", "ＵＴＣ", "Etc/GMT+5", "PST", "PDT",
)

FLAGS = ("-n", "--per-row", "--scale", "--cell-ratio", "--hpad", "--vpad",
         "--halign", "--valign", "--color", "--day", "-q", "--quiet",
         "--no-color", "--no-day", "-h", "--help", "--version", "--")

# Environment worth varying. TZ and CLOCK_CELL_RATIO are read by the clock
# itself; COLUMNS and LINES stand in for the window, which is what makes a case
# comparable at all.
TZISH = ("UTC", "Asia/Tokyo", ":Asia/Tokyo", "", "Bogus/Zone", "PST8PDT,M3.2.0,M11.1.0",
         "<+07>-7", "GMT+5", "/usr/share/zoneinfo/Asia/Tokyo", "EST5EDT")
FREEZEISH = (FROZEN, "2026-01-15T09:53:07.123456Z", "2026-11-01T05:59:59.900000Z",
             "0000-01-01T00:00:00.000000Z", "0001-01-01T00:00:00.000000Z",
             "9999-12-31T23:59:59.999999Z", "1969-12-31T23:59:59.999999Z",
             "2026-7-15T09:53:07.123456Z", "nonsense")


def mutate(rng, argv):
    """One argument list, bent one way."""
    argv = list(argv)
    which = rng.randrange(7)
    if which == 0 and argv:  # a value swapped for a badly spelt number
        argv[rng.randrange(len(argv))] = rng.choice(NUMBERISH)
    elif which == 1 and argv:  # a zone token swapped for another
        argv[rng.randrange(len(argv))] = rng.choice(ZONEISH)
    elif which == 2:  # a flag appended, with or without its value
        argv.append(rng.choice(FLAGS))
        if rng.random() < 0.5:
            argv.append(rng.choice(NUMBERISH))
    elif which == 3 and argv:  # a flag glued to its value with =
        argv = [a + "=" + rng.choice(NUMBERISH) if a.startswith("--") else a for a in argv]
    elif which == 4 and argv:  # something dropped
        del argv[rng.randrange(len(argv))]
    elif which == 5 and len(argv) > 1:  # two arguments swapped
        i, j = rng.randrange(len(argv)), rng.randrange(len(argv))
        argv[i], argv[j] = argv[j], argv[i]
    elif argv:  # a case change, which is its own kind of trap
        i = rng.randrange(len(argv))
        argv[i] = argv[i].upper() if rng.random() < 0.5 else argv[i].lower()
    return argv


def case(rng):
    """One (environment, argv) pair to run through both implementations."""
    argv = rng.choice(BASES)
    for _ in range(rng.randint(1, 3)):
        argv = mutate(rng, argv)
    env = {
        "CLOCK_FREEZE": rng.choice(FREEZEISH),
        "COLUMNS": str(rng.choice((40, 80, 120, 200))),
        "LINES": str(rng.choice((10, 24, 40, 60))),
    }
    if rng.random() < 0.3:
        env["TZ"] = rng.choice(TZISH)
    if rng.random() < 0.2:
        env["CLOCK_CELL_RATIO"] = rng.choice(NUMBERISH)
    if rng.random() < 0.1:
        env["NO_COLOR"] = rng.choice(("", "1"))
    return env, argv


def run(argv, env):
    base = {k: v for k, v in os.environ.items() if k not in ("TZ", "COLUMNS", "LINES",
                                                             "CLOCK_CELL_RATIO", "NO_COLOR",
                                                             "CLOCK_FREEZE", "CLOCK_FRAMES",
                                                             "CLOCK_STEP")}
    try:
        done = subprocess.run(
            argv, env=dict(base, **env), cwd=ROOT, timeout=LIMIT,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired:
        return "hung", b"", b""
    return done.returncode, done.stdout, done.stderr


def show(env, argv):
    settings = " ".join(f"{k}={v!r}" for k, v in sorted(env.items()))
    return f"{settings} clock {' '.join(repr(a) for a in argv)}"


def main():
    args = sys.argv[1:]
    verbose = "-v" in args
    count = int(args[args.index("--cases") + 1]) if "--cases" in args else CASES
    seed = int(args[args.index("--seed") + 1]) if "--seed" in args else SEED

    subprocess.run(["go", "build", "-o", "clock-argfuzz", "."], cwd=ROOT, check=True)
    rng = random.Random(seed)
    passed = failed = 0
    try:
        for _ in range(count):
            env, argv = case(rng)
            go = run(["./clock-argfuzz"] + argv, env)
            py = run([sys.executable, "clock.py"] + argv, env)
            if go == py:
                passed += 1
                if verbose:
                    print(f"ok   {show(env, argv)}")
                continue
            failed += 1
            print(f"FAIL {show(env, argv)}")
            print(f"     go: status {go[0]}, {len(go[1])} bytes out, stderr {go[2][:160]!r}")
            print(f"     py: status {py[0]}, {len(py[1])} bytes out, stderr {py[2][:160]!r}")
            if go[1] != py[1] and go[1] and py[1]:
                for i, (a, b) in enumerate(zip(go[1], py[1])):
                    if a != b:
                        print(f"     first byte that differs: {i}")
                        break
    finally:
        (ROOT / "clock-argfuzz").unlink(missing_ok=True)

    print(f"\n{passed} agreed, {failed} differed (seed {seed}, {count} cases)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
