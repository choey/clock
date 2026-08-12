#!/usr/bin/env python3
"""Regenerate the US ZIP-prefix time zone table in clock.go and clock.py.

Development tool; the clocks themselves need nothing but the standard library.
It writes both files in one pass, which is what keeps the two embedded literals
from drifting apart.

    pip install timezonefinder
    tools/genzips.py                    # downloads to tools/cache/, once
    tools/genzips.py path/to/gaz.txt    # or reads a local copy instead

The download is cached in tools/cache/ (gitignored) and reused on every later
run, so regenerating costs nothing but the timezonefinder pass.

Data: US Census ZCTA Gazetteer centroids (a US Government work, public domain)
resolved through timezonefinder, whose boundaries come from
timezone-boundary-builder (ODbL). The output is roughly forty range boundaries
-- an aggregate, not a substantial extract -- but both sources are credited in
the generated comment and in the README.

Accuracy: one zone per 3-digit prefix, decided by majority of the ZCTAs under
it, plus an exception list naming every individual ZIP that majority gets
wrong. A 5-digit ZIP is therefore always right; a 3-digit prefix is right only
for the side that won. Every straddling prefix is printed at the end.
"""

import io
import re
import sys
import urllib.request
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

GAZETTEER_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2024_Gazetteer/2024_Gaz_zcta_national.zip"
)

# Every zone timezonefinder can return for a US ZIP centroid, folded onto the
# letters clock.go and clock.py know. Fine-grained zones collapse onto the
# canonical one they have agreed with since 1970 -- America/Detroit has kept
# Eastern time throughout, so a Detroit ZIP is an "E" -- while zones that
# genuinely differ stay apart: Phoenix skips daylight saving, and Adak runs an
# hour behind Anchorage.
CANONICAL = {
    "A": (
        "America/Anchorage",
        "America/Juneau",
        "America/Metlakatla",
        "America/Nome",
        "America/Sitka",
        "America/Yakutat",
    ),
    "C": (
        "America/Chicago",
        "America/Indiana/Knox",
        "America/Indiana/Tell_City",
        "America/Menominee",
        "America/North_Dakota/Beulah",
        "America/North_Dakota/Center",
        "America/North_Dakota/New_Salem",
    ),
    "D": ("America/Adak",),
    "E": (
        "America/Detroit",
        "America/Indiana/Indianapolis",
        "America/Indiana/Marengo",
        "America/Indiana/Petersburg",
        "America/Indiana/Vevay",
        "America/Indiana/Vincennes",
        "America/Indiana/Winamac",
        "America/Kentucky/Louisville",
        "America/Kentucky/Monticello",
        "America/New_York",
    ),
    "G": ("Pacific/Guam", "Pacific/Saipan"),
    "H": ("Pacific/Honolulu",),
    "M": ("America/Boise", "America/Denver"),
    "P": ("America/Los_Angeles",),
    "R": ("America/Puerto_Rico", "America/St_Thomas"),
    "S": ("Pacific/Pago_Pago",),
    "Z": ("America/Phoenix",),
}
LETTER = {zone: letter for letter, zones in CANONICAL.items() for zone in zones}

UNASSIGNED = "-"


CACHE = Path(__file__).resolve().parent / "cache"


def gazetteer(source):
    """Yield (zcta, lat, lon) from the Census gazetteer.

    The download lands in tools/cache/ and is reused from then on: it is a
    static yearly release, and regenerating should not re-fetch a megabyte from
    census.gov every time. The directory is gitignored -- the archive is
    upstream data, not something to vendor into the repo.
    """
    if source:
        text = Path(source).read_text(encoding="utf-8", errors="replace")
    else:
        cached = CACHE / GAZETTEER_URL.rsplit("/", 1)[-1]
        if not cached.exists():
            sys.stderr.write(f"downloading {GAZETTEER_URL}\n")
            with urllib.request.urlopen(GAZETTEER_URL) as response:
                blob = response.read()
            CACHE.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(blob)
        else:
            sys.stderr.write(f"reusing {cached}\n")
        with zipfile.ZipFile(cached) as archive:
            name = next(n for n in archive.namelist() if n.endswith(".txt"))
            text = archive.read(name).decode("utf-8", errors="replace")

    for line in text.splitlines()[1:]:
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        try:
            yield fields[0].strip(), float(fields[5]), float(fields[6])
        except ValueError:
            continue


