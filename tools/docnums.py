#!/usr/bin/env python3
"""Hold the documentation's numbers to the tables they describe.

The prose quotes figures that come out of the data -- how many ZIPs the prefix
table gets wrong, across how many prefixes, how many records the run table
holds. Regenerate the tables and those sentences are quietly false, and nothing
notices: they read fine, and no test reads prose.

This recomputes each one from the shipped tables and checks the file says it.
It found `86504` where the table says `86502`, and "roughly forty range
boundaries" where there are 157.

    tools/docnums.py [-v]
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import ziptz  # the released module, not a copy in this tree


def tables():
    """The figures, straight off the tables the libraries ship."""
    runs = [ziptz.RUNS[i + 3] for i in range(0, len(ziptz.RUNS), 4)]
    groups, exceptions, i = 0, 0, 0
    while i < len(ziptz.EXCEPTIONS):
        count = int(ziptz.EXCEPTIONS[i + 4 : i + 6])
        groups, exceptions, i = groups + 1, exceptions + count, i + 6 + count * 2

    # The one figure that comes from a harness rather than from the data: how
    # many frames tools/golden keeps. Four sentences across two documents quote
    # it, and adding a golden is exactly the moment nobody rereads them.
    difftest = (ROOT / "tools" / "difftest.sh").read_text(encoding="utf-8")

    return {
        "goldens": len(re.findall(r"(?m)^golden ", difftest)),
        "runs": len(runs),
        "runs_named": sum(1 for letter in runs if letter != "-"),
        "exceptions": exceptions,
        "exception_prefixes": groups,
        "letters": len(ziptz.ZONES),
        "generics": len(ziptz.GENERIC),
    }


# Each claim: the file, a pattern with one group holding the number, and which
# figure that number has to equal. The pattern is written to match the sentence
# it lives in, so a rewording fails loudly here rather than drifting silently.
CLAIMS = (
    ("README.md", r"So ([a-z]+)\nframes are kept as bytes", ("goldens",)),
    ("README.md", r"of the (\d+) goldens", ("goldens",)),
    ("ARCHITECTURE.md", r"is the other half, ([a-z]+) renderings", ("goldens",)),
    ("ARCHITECTURE.md", r"what the clock actually draws, at ([a-z]+) sizes", ("goldens",)),
    ("README.md", r"(\d+) ZIP codes, across (\d+) prefixes", ("exceptions", "exception_prefixes")),
    ("README.md", r"of 33,791 ZIP codes, ([\d,]+) \(0\.69%\)", ("exceptions",)),
    ("ARCHITECTURE.md", r"is the (\d+) ZIPs the prefix table gets wrong", ("exceptions",)),
    ("README.md", r"(\d+) ZIPs across (\d+) prefixes, (\d+) range records",
     ("exceptions", "exception_prefixes", "runs")),
    ("README.md", r"the (\d+) letters those fold onto", ("letters",)),
)

# ziptz's own documents are checked by ziptz, which is where they live now.
# What stays here is every figure a *clock* document quotes, recomputed from
# the library the clock actually depends on -- so a ziptz release that moved a
# ZIP would fail this repository's prose too, which is the point.

WORDS = {"eleven": 11, "ten": 10, "twelve": 12, "sixteen": 16, "thirty": 30, "forty": 40}


def documented(verbose):
    """Every flag and variable the program knows, mentioned in the README.

    A flag added to the usage text and nowhere else is a flag nobody finds:
    --help is what you read when you already know it exists.
    """
    clock = (ROOT / "clock.py").read_text(encoding="utf-8")
    usage = re.search(r'USAGE = """(.*?)"""', clock, re.S).group(1)
    names = sorted(set(re.findall(r"--[a-z][a-z-]+", usage)))
    names += sorted(set(re.findall(r"CLOCK_[A-Z_]+", clock)))

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    passed = failed = 0
    for name in names:
        if name in readme:
            passed += 1
            if verbose:
                print(f"ok   README.md documents {name}")
        else:
            print(f"FAIL README.md never mentions {name}, which the program accepts")
            failed += 1
    return passed, failed


def versions(verbose):
    """The one number that is written three times and shown to the user.

    clock.py, clock.go and pyproject.toml each declare it, and `clock
    --version` prints it from two of them -- so a bump that misses one makes
    the two implementations disagree about which release they are, which is
    the single claim difftest cannot catch: it compares the two clocks to each
    other, and a wrong version printed identically by both is still wrong.
    """
    found = {
        "clock.py": r'^VERSION = "([^"]+)"',
        "clock.go": r'^const version = "([^"]+)"',
        "pyproject.toml": r'^version = "([^"]+)"',
    }
    seen = {}
    for path, pattern in found.items():
        text = (ROOT / path).read_text(encoding="utf-8")
        match = re.search(pattern, text, re.M)
        if not match:
            print(f"FAIL {path}: no version declaration matching /{pattern}/")
            return 0, 1
        seen[path] = match.group(1)

    if len(set(seen.values())) != 1:
        print("FAIL the version is declared three times and they disagree:")
        for path, value in seen.items():
            print(f"     {path:16} {value}")
        return 0, 1
    if verbose:
        print(f"ok   version {next(iter(seen.values()))} in all three declarations")
    return 1, 0


def main():
    verbose = "-v" in sys.argv[1:]
    figures = tables()
    passed = failed = 0

    for path, pattern, keys in CLAIMS:
        text = (ROOT / path).read_text(encoding="utf-8")
        found = re.search(pattern, text)
        if not found:
            print(f"FAIL {path}: no sentence matching /{pattern}/")
            print("     (reworded? then reword the pattern with it)")
            failed += 1
            continue
        numbers = [g for g in found.groups() if g is not None]
        if len(numbers) != len(keys):
            print(f"FAIL {path}: /{pattern}/ caught {numbers}, wanted {len(keys)} numbers")
            failed += 1
            continue
        for got, key in zip(numbers, keys):
            value = WORDS.get(got.lower(), None)
            if value is None:
                value = int(got.replace(",", ""))
            if value != figures[key]:
                print(f"FAIL {path}: says {got} for {key}, the tables say {figures[key]}")
                failed += 1
            else:
                passed += 1
                if verbose:
                    print(f"ok   {path}: {key} = {got}")

    for check in (documented, versions):
        more_passed, more_failed = check(verbose)
        passed, failed = passed + more_passed, failed + more_failed

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
