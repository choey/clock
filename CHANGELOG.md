# Changelog

What changed between releases, and what it means for someone running the clock.
Versions follow [semantic versioning](https://semver.org), with one reading
worth stating: the *picture* is part of the interface. A change that alters
what a frame looks like is a minor bump at least, never a patch, which is what
the golden frames in `tools/golden/` exist to make impossible to do by accident.

The version is written in `pyclock.py`, `clock.go` and `pyproject.toml`, and
`tools/docnums.py` fails the build if the three disagree.

## Unreleased

- **Every release carries prebuilt binaries.** `go install` wanted a Go
  toolchain, which was a strange price for a clock and until now the only way
  to get the Go port. Five targets, cross-compiled from one runner with
  `CGO_ENABLED=0` -- the clock is pure Go, so this costs nothing but the
  workflow. `SHA256SUMS` covers them, and the workflow checks the binary's
  `--version` against the tag before it uploads anything.
- The installation section reads as Go and Python separately rather than
  alternating between them, and three things it claimed are fixed: a `replace`
  directive in `go.mod` that does not exist, a sentence saying the Python
  command is still `clock`, and a `curl` line pinning ziptz two releases back.
- The README's examples are the installed commands now -- `clock` and
  `pyclock` -- rather than `./pyclock.py` and `go run .`, which only worked
  from a checkout and only showed one of the two ports. Which install leaves
  which command is a table at the top of the installation section rather than
  a sentence inside it, and `## Running` follows `## Installation` instead of
  preceding it, since it now shows commands you have to install first. The
  from-a-clone invocations are still there, under a heading that says so.

## 0.2.1

Packaging and the test harness. Neither clock changed -- the two tags build
the same program, and `go install ...@v0.2.0` stays as valid as this one.

For packaging, what changed is what PyPI is handed -- which 0.2.0 was tagged
before anyone had looked at. Keywords, classifiers and project links, so the
page says which pythons and which systems; a licence declared as PEP 639's
`license = "MIT"` with `license-files`, rather than the table form and a
`License ::` classifier that both carry a 2027 removal date; and README links
made absolute, because a relative `ARCHITECTURE.md` resolves against github.com
in a repository and against nothing at all on PyPI.

The version number exists so that the tag and the uploaded artifact are the
same thing. 0.2.0 was tagged, then improved, and rather than move a tag the
Go proxy has already cached, this is the tag that matches what was published.

And two things in `tools/difftest.sh` are fixed, which matter more than any of
the above:

- **It reports its result again.** Splitting ziptz out took the ziptz checks
  off the end of the file and took the two lines below them too -- the summary
  and `[ "$fail" -eq 0 ]`. Since then it had counted every failure and then
  exited on whatever the last command happened to return, which was always
  zero. `make test` could not fail on a difftest case in 0.1.0 or 0.2.0.
- **The without-ziptz case hides ziptz again.** It copies `pyclock.py` into an
  empty directory and runs it there, which stopped meaning anything once ziptz
  became a `pip install` that follows the interpreter everywhere. It runs under
  `-S` now, which drops site-packages and leaves the standard library. That
  case had been failing, invisibly, for exactly as long as the summary was
  missing.

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
