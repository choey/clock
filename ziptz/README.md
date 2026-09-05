# ziptz

US ZIP code to IANA time zone, in Go and in Python, from about 1.3 KB of
tables. No dependencies, no data files, no network: both tables are string
constants compiled into the source, so a lookup is a binary search and a short
scan.

```go
name, err := ziptz.Zone("94110")          // "America/Los_Angeles"
loc, err := ziptz.Location("10001")       // *time.Location
abb, err := ziptz.Abbrev("94110", when)   // "PST" in January, "PDT" in July
gen, err := ziptz.Generic("94110")        // "PT", whatever the date
```

```python
ziptz.zone("94110")           # 'America/Los_Angeles'
ziptz.location("10001")       # ZoneInfo(key='America/New_York')
ziptz.abbrev("94110", when)   # 'PST' in January, 'PDT' in July
ziptz.generic("94110")        # 'PT', whatever the date
```

The two implementations answer identically for every ZIP code, down to the
wording of the errors — the tables are generated into both in one pass by
[`tools/genzips.py`](tools/genzips.py), and `make test` puts all 101,000
tokens through both and compares every answer. Not a claim; a build step.

## What it is for, and what it is not for

It is small enough to stop being a dependency and start being a file. The
library is a single 10 KB source file per language, 1.3 KB of which is the
tables, and it occupies under a megabyte once imported. Neither side has a
dependency, and `Zone` and `Generic` read no time zone database at all, so both
answer on a machine that has none. That is what makes vendoring one file a real
option rather than a compromise.

The 23 KB wheel is mostly not the library: the tests and their case file ship
with it on purpose, so an installed copy can prove itself where it landed
rather than only in a checkout.

It covers the places a US-only table usually forgets:

```
00601  America/Puerto_Rico     00802  America/Puerto_Rico   (US Virgin Islands)
96799  Pacific/Pago_Pago       96910  Pacific/Guam
96950  Pacific/Guam            (Northern Mariana Islands)
```

And it answers a 3-digit prefix, not just a whole ZIP, which is what you have
when an address is partial or a form was only half filled in.

Three things it is deliberately or unavoidably bad at:

- **Bulk lookups.** Every five-digit query binary-searches the run table and
  then scans the exception list. That is nothing for one lookup and the wrong
  shape for millions of them; a flat dictionary would beat it comfortably, and
  this trades that away for the 1.3 KB.
