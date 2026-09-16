#!/usr/bin/env python3
"""Hold the state and city tables to what they claim.

US_STATES and COMMON_CITIES are written by hand, and a hand-written row can go
wrong in ways nothing else notices. Its zone can fail to load, so the name
errors only on the day someone types it. It can be unreachable: something
resolve_zone asks first -- the tz database, an abbreviation, a country code --
already answers to that name, so the row is dead and its label never shows.
Or it can be out of order or doubled, so the next edit to clock.go's copy puts
the two out of step in a way difftest reports as the ports disagreeing rather
than as the table mistake it is.

So every row goes through the real resolve_zone in each spelling that has to
reach it -- as written, with underscores for its spaces, lower-cased and
upper-cased -- and has to land on its own zone under its own name. difftest holds clock.go's copy of the
tables to this one, which makes this check cover both ports.

US_CODES is checked the same way: each ISO 3166-2 code has to answer exactly
as the full name it stands for does, and the bare two letters have to stay
whatever they already were -- a country, an abbreviation, or nothing.

    tools/placecheck.py [-v]
    tools/placecheck.py --geonames DIR [-v]

--geonames is for when a row is added or changed. DIR holds cities1000.txt from
https://download.geonames.org/export/dump/, and each state is checked against
its capital's zone, each territory against zone.tab, and each city against the
largest city of that name in its country. It also applies the rule the city
table was built under: a name shared by cities on different clocks is kept only
when the largest on this clock is three times the size of any namesake on
another. Two zones are one clock when they read the same abbreviation and
offset at every probe instant, the test the clock merges faces on.
"""

import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pyclock  # noqa: E402

# What --geonames checks each row against. Every row needs an entry, checked
# with or without --geonames, so a row added without one fails here rather
# than being skipped by the check that would have caught it.
CAPITALS = {
    "Alabama": ("Montgomery", "AL"), "Alaska": ("Juneau", "AK"), "Arizona": ("Phoenix", "AZ"),
    "Arkansas": ("Little Rock", "AR"), "California": ("Sacramento", "CA"), "Colorado": ("Denver", "CO"),
    "Connecticut": ("Hartford", "CT"), "Delaware": ("Dover", "DE"), "Florida": ("Tallahassee", "FL"),
    "Georgia": ("Atlanta", "GA"), "Hawaii": ("Honolulu", "HI"), "Idaho": ("Boise", "ID"),
    "Illinois": ("Springfield", "IL"), "Indiana": ("Indianapolis", "IN"), "Iowa": ("Des Moines", "IA"),
    "Kansas": ("Topeka", "KS"), "Kentucky": ("Frankfort", "KY"), "Louisiana": ("Baton Rouge", "LA"),
    "Maine": ("Augusta", "ME"), "Maryland": ("Annapolis", "MD"), "Massachusetts": ("Boston", "MA"),
    "Michigan": ("Lansing", "MI"), "Minnesota": ("Saint Paul", "MN"), "Mississippi": ("Jackson", "MS"),
    "Missouri": ("Jefferson City", "MO"), "Montana": ("Helena", "MT"), "Nebraska": ("Lincoln", "NE"),
    "Nevada": ("Carson City", "NV"), "New Hampshire": ("Concord", "NH"), "New Jersey": ("Trenton", "NJ"),
    "New Mexico": ("Santa Fe", "NM"), "North Carolina": ("Raleigh", "NC"), "North Dakota": ("Bismarck", "ND"),
    "Ohio": ("Columbus", "OH"), "Oklahoma": ("Oklahoma City", "OK"), "Oregon": ("Salem", "OR"),
    "Pennsylvania": ("Harrisburg", "PA"), "Rhode Island": ("Providence", "RI"),
    "South Carolina": ("Columbia", "SC"), "South Dakota": ("Pierre", "SD"), "Tennessee": ("Nashville", "TN"),
    "Texas": ("Austin", "TX"), "Utah": ("Salt Lake City", "UT"), "Vermont": ("Montpelier", "VT"),
    "Virginia": ("Richmond", "VA"), "Washington": ("Olympia", "WA"), "West Virginia": ("Charleston", "WV"),
    "Wisconsin": ("Madison", "WI"), "Wyoming": ("Cheyenne", "WY"),
    "District of Columbia": ("Washington", "DC"), "Washington DC": ("Washington", "DC"),
    "Washington D.C.": ("Washington", "DC"),
}
TERRITORIES = {"US Virgin Islands": "VI", "U.S. Virgin Islands": "VI", "American Samoa": "AS",
               "Northern Mariana Islands": "MP"}