def vote(source):
    """Tally zone letters per 3-digit prefix, and per-prefix vote breakdowns.

    Also returns every ZCTA's own letter, which is what the exception pass
    diffs against the prefix winners.
    """
    try:
        from timezonefinder import TimezoneFinder
    except ImportError:
        sys.exit("genzips: needs timezonefinder (pip install timezonefinder)")

    finder = TimezoneFinder()
    votes = defaultdict(Counter)
    lowest = {}
    letters = {}
    for zcta, lat, lon in gazetteer(source):
        zone = finder.timezone_at(lat=lat, lng=lon)
        if zone is None:
            continue  # centroid fell offshore
        if zone not in LETTER:
            sys.exit(
                f"genzips: {zcta} resolved to {zone}, which has no letter in "
                "CANONICAL. Add it there (and to zipZones/ZIP_ZONES in both "
                "clocks) rather than letting it be silently mis-assigned."
            )
        prefix = zcta[:3]
        votes[prefix][LETTER[zone]] += 1
        if prefix not in lowest or zcta < lowest[prefix][0]:
            lowest[prefix] = (zcta, LETTER[zone])
        letters[zcta] = LETTER[zone]
    return votes, lowest, letters


def winners_by_prefix(votes, lowest):
    """The letter each 3-digit prefix rounds to."""
    winners = {}
    for prefix, tally in votes.items():
        top = tally.most_common()
        if len(top) > 1 and top[0][1] == top[1][1]:
            winners[prefix] = lowest[prefix][1]  # tie: the lowest ZIP decides
        else:
            winners[prefix] = top[0][0]
    return winners


def encode(winners):
    """Fixed-width run-length encoding: "NNNc" per run start, sorted."""
    runs = []
    previous = None
    for n in range(1000):
        letter = winners.get(f"{n:03d}", UNASSIGNED)
        if letter != previous:
            runs.append(f"{n:03d}{letter}")
            previous = letter
    return "".join(runs)


def encode_exceptions(winners, letters):
    """Fixed-width "NNNNNc" per ZIP the prefix gets wrong, sorted.

    Only the losers of a straddling prefix appear, so the table stays a few
    hundred records rather than a full 5-digit map of every ZIP in the country.
    Sorted so both clocks can binary-search it as a flat string.
    """
    wrong = [z for z, letter in letters.items() if letter != winners[z[:3]]]
    return "".join(f"{z}{letters[z]}" for z in sorted(wrong))


def splice(path, pattern, replacement):
    """Rewrite the one generated line in a source file."""
    text = path.read_text(encoding="utf-8")
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        sys.exit(f"genzips: found {count} generated lines in {path}, expected 1")
    path.write_text(new, encoding="utf-8")


def main():
    source = sys.argv[1] if len(sys.argv) > 1 else None
    votes, lowest, letters = vote(source)
    winners = winners_by_prefix(votes, lowest)
    runs = encode(winners)
    exceptions = encode_exceptions(winners, letters)

    root = Path(__file__).resolve().parent.parent
    splice(
        root / "clock.go",
        r'^const zipRuns = ".*" // zip-runs: .*$',
        f'const zipRuns = "{runs}" // zip-runs: generated by tools/genzips.py',
    )
    splice(
        root / "clock.py",
        r'^ZIP_RUNS = ".*"  # zip-runs: .*$',
        f'ZIP_RUNS = "{runs}"  # zip-runs: generated by tools/genzips.py',
    )
    splice(
        root / "clock.go",
        r'^const zipExceptions = ".*" // zip-exceptions: .*$',
        f'const zipExceptions = "{exceptions}" '
        "// zip-exceptions: generated by tools/genzips.py",
    )
    splice(
        root / "clock.py",
        r'^ZIP_EXCEPTIONS = ".*"  # zip-exceptions: .*$',
        f'ZIP_EXCEPTIONS = "{exceptions}"  '
        "# zip-exceptions: generated by tools/genzips.py",
    )

    prefixes = len(votes)
    print(f"{prefixes} prefixes -> {len(runs) // 4} runs, {len(runs)} characters")
    print(
        f"{len(exceptions) // 6} ZIPs the prefixes get wrong -> "
        f"{len(exceptions)} characters"
    )
    print("wrote clock.go and clock.py")

    split = sorted(p for p, tally in votes.items() if len(tally) > 1)
    if not split:
        return
    print(f"\n{len(split)} prefixes straddle a zone boundary and were rounded:")
    for prefix in split:
        tally = votes[prefix]
        won = max(tally, key=lambda k: (tally[k], -ord(k)))
        detail = ", ".join(f"{k}={v}" for k, v in tally.most_common())
        print(f"  {prefix} -> {won}   ({detail})")


if __name__ == "__main__":
    main()