- **Zone identity.** About 34 IANA zones are folded onto the 11 that agree with
  them *today*, so a ZIP in Knox County, Indiana answers `America/New_York`
  rather than `America/Indiana/Knox`. The clock is right; the name is coarser
  than the database's. [When to
  regenerate](#when-to-regenerate) explains what keeps that true.
- **Validating ZIP codes.** A five-digit code with an assigned prefix always
  gets an answer, whether or not the Postal Service has ever issued it. An
  error means "no such prefix", never "no such ZIP".

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

```sh
go get github.com/choey/ziptz
pip install ziptz-us          # imports as ziptz; see below
```

The Python distribution is `ziptz-us` and the module is `ziptz` — the same
split as `python-dateutil`/`dateutil`. `ziptz` on PyPI is a 2013-era name
registration with no files ever attached, so `pip install ziptz` fails for
everyone; the `-us` is also simply true, since this resolves US ZIP codes and
nothing else. Go has no central registry, so the import path there is the
repository's own.

Or copy it. Each side is one standard-library-only file: drop `ziptz.go` into
a package of your own, or `ziptz.py` next to whatever imports it — no build
step, nothing to fetch, and the tables come along because they *are* source.
That is a supported way to use this rather than a workaround: at 1.3 KB of
tables, the library is smaller than most manifests that would name it.

Python therefore imports two ways, and both answer the same: `ziptz.py` alone
is a module, and the directory around it is a package whose `__init__.py`
hands through to that module — which is what lets a clone `import ziptz` with
nothing installed. A test compares the two, since only the package form is
what a wheel contains.

The Python side is annotated and ships `py.typed`, so mypy and editors read the
signatures rather than treating the package as untyped. The annotations are
`from __future__ import annotations` strings, which is what lets them be spelled
`datetime | None` while the package still imports on the 3.9 it supports.

## API

| Go | Python | |
| --- | --- | --- |
| `Zone(token) (string, error)` | `zone(token) -> str` | the IANA name for a 3- or 5-digit ZIP |
| `Location(token) (*time.Location, error)` | `location(token) -> ZoneInfo` | the same, loaded from the system tz database |
| `Abbrev(token, at) (string, error)` | `abbrev(token, at=None) -> str` | the abbreviation at a given instant — `PST` in winter, `PDT` in summer |
| `Generic(token) (string, error)` | `generic(token) -> str` | the name without daylight saving, e.g. `PT` |
| `PrefixZone(p3) string` | `prefix_zone(p3) -> str` | the majority zone for a prefix, `""` if unassigned |
| `ExactZone(zip5) string` | `exact_zone(zip5) -> str` | the zone for one of the 233 ZIPs its prefix gets wrong, `""` for the rest |

Those first four report an error for anything that is not three or five ASCII
digits, and for prefixes the Postal Service has never assigned. Python raises
`ZipError`, a `ValueError`. Both error texts are written to be printed as-is.

The last two are the two table lookups `Zone` is built from, exposed for a
caller that wants to know which of them answered. Neither validates its input
and neither reports an error: each returns `""` for anything it has no record
of. For `ExactZone` that is almost every ZIP — only the 233 exceptions have a
record at all — so `""` there means *no exception; the prefix is the answer*,
not *unknown*. `Zone` is exactly the two composed in that order.

### What `Location` costs

`Location` and `location` return the same thing under two names: `*time.Location`
is Go's loaded zone and `ZoneInfo` is Python's, and neither language spells it
the other's way. What differs is the price of asking twice, and that is the
standard libraries' doing rather than this library's.

```
20,000 calls for one zone       (one machine; the ratio is the point, not the ms)
  Go   time.LoadLocation      459 ms    reads the tz database every call
  Py   ZoneInfo                 2 ms    interned by name; the first call does the work
  Py   ZoneInfo.no_cache    1,053 ms    what that cache is saving
```

Python interns by name, so `location("94110") is location("90210")` — two ZIPs,
one zone, one object. Go does not, and every `Location` is a file read. Hold the
result if you are calling it per row of anything, and note that `Abbrev` goes
through `Location` and inherits the same cost.

`Zone` and `Generic` are the cheap ones in both languages: a binary search and a
short scan over a string constant, with no I/O at all and nothing to cache.

### The three names for one zone

`Zone` gives the IANA name, `Abbrev` the abbreviation at an instant, `Generic`
the abbreviation with the daylight-saving question left out — CLDR's terms for
the last two are the specific and generic non-location short formats.

```
94110  →  America/Los_Angeles      PST in January, PDT in July      PT
85001  →  America/Phoenix          MST all year                     MST
99546  →  America/Adak             HST in January, HDT in July      HAT
```

Which to print depends on whether you have an instant to be right about. A
label on a clock face has one; a form field asking which coast you are on does
not.

They come from different places, which decides what each needs and when each
can be wrong. `Abbrev` asks the system's time zone database, so it follows
rule changes without a new release of this library — but needs that database
present, and needs the instant. `Generic` is a table here, eleven entries, and
so answers on a machine with no tzdata at all. A zone that never shifts has no
pair to generalise over, so its generic name is simply its abbreviation:
Phoenix is `MST`, Honolulu `HST`, and the tests hold every entry to that rule.

Go has no default arguments, so `Abbrev` always takes the instant; Python's
defaults to now. Pass an aware `datetime` — a naive one is read as system
local time, the way `astimezone()` reads it.

## Data

US Census ZCTA Gazetteer centroids (a US Government work, public domain)
resolved through [timezone-boundary-builder](
https://github.com/evansiroky/timezone-boundary-builder) (ODbL), by way of
[timezonefinder](https://github.com/jannikmi/timezonefinder). The generated
output is 157 range records, 94 of which name a zone, and 233 exceptions — an
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
make test        # both suites, then the sweep below
```

`make test-go` and `make test-py` run one suite each. `make sweep` is the
third thing `make test` does, and the one the cases cannot be: every ZIP there
is, through both libraries, compared. All 1,000 prefixes and all 100,000
five-digit codes — 101,000 answers, zone names and error text alike — must
come out identical, which is what makes "the two answer the same, ZIP for ZIP"
a measured claim rather than a hopeful one. The cases above cover what someone
thought of; this covers what nobody did.

An installed copy carries its tests and their data, so it can prove itself
where it landed rather than only in a checkout:

```sh
python3 -m unittest ziptz.test_ziptz
go test github.com/choey/ziptz
```

The data is the point: `testdata/cases.json` holds the cases, the zones, the
figures the generated tables should contain, and — in `checks` — the names of
the properties both suites must test. Each suite maps every name to a test and
fails on one it does not implement, so neither can quietly cover less than the
other. Cases alone were not enough: the structural checks around them were
written twice, once per language, and two hand-written lists drift — one suite
had a check on the exception suffixes being in order for a while before the
other did. Each case is a token and either the zone it must resolve to or the kind
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

Only two things are language-only, and the file says which: Go has no default
arguments, so `abbrev`'s default of now is Python's to test, and only Python
can be imported two ways, as a module and as a package.

## Licence

The code is MIT; see [LICENSE](LICENSE).

The tables are a separate question, and [NOTICE](NOTICE) is the answer to it.
They were produced from public-domain Census centroids resolved through
timezone-boundary-builder, which is ODbL — so `NOTICE` carries that
attribution and the reasoning for treating 1.3 KB of zone names as a produced
work rather than an extract of the boundary database. It ships in the wheel
and the sdist, and travels with the Go module. Keep it with any copy you make,
including the copy-one-file install above.

## Credits

The hard part was already done by other people. [Evan
Siroky](https://github.com/evansiroky/timezone-boundary-builder) builds the
time zone boundaries out of OpenStreetMap, [Jannik
Michel](https://github.com/jannikmi/timezonefinder) makes them queryable in
Python, and the US Census Bureau publishes the ZCTA centroids. This library is
the small, boring artefact left over once their work has been asked 33,791
questions.
