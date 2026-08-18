# ziptz

US ZIP code to IANA time zone, in Go and in Python, from about 1.3 KB of
tables. No dependencies, no data files, no network: both tables are string
constants compiled into the source, so a lookup is a binary search and a short
scan.

```go
name, err := ziptz.Zone("94110")   // "America/Los_Angeles"
loc, err := ziptz.Location("10001")
```

```python
ziptz.zone("94110")        # 'America/Los_Angeles'
ziptz.location("10001")    # ZoneInfo(key='America/New_York')
```

The two implementations answer identically for every ZIP code, down to the
wording of the errors — the tables are generated into both in one pass by
[`tools/genzips.py`](../tools/genzips.py), and
[`tools/difftest.sh`](../tools/difftest.sh) compares them.

## Accuracy

Give all five digits and the answer is exact. Three digits — the prefix alone —
gets the majority zone for that prefix, which is right for 33,558 of the 33,791
ZIP codes and wrong for the 233 that sit on the losing side of a zone boundary
their prefix has to round the wrong way.

```
ziptz.zone("79835")   # America/Denver  — Canutillo, TX; five digits are exact
ziptz.zone("798")     # America/Chicago — the prefix rounds to the majority
```

One gap: PO-box and single-building ZIPs have no delivery area in the source
data, so even given in full they fall back to their prefix's answer. `00501`
(Holtsville, NY) has no prefix to fall back to either, and is an error.

The tables carry today's zone *names*, not today's offsets — daylight saving
comes from whatever tzdata the machine has, so a rule change needs no new
release. They are wrong for historical dates: several places have changed zone
(Kentucky/Monticello left Central in 2000, North Dakota/Beulah left Mountain in
2010) and the tables record only where each ZIP is now.

## Install

It lives in the [clock](https://github.com/choey/clock) repository, as a module
of its own — a nested Go module and a Python package, both installable without
the clock.

```sh
go get github.com/choey/clock/ziptz     # needs a ziptz/vN.N.N tag
pip install ./ziptz                     # from a checkout
```

Or copy it. Each side is one standard-library-only file: drop `ziptz.go` into
a package of your own, or `ziptz.py` next to whatever imports it. That is what
`clock.py` itself supports — `cp clock.py ziptz/ziptz.py /usr/local/bin/` is a
complete install.

## API

| Go | Python | |
| --- | --- | --- |
| `Zone(token) (string, error)` | `zone(token) -> str` | the IANA name for a 3- or 5-digit ZIP |
| `Location(token) (*time.Location, error)` | `location(token) -> ZoneInfo` | the same, loaded from the system tz database |
| `PrefixZone(p3) string` | `prefix_zone(p3) -> str` | the majority zone for a prefix, `""` if unassigned |
| `ExactZone(zip5) string` | `exact_zone(zip5) -> str` | the exception for one ZIP, `""` if its prefix is right |

`Zone` and `Location` report an error for anything that is not three or five
ASCII digits, and for prefixes the Postal Service has never assigned. Python
raises `ZipError`, a `ValueError`. Both error texts are written to be printed
as-is.

## Data

US Census ZCTA Gazetteer centroids (a US Government work, public domain)
resolved through [timezone-boundary-builder](
https://github.com/evansiroky/timezone-boundary-builder) (ODbL), by way of
[timezonefinder](https://github.com/jannikmi/timezonefinder). The generated
output is roughly forty range boundaries and a list of exceptions — an
aggregate, not a substantial extract — but both sources are credited here and
in the source.

## Regenerating

`tools/genzips.py` writes the tables into `ziptz.go` and `ziptz.py` in one
pass, which is what keeps the two literals from drifting. Never edit them by
hand.

```sh
python3 -m venv .venv && .venv/bin/pip install timezonefinder
.venv/bin/python tools/genzips.py      # or: make regen, if it is on your path
```

It is deliberately *not* a build step. timezonefinder pulls in numpy and a
megabyte of boundary data, where ziptz itself needs neither, and a generator
that ran at install time would let two builds of one version ship different
tables. Checked-in generated source is what makes every install byte-identical
— and what lets the Go and Python copies be compared character for character.
`go generate ./...` runs the same script, for the same reason `go:generate`
exists: it is a developer's command, not the build's.

The Census archive is cached in `tools/cache/` (gitignored) and reused on
every later run, so only the first regeneration touches the network. Pass a
path to read a local copy instead.

### When to regenerate

Almost never, and *not* for daylight-saving changes. The tables store zone
names, not offsets or rules, so the answer to "is Denver on MDT today" comes
from whatever tzdata the machine has. A state dropping daylight saving, or the
country abolishing the switch, arrives with an OS update and needs nothing
here.

Regenerate when the mapping itself moves:

| what changed | why it matters |
|---|---|
| a place changes zone | Kentucky/Monticello left Central for Eastern in 2000; its ZIPs now belong to a different name |
| new or redrawn ZIP codes | a new Census gazetteer describes them |
| a zone splits from the letter it folds onto | `CANONICAL` collapses ~34 zones onto 11 letters, and that only holds while they keep the same rules |

The last is the one that could go wrong quietly, so `genzips.py` re-tests it on
every run: each folded zone is compared against its letter's zone every six
hours for the next thirteen months, and the run aborts if any of them parts
company. Indiana observed no daylight saving until 2006 and North Dakota/Beulah
left Mountain in 2010, so this is not hypothetical.

```
genzips: these zones no longer track the letter they fold onto, so folding
them would serve the wrong hour:
  America/Phoenix parts from America/Denver on 2026-08-12
Give the divergent one its own letter in CANONICAL, and add that letter to
zones/ZONES in both ziptz libraries.
```

## Tests

```sh
make test        # both, or: make test-go / make test-py
```

The cases are the point: `testdata/cases.json` holds them, and both suites
read it, so the two libraries are held to one list rather than to two that can
drift. Each case is a token and either the zone it must resolve to or the kind
of failure it must produce, with a note saying why it is there.

```json
{"token": "96799", "zone": "Pacific/Pago_Pago", "why": "the worst exception: American Samoa, an hour behind Honolulu"}
{"token": "00501", "error": "unassigned", "why": "known gap: Holtsville NY, a single-building ZIP; really America/New_York"}
```

Adding a case there is the whole edit — both suites pick it up. They cover the
ordinary lookups, both sides of every kind of boundary (first and last
exception in the table, first and last suffix of the largest group, a ZIP just
outside one, the top and bottom of the prefix range), the malformed tokens
(wrong length, letters, whitespace, a trailing newline, Arabic-Indic and
fullwidth digits that Python's `isdigit()` accepts and this must not), and the
**known gaps** — the ZIPs that resolve wrongly or not at all, pinned so that
fixing one fails the file and makes someone update it.

On top of the shared cases each suite checks the generated tables themselves:
records the right width, prefixes ascending, suffixes ascending, every letter
one that `zones`/`ZONES` knows, and every exception actually disagreeing with
the prefix it overrides.
