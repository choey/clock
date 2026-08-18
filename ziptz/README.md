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
https://github.com/evansiroky/timezone-boundary-builder) (ODbL). The generated
output is roughly forty range boundaries and a list of exceptions — an
aggregate, not a substantial extract — but both sources are credited here, in
the source, and in the parent project's README.

To regenerate, from the repository root:

```sh
pip install timezonefinder
tools/genzips.py
```

Never edit the table literals by hand: they are written into `ziptz.go` and
`ziptz.py` in the same pass, and hand-editing one is how the two stop agreeing.

## Tests

```sh
go test ./...
python3 -m unittest -q test_ziptz
```

`test_ziptz.py` mirrors `ziptz_test.go` case for case; both check the known
ZIPs, the error paths, and the shape of the generated tables.
