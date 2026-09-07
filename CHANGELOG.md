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

## 0.2.0

Two names moved. Nothing about the picture changed.

- **The Python command is `pyclock`.** `go install` produces a binary called
  `clock` -- that is the last element of the Go module path, not a choice -- so
  a pip entry point by the same name meant the two shadowed each other on
  `PATH` and could not be installed together. Apart, they compose:
  `clock ET,PT | diff - <(pyclock ET,PT)`, which is the first time this
  project's central claim has been checkable without a clone. `clock.py` is
  `pyclock.py`, and the module it installs is `pyclock` -- `clock` on PyPI is
  an unrelated 2014 package that claimed the same top-level name, and whichever
  installed second silently overwrote the other.
- **`go install github.com/choey/clock@latest` works.** It never did: `go.mod`
  declared `module clock`, so the proxy refused it with "module declares its
  path as: clock". `v0.1.0` is cached with the wrong path and stays broken;
  this is the first tag that can be installed that way.
- ZIP resolution is `ziptz-us` from PyPI now, not a directory in this
  repository. `make setup` builds a `.venv` for the test suite, which a system
  python can no longer satisfy on its own.

The program still calls itself `clock` in its own usage text, in both ports.
`--help` is compared byte for byte, so a port that renamed itself there would
fail every case.

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
