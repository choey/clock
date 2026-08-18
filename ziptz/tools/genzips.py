#!/usr/bin/env python3
"""Regenerate the US ZIP-prefix time zone tables in ziptz.go and ziptz.py.

Development tool, and deliberately not a build step: it needs timezonefinder
and a megabyte of Census data, where ziptz itself needs neither -- so the
tables it writes are checked in as source, and every install of either library
ships the exact bytes generated here. It writes both files in one pass, which
is what keeps the two literals from drifting apart.

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
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

GAZETTEER_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2024_Gazetteer/2024_Gaz_zcta_national.zip"
)

# Every zone timezonefinder can return for a US ZIP centroid, folded onto the
# letters ziptz.go and ziptz.py know. Fine-grained zones collapse onto the one
# they agree with *today* -- a Detroit ZIP is an "E" -- while zones that
# genuinely differ stay apart: Phoenix skips daylight saving, and Adak runs an
# hour behind Anchorage.
#
# Today, not always: many of these agreed only recently. Indiana kept no
# daylight saving until 2006, Kentucky/Monticello left Central for Eastern in
# 2000, North Dakota/Beulah left Mountain for Central in 2010. The folding is
# therefore a claim about the present that check_folding() re-tests on every
# run, and it makes these tables wrong for historical dates -- which the clocks
# never show.
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
        zcta = fields[0].strip()
        # Both tables slice ZCTAs at fixed offsets -- three for the prefix, two
        # for the suffix -- so anything but five digits would silently shift
        # every record after it. Drop it rather than encode a corrupt table.
        if len(zcta) != 5 or not zcta.isdigit():
            continue
        try:
            yield zcta, float(fields[5]), float(fields[6])
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
                "CANONICAL. Add it there (and to zones/ZONES in both ziptz "
                "libraries) rather than letting it be silently mis-assigned."
            )
        prefix = zcta[:3]
        votes[prefix][LETTER[zone]] += 1
        if prefix not in lowest or zcta < lowest[prefix][0]:
            lowest[prefix] = (zcta, LETTER[zone])
        letters[zcta] = LETTER[zone]
    return votes, lowest, letters


def zip_zone_targets(root):
    """The letter -> zone map the libraries use, read from ziptz.py.

    Not duplicated here: a third copy is a third thing to drift. genzips writes
    into ziptz.py anyway, so reading the map back out keeps one source.
    """
    body = re.search(
        r"ZONES = \{(.*?)\}",
        root.joinpath("ziptz.py").read_text(encoding="utf-8"),
        re.S,
    )
    if not body:
        sys.exit("genzips: cannot find ZONES in ziptz.py")
    return dict(re.findall(r'"(.)":\s*"([A-Za-z_/]+)"', body.group(1)))


def check_folding(targets):
    """Re-test the claim CANONICAL makes: each zone still agrees with its letter.

    One letter stands for a dozen zones, which holds only while they keep the
    same rules. They have not always: Indiana had no daylight saving until 2006,
    Kentucky/Monticello left Central in 2000, North Dakota/Beulah left Mountain
    in 2010. Should a state break away again, every ZIP folded onto that letter
    would quietly serve the wrong hour -- so fail here, at generation, rather
    than let it reach a clock face.

    Note what this does *not* cover. A change to daylight-saving rules alone --
    a state stopping, or the country abolishing the switch -- needs no
    regeneration at all: the tables store zone names, and the rules come from
    whatever tzdata the machine running the clock has. Only a change to which
    zone a place belongs to, or a split within a letter, needs new tables.
    """
    from zoneinfo import ZoneInfo

    start = datetime.now(dt_timezone.utc)
    probes = [start + timedelta(hours=6 * i) for i in range(4 * 400)]  # ~13 months

    problems = []
    for letter, zones in sorted(CANONICAL.items()):
        if letter not in targets:
            sys.exit(
                f"genzips: letter {letter!r} is in CANONICAL but not in "
                "ZONES; add it to both ziptz libraries"
            )
        target_name = targets[letter]
        target = ZoneInfo(target_name)
        for name in zones:
            if name == target_name:
                continue
            zone = ZoneInfo(name)
            for probe in probes:
                if probe.astimezone(zone).utcoffset() != probe.astimezone(target).utcoffset():
                    problems.append((name, target_name, probe.date()))
                    break

    if problems:
        lines = "\n".join(
            f"  {name} parts from {target} on {when}" for name, target, when in problems
        )
        sys.exit(
            "genzips: these zones no longer track the letter they fold onto, so "
            "folding them would serve the wrong hour:\n" + lines + "\n"
            "Give the divergent one its own letter in CANONICAL, and add that "
            "letter to zones/ZONES in both ziptz libraries."
        )
    folded = sum(len(z) for z in CANONICAL.values())
    print(f"folding checked: {folded} zones track their letter for the next 13 months")


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
    """Group the ZIPs a prefix gets wrong under one header each.

    A record is "PPPcNN" -- prefix, zone letter, then how many two-digit
    suffixes follow -- and then that many suffixes, ascending:

        373C38 01 02 07 ...        (spaces for clarity only)

    Only the losers of a straddling prefix appear, and the prefix is written
    once for all of them rather than repeated on every ZIP, which is where the
    saving over a flat 5-digit table comes from. Grouping on (prefix, letter)
    rather than prefix alone costs nothing today -- every straddling prefix has
    exactly one exception letter -- but keeps the format working if a future
    regeneration turns up a prefix split three ways.

    Groups longer than 99 are chunked into consecutive records, so a two-digit
    count can never overflow however the source data shifts.
    """
    wrong = sorted(z for z, letter in letters.items() if letter != winners[z[:3]])
    groups = {}
    for zcta in wrong:
        groups.setdefault((zcta[:3], letters[zcta]), []).append(zcta[3:])

    out = []
    for (prefix, letter), suffixes in sorted(groups.items()):
        for start in range(0, len(suffixes), 99):
            chunk = suffixes[start : start + 99]
            out.append(f"{prefix}{letter}{len(chunk):02d}" + "".join(chunk))
    return "".join(out), len(wrong)


def splice(path, pattern, replacement):
    """Rewrite the one generated line in a source file."""
    text = path.read_text(encoding="utf-8")
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        sys.exit(f"genzips: found {count} generated lines in {path}, expected 1")
    path.write_text(new, encoding="utf-8")


def main():
    source = sys.argv[1] if len(sys.argv) > 1 else None
    root = Path(__file__).resolve().parent.parent  # the ziptz/ directory
    check_folding(zip_zone_targets(root))
    votes, lowest, letters = vote(source)
    winners = winners_by_prefix(votes, lowest)
    runs = encode(winners)
    exceptions, wrong_count = encode_exceptions(winners, letters)

    splice(
        root / "ziptz.go",
        r'^const runs = ".*" // zip-runs: .*$',
        f'const runs = "{runs}" // zip-runs: generated by tools/genzips.py',
    )
    splice(
        root / "ziptz.py",
        r'^RUNS = ".*"  # zip-runs: .*$',
        f'RUNS = "{runs}"  # zip-runs: generated by tools/genzips.py',
    )
    splice(
        root / "ziptz.go",
        r'^const exceptions = ".*" // zip-exceptions: .*$',
        f'const exceptions = "{exceptions}" '
        "// zip-exceptions: generated by tools/genzips.py",
    )
    splice(
        root / "ziptz.py",
        r'^EXCEPTIONS = ".*"  # zip-exceptions: .*$',
        f'EXCEPTIONS = "{exceptions}"  '
        "# zip-exceptions: generated by tools/genzips.py",
    )

    prefixes = len(votes)
    print(f"{prefixes} prefixes -> {len(runs) // 4} runs, {len(runs)} characters")
    print(
        f"{wrong_count} ZIPs the prefixes get wrong -> {len(exceptions)} characters"
    )
    print("wrote ziptz.go and ziptz.py")

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
