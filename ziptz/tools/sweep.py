#!/usr/bin/env python3
"""Dumps what this library answers for every ZIP code there is.

All 1,000 three-digit prefixes and all 100,000 five-digit codes, one per line.
Its twin tools/sweep.go prints the same thing from the Go side, and the two
files are compared -- which is the only way to know the two lookups agree on
inputs nobody thought to write a case for.

    python3 tools/sweep.py > /tmp/py.sweep
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ziptz  # noqa: E402  (after the path, on purpose)


def say(token):
    """One answer, error text included: what it refuses and why is as much a
    part of the port as what it resolves."""
    try:
        return f"{token}\t{ziptz.zone(token)}\t{ziptz.generic(token)}\n"
    except ziptz.ZipError as exc:
        return f"{token}\t!{exc}\n"


def main():
    out = sys.stdout
    out.writelines(say(f"{n:03d}") for n in range(1000))
    out.writelines(say(f"{n:05d}") for n in range(100000))


if __name__ == "__main__":
    main()