COUNTRIES = {
    "US": "Albuquerque Atlanta Austin Baltimore Boston Charlotte Cincinnati Cleveland Columbus Dallas "
          "El_Paso Fort_Worth Houston Jacksonville Kansas_City Las_Vegas Memphis Miami Milwaukee Minneapolis "
          "Nashville New_Orleans New_York_City Oklahoma_City Omaha Orlando Philadelphia Pittsburgh Portland "
          "Raleigh Sacramento Salt_Lake_City San_Antonio San_Diego San_Francisco Seattle Tampa Tucson",
    "CA": "Calgary Montreal Ottawa Quebec_City", "MX": "Guadalajara", "BR": "Brasilia Rio_de_Janeiro",
    "CN": "Beijing Chengdu Guangzhou Shenzhen",
    "IN": "Bangalore Bengaluru Chennai Delhi Hyderabad Mumbai New_Delhi Pune",
    "JP": "Kyoto Osaka Yokohama", "KR": "Busan", "VN": "Hanoi", "IL": "Tel_Aviv", "AE": "Abu_Dhabi",
    "SA": "Jeddah Mecca", "PK": "Islamabad Lahore", "DE": "Cologne Frankfurt Hamburg Munich",
    "IT": "Florence Milan Naples", "ES": "Seville", "FR": "Lyon Marseille",
    "GB": "Edinburgh Glasgow Manchester", "CH": "Geneva", "NL": "Rotterdam The_Hague",
    "RU": "Saint_Petersburg St._Petersburg", "PL": "Krakow", "TR": "Ankara",
    "ZA": "Cape_Town Durban Pretoria", "AU": "Canberra", "NZ": "Christchurch Wellington", "NG": "Abuja",
}
CITY_COUNTRY = {n.replace("_", " "): cc for cc, names in COUNTRIES.items() for n in names.split()}
# Where GeoNames files a city under another spelling than the table's: its
# local name, or the longer official one. Only for finding the city itself --
# namesakes are still looked for under the name a person types.
GEONAMES_AS = {"Bangalore": "Bengaluru", "Cologne": "Koln", "Frankfurt": "Frankfurt am Main",
               "Mecca": "Makkah", "Quebec City": "Quebec", "Seville": "Sevilla",
               "St. Petersburg": "Saint Petersburg"}

# Names the city table leaves out on purpose, for failing the three-times rule
# against a namesake on another clock. They have to stay unknown zones: one
# that started resolving would be a decision reversed without anyone deciding.
LEFT_OUT = ("San Jose", "St. Louis", "Saint Louis", "Barcelona", "Venice")
# Names the state table leaves to the tz database, which it asks first. They
# have to resolve there, under that name, or the table's comment is wrong.
LEFT_TO_TZ = ("New York", "Puerto Rico", "Guam")
# Two-letter state codes are not taken, because these are countries already.
COUNTRY_NOT_STATE = ("CA", "IN", "DE", "GA")
# Countries come out of the tz database's own iso3166.tab rather than a table
# here, so there is no list to hold to -- but the label has to be right, and
# the names that ordering decides have to stay decided.
#
# (token, a zone it must read the same clock as, the label). The zone is
# compared as a clock and not by name on purpose: GB, Japan and Portugal land
# on tzdata's backward-compatibility links where a build installs them and on
# the country's own zone where it does not, and those are the same clock. This
# check ran on a machine that has them and would have failed on one that does
# not, which is a property of the machine and not of the clock.
COUNTRIES = (
    ("DE", "Europe/Berlin", "DE"),
    ("de", "Europe/Berlin", "DE"),
    ("GB", "Europe/London", "GB"),
    ("Malta", "Europe/Malta", "Malta"),
    ("Georgia", "America/New_York", "Georgia"),
    ("GE", "Asia/Tbilisi", "GE"),
)
# Tokens whose label is iso3166.tab's own spelling, which is the database's to
# choose and not this repository's to write down: (code, a zone the country
# must read alike). The name is read out of the file, and where it holds an "&"
# the "and" spelling has to answer the same way.
COUNTRY_NAMES = (
    ("DE", "Europe/Berlin"),
    ("JP", "Asia/Tokyo"),
    ("PT", "Europe/Lisbon"),
    ("AG", "America/Antigua"),
)

