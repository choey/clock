# Changelog

What changed between releases, and what it means for someone running the clock.
Versions follow [semantic versioning](https://semver.org), with one reading
worth stating: the *picture* is part of the interface. A change that alters
what a frame looks like is a minor bump at least, never a patch, which is what
the golden frames in `tools/golden/` exist to make impossible to do by accident.

The version is written in `pyclock.py`, `clock.go` and `pyproject.toml`, and
`tools/docnums.py` fails the build if the three disagree.

## Unreleased

Nothing yet.

## 0.1.0

First release.

A terminal clock: one analog face per time zone, its name and digital readout
underneath, ordered by the time they read and spread over the window. Two
independent implementations, Python and Go, that render byte-for-byte identical
output — which is the project's actual claim, and what
[`tools/difftest.sh`](tools/difftest.sh) exists to hold them to across hundreds
of argument lists, window sizes and cell ratios.

Install as `terminal-clock` on PyPI or `go install github.com/choey/clock@latest`;
either way the command is `clock`. See [README.md](README.md).

Two things worth knowing about this release in particular:

- **ziptz is a separate library now.** ZIP-code resolution used to live in this
  repository as a nested module. It is [its own
  project](https://github.com/choey/ziptz), published as `ziptz-us` on PyPI and
  `github.com/choey/ziptz` for Go, and the clock depends on it like anything
  else. The Go build links it in; the Python one wants it installed, which is
  the one thing a bare clone no longer does for you.
- **`CLOCK_FREEZE` takes far more than one spelling.** The dev hook that pins
  the clock to an instant reads a plain ISO instant, a bare clock time with the
  nearest day filled in, a date with or without a year, and any zone the
  `--zones` argument accepts — resolving daylight-saving edges by an explicit
  rule rather than by whatever `time.Date` and `zoneinfo` each happen to do,
  since those two disagree.
