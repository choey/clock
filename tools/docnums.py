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
sys.path.insert(0, str(ROOT / "ziptz"))

import ziptz  # noqa: E402  (after the path, on purpose)


def tables():
    """The figures, straight off the tables the libraries ship."""
    runs = [ziptz.RUNS[i + 3] for i in range(0, len(ziptz.RUNS), 4)]
    groups, exceptions, i = 0, 0, 0
    while i < len(ziptz.EXCEPTIONS):
        count = int(ziptz.EXCEPTIONS[i + 4 : i + 6])
        groups, exceptions, i = groups + 1, exceptions + count, i + 6 + count * 2

    canonical = re.search(
        r"CANONICAL = \{(.*?)\n\}",
        (ROOT / "ziptz" / "tools" / "genzips.py").read_text(encoding="utf-8"),
        re.S,
    )
    return {
        "runs": len(runs),
        "runs_named": sum(1 for letter in runs if letter != "-"),
        "exceptions": exceptions,
        "exception_prefixes": groups,
        "letters": len(ziptz.ZONES),
        "generics": len(ziptz.GENERIC),
        "folded": len(re.findall(r'"[A-Za-z_]+/[A-Za-z_/]+"', canonical.group(1))),
    }


# Each claim: the file, a pattern with one group holding the number, and which
# figure that number has to equal. The pattern is written to match the sentence
# it lives in, so a rewording fails loudly here rather than drifting silently.
CLAIMS = (
    ("README.md", r"(\d+) ZIP codes, across (\d+) prefixes", ("exceptions", "exception_prefixes")),
    ("README.md", r"of 33,791 ZIP codes, ([\d,]+) \(0\.69%\)", ("exceptions",)),
    ("ARCHITECTURE.md", r"is the (\d+) ZIPs the prefix table gets wrong", ("exceptions",)),
    ("ziptz/README.md", r"wrong for the (\d+) that sit on the losing side", ("exceptions",)),
    ("ziptz/README.md", r"(\d+) range records, (\d+) of which name a zone, and (\d+) exceptions",
     ("runs", "runs_named", "exceptions")),
    ("ziptz/README.md", r"`Generic` is a table here, ([a-z]+) entries", ("letters",)),
    ("ziptz/ziptz.py", r"wrong for the (\d+) that sit on the losing side", ("exceptions",)),
    ("ziptz/ziptz.go", r"// the 33,791 ZIP codes and wrong for the (\d+) that sit", ("exceptions",)),
    ("ziptz/tools/genzips.py", r"The output is (\d+) range records and (\d+)\n-- ?exceptions|"
                               r"The output is (\d+) range records and (\d+)\nexceptions",
     ("runs", "exceptions")),
    ("ziptz/README.md", r"`CANONICAL` collapses ~(\d+) zones onto (\d+) letters",
     ("folded", "letters")),
)

WORDS = {"eleven": 11, "ten": 10, "twelve": 12, "thirty": 30, "forty": 40}


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

    # The one figure no table holds: how many ZIPs there are at all. It comes
    # from the Census file, so this checks the documents agree with each other
    # and with their own arithmetic instead.
    total, right = 33791, 33791 - figures["exceptions"]
    for path in ("ziptz/README.md", "ziptz/ziptz.py", "ziptz/ziptz.go"):
        text = (ROOT / path).read_text(encoding="utf-8")
        if f"{right:,}" in text and f"{total:,}" in text:
            passed += 1
            if verbose:
                print(f"ok   {path}: {right:,} of {total:,} add up")
        else:
            print(f"FAIL {path}: does not say {right:,} of {total:,}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