PROBES = [datetime(y, m, d, 12, tzinfo=timezone.utc)
          for y in (2026, 2027) for m, d in ((1, 15), (3, 20), (4, 15), (7, 15), (10, 1), (11, 15))]


def one_clock(zone):
    z = ZoneInfo(zone)
    return tuple((t.astimezone(z).tzname(), t.astimezone(z).utcoffset()) for t in PROBES)


class Report:
    def __init__(self, verbose):
        self.verbose, self.passed, self.failed = verbose, 0, 0

    def __call__(self, ok, what):
        if ok:
            self.passed += 1
            if self.verbose:
                print(f"ok   {what}")
        else:
            self.failed += 1
            print(f"FAIL {what}")


def iso3166_name(code):
    """The tz database's own English name for a country code, or None."""
    data, found = pyclock.tz_file("iso3166.tab")
    if not found:
        return None
    for line in data.split("\n"):
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 2 and fields[0] == code:
            return fields[1]
    return None


def resolves(name, now):
    try:
        zone, place = pyclock.resolve_zone(name, now)
    except pyclock.ClockError as exc:
        return None, str(exc)
    return (zone.key if zone is not None else None), place


def check_tables(report):
    now = datetime.now(timezone.utc)
    tables = (("US_STATES", pyclock.US_STATES), ("COMMON_CITIES", pyclock.COMMON_CITIES))
    seen = {}
    for title, table in tables:
        names = [name for name, _ in table]
        report(names == sorted(names), f"{title} is sorted")
        for name, zone in table:
            key = pyclock.place_key(name)
            report(key not in seen, f"{name} appears once across both tables"
                   + (f" (also {seen[key]})" if key in seen else ""))
            seen[key] = name
            report(name.isascii() and len(name) != 2 and ".." not in name and not name.startswith("/"),
                   f"{name} is a name resolve_zone can be handed")
            try:
                ZoneInfo(zone)
                loads = True
            except Exception:
                loads = False
            report(loads, f"{name}: {zone} loads")
            spellings = {name, name.replace(" ", "_"), pyclock.ascii_lower(name), pyclock.ascii_upper(name)}
            for spelling in sorted(spellings):
                got = resolves(spelling, now)
                report(got == (zone, name), f"{spelling!r} resolves to {zone} as {name!r}"
                       + ("" if got == (zone, name) else f" -- got {got}"))
            if title == "US_STATES":
                report(name in CAPITALS or name in TERRITORIES, f"{name} has a capital or country to check against")
            else:
                report(name in CITY_COUNTRY, f"{name} has a country to check against")
    codes = [code for code, _ in pyclock.US_CODES]
    report(codes == sorted(codes), "US_CODES is sorted")
    report(len(codes) == len(set(codes)), "US_CODES has no code twice")
    for code, name in pyclock.US_CODES:
        want = resolves(name, now)
        report(want[1] == name, f"{code}: {name!r} resolves as itself -- got {want}")
        for spelling in sorted({code, pyclock.ascii_lower(code), pyclock.ascii_upper(code)}):
            got = resolves(spelling, now)
            report(got == want, f"{spelling!r} answers as {name!r} does"
                   + ("" if got == want else f" -- {got} against {want}"))
        report(pyclock.place_key(code) not in seen, f"{code} is not also a row name")
        # The reason the codes carry the US- prefix: the bare two letters are
        # countries and abbreviations already, and must never answer as the
        # state does. Some reach the same zone by accident -- MT is Montana and
        # Mountain alike -- so it is the whole answer, label included, that has
        # to differ.
        bare = code[len("US-"):]
        got = resolves(bare, now)
        report(got != want, f"{bare} alone does not answer as {name!r} does ({got})")
    for name in LEFT_OUT:
        zone, why = resolves(name, now)
        report(zone is None and why.startswith("unknown zone"), f"{name} stays out: {why[:60]}")
    for name in LEFT_TO_TZ:
        zone, place = resolves(name, now)
        report(zone is not None and place == name, f"{name} is the tz database's, as {name!r}")
    def reads_like(key, zone):
        try:
            return key is not None and one_clock(key) == one_clock(zone)
        except Exception:
            return False

    for token, zone, label in COUNTRIES:
        key, place = resolves(token, now)
        ok = reads_like(key, zone) and place == label
        report(ok, f"{token!r} reads as {zone} and is labelled {label!r}"
               + ("" if ok else f" -- got {(key, place)}"))
    for code, zone in COUNTRY_NAMES:
        name = iso3166_name(code)
        if name is None:
            report(False, f"iso3166.tab has no name for {code}")
            continue
        for token in {name, name.replace(" & ", " and ")}:
            key, place = resolves(token, now)
            ok = reads_like(key, zone) and place == name
            report(ok, f"{token!r} reads as {zone} and is labelled {name!r}"
                   + ("" if ok else f" -- got {(key, place)}"))
    # A country that genuinely spans zones is refused by the name that was
    # typed, not by the code it was looked up from.
    for token in ("US", "United States"):
        zone, why = resolves(token, now)
        report(zone is None and why.startswith(f"{token} spans"),
               f"{token!r} is refused in its own name: {why[:52]}")
    for code in COUNTRY_NOT_STATE:
        zone, place = resolves(code, now)
        # A country that spans zones is refused by name -- which is the country
        # path answering, and exactly as much a country as one that resolves.
        # A code that resolves is labelled with itself; one whose country
        # spans zones is refused by name. Either is the country path answering.
        country = (zone is not None and place == code) or (zone is None and place.startswith(f"{code} spans"))
        report(country, f"{code} is a country code, not a state ({zone or place[:40]})")


def norm(s):
    """A name as a person reads it: accents off, hyphens and dots as spaces, St as Saint."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\s+", " ", re.sub(r"[-.]", " ", s)).strip()
    return re.sub(r"^st ", "saint ", s)


def check_geonames(report, directory):
    rows = []
    with open(Path(directory) / "cities1000.txt", encoding="utf-8") as handle:
        for line in handle:
            f = line.rstrip("\n").split("\t")
            rows.append({"name": f[1], "ascii": f[2], "cc": f[8], "a1": f[10],
                         "pop": int(f[14] or 0), "tz": f[17]})
    by_name = {}
    for r in rows:
        for key in {norm(r["name"]), norm(r["ascii"])}:
            by_name.setdefault(key, []).append(r)
    zonetab = {}
    for line in open("/usr/share/zoneinfo/zone.tab", encoding="utf-8"):
        if line.strip() and not line.startswith("#"):
            f = line.rstrip("\n").split("\t")
            zonetab.setdefault(f[0], []).append(f[2])

    def clock_of(zone):
        try:
            return one_clock(zone)
        except Exception:
            return None

    for name, zone in pyclock.US_STATES:
        if name in TERRITORIES:
            want = zonetab.get(TERRITORIES[name], ["?"])[0]
            report(clock_of(zone) == clock_of(want), f"{name}: {zone} is zone.tab's {want}")
            continue
        if name not in CAPITALS:
            report(False, f"{name}: no capital recorded, so its zone cannot be checked")
            continue
        capital, a1 = CAPITALS[name]
        hits = [r for r in by_name.get(norm(capital), []) if r["cc"] == "US" and r["a1"] == a1]
        if not hits:
            report(False, f"{name}: capital {capital}, {a1} is not in GeoNames")
            continue
        top = max(hits, key=lambda r: r["pop"])
        report(clock_of(zone) == clock_of(top["tz"]), f"{name}: {zone} is {capital}'s {top['tz']}")

    for name, zone in pyclock.COMMON_CITIES:
        cc = CITY_COUNTRY[name]
        home = [r for r in by_name.get(norm(GEONAMES_AS.get(name, name)), []) if r["cc"] == cc]
        if not home:
            report(False, f"{name}: not in GeoNames under {cc}")
            continue
        top = max(home, key=lambda r: r["pop"])
        report(clock_of(zone) == clock_of(top["tz"]), f"{name}: {zone} is {top['name']}, {cc}'s {top['tz']}")
        typed = by_name.get(norm(name), [])
        ours = max([top["pop"]] + [r["pop"] for r in typed if r["pop"] >= 20000
                                   and clock_of(r["tz"]) == clock_of(zone)])
        others = [r for r in typed if r["pop"] >= 20000 and clock_of(r["tz"]) != clock_of(zone)]
        if others:
            rival = max(others, key=lambda r: r["pop"])
            ratio = ours / rival["pop"]
            report(ratio >= 3, f"{name}: {ratio:.1f}x {rival['name']}, {rival['cc']} ({rival['tz']})")


def main():
    args = sys.argv[1:]
    report = Report("-v" in args)
    check_tables(report)
    if "--geonames" in args:
        check_geonames(report, args[args.index("--geonames") + 1])
    print(f"\n{report.passed} passed, {report.failed} failed")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
