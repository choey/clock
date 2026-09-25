# Changelog

What changed between releases, and what it means for someone running the clock.
Versions follow [semantic versioning](https://semver.org), with one reading
worth stating: the *picture* is part of the interface. A change that alters
what a frame looks like is a minor bump at least, never a patch, which is what
the golden frames in `tools/golden/` exist to make impossible to do by accident.

The version is written in `pyclock.py`, `clock.go` and `pyproject.toml`, and
`tools/docnums.py` fails the build if the three disagree.

## 0.4.3

FreeBSD builds again. 0.4.2's one select-per-frame reached for
`syscall.FdSet`, whose single field FreeBSD spells `X__fds_bits` where every
other platform says `Bits` -- so the release build got four of its five
targets and stopped, and v0.4.2 has no binaries to its name. The question is
asked per platform now, in the files that already existed for the three
termios requests that differ, and all five targets are built here before
tagging as well as in CI.

## 0.4.2

0.4.1 was the right diagnosis and the wrong cure. Batching the bytes did not
help, because the reader was never the problem -- its being there was: a
goroutine sitting in `read(2)` is handed the first byte of a keystroke the
moment the terminal has it, so an arrow can come back as `ESC` and then `[C`,
and a typed `5%` as `5` and then `%`. pyclock.py cannot do that, because
between frames it is not reading at all.

So `clock` now asks stdin for keys the way pyclock.py always has: one
select-then-read a frame, at the same point in the loop, answering whatever
has gathered since the last one. The goroutine, the channel and its
dropped-byte fallback are gone with it. `syscall.Select` turns out to be the
fourth name that differs between the BSDs and Linux, and joins the three
termios requests that already do.

- Reading stdin also means only reading it when a terminal is on the other
  end: a redirected clock would otherwise eat whatever it was handed.
- keytest compares the tuner by what it said -- the values, the knob picked,
  the refusals, in order -- rather than by how many frames it said each in.
  Where a read boundary falls inside a keystroke is the terminal's business,
  and two implementations can answer the same arrow in one frame and two
  without either being wrong. It is stricter about content than what it
  replaced and blind to timing, which is the right way round.

## 0.4.1

One read of keys per frame, in both ports. 0.4.0's tuner made typing matter
for the first time, and the two implementations were taking keys off the
terminal differently: pyclock.py answers one `os.read` and draws, where
clock.go answered one byte and drew. Fast enough, that is the same thing;
loaded -- a CI runner was the first machine slow enough to show it -- a tick
lands in the middle of a keystroke and Go paints a frame Python never paints.
`clock` now takes a read of the same 64 bytes per frame and answers all of it,
which is the rule written the other way round.

Nothing about a single keypress changes. What changes is a burst of them: a
held arrow, or a value pasted in, is now spread over the same frames in both.

- keytest holds it with a burst longer than one read -- 33 arrows, 99 bytes,
  which has to be two frames either side rather than one or thirty-three.

## 0.4.0

A clock you can shape while it runs, and one that remembers how you shaped it.
`r` opens the seven knobs that decide the grid and works them against the
faces themselves; `S` writes what is on screen to `~/.config/clock/config`,
which every clock started afterwards reads.

Nothing about an existing command line draws differently, and nothing new is
read unless a file is saved -- but the keys and the file are both interface,
so this is a minor release. A ZIP now labels its face like every other place,
which is the one thing here that changes a picture.

- **`r` tunes the layout while the clock runs.** The seven knobs that shape
  the grid -- `--halign`, `--valign`, `--hpad`, `--vpad`, `--per-row`,
  `--scale` and `--cell-ratio` -- can now be worked against the clocks
  themselves instead of against a guess, with the faces redrawing as each
  value is taken. Left and right step a value and space walks a list round;
  each knob shows what it holds in brackets between the values either side of
  it, so the size of a step is on screen rather than something to find out by
  pressing a key. Typing one still works, and is read by exactly the parser
  its flag uses, so what the command line refuses the tuner refuses, in the
  same words. Leaving says what it would have taken to start the clock that
  way, and prints it again once the screen has gone back to the shell.
- **`S` saves the clock, and the next one starts that way.** The layout the
  tuner left, the per-row count, and the zones on screen, written to
  `~/.config/clock/config` and read at startup. The file is an argument list
  rather than a format of its own -- one token per line, read by the same
  parser the command line goes through, before the command line goes through
  it -- so a flag typed beats the same flag saved, everything the command line
  takes the file takes, and a zone list with a space in it needs no quoting.
  `CLOCK_CONFIG` points somewhere else, and set-but-empty means no file at
  all.
- **A ZIP code says where it landed, like every other place.** `clock 94110`
  draws `PDT (94110)` where it drew `PDT`, which is the label 0.3.0 should have
  given it: a ZIP is the token whose zone is least guessable of all, and it was
  the one place form left without one. That changes the picture, so the next
  release is a minor.
- **A country answers to the spelling people actually write.** The tz database
  files `Britain (UK)`, `Korea (South)` and `St Lucia`; `United Kingdom`,
  `South Korea` and `Saint Lucia` now reach them. Three mechanical rules do
  most of it -- an `&` written `and`, an `St` written `Saint`, a trailing
  qualifier moved to the front -- and a short table of ten covers what no rule
  reaches, `USA` and `Czechia` and `Ivory Coast` among them. It is a list of
  exceptions, not a second copy of the database, and it answers even where
  `iso3166.tab` is missing.
- The label is the spelling that was typed rather than the file's, so nobody
  reads `KST (Korea (South))` for having written `South Korea`.
  `Antigua and Barbuda` draws its own spelling now instead of the file's `&`.

## 0.3.0

Places, and faces that say which place they are. A zone can be a US state,
one of about a hundred common cities, or a country by name or by code -- and
a face that a place landed on now says so in parentheses: `MST (Arizona)`,
`PDT (Seattle)`, `CEST (DE)`.

That last part changes what an existing command line draws, which is what
makes this a minor release rather than a patch: `clock Berlin` reads
`CEST (Berlin)` where it read `CEST`, and `clock JP` reads `JST (JP)`. Five
golden frames were re-blessed for it, each one line, each the label row.

- **US states and common cities are zones, and a face says where one landed.**
  `clock Arizona,Boise,"Salt Lake City"` draws `MST (Arizona)` and
  `MDT (Boise, Salt Lake City)`. A state means the zone its capital keeps,
  which for a state that spans two is not every resident's clock -- the label
  is how they find out. About a hundred cities the tz database has no zone for
  are in a table checked against GeoNames, and a name shared with a comparably
  large city on another clock is left out, which is why `San Jose` and
  `St. Louis` are not there.
- `tools/keytest.py`'s hold case reads each phase on its own frames. It
  compared the whole run against fixed counts, which made it flake on a loaded
  CI runner -- 0.6s bought eight frames where a quiet machine paints thirty --
  and, worse, could not see a clock that held and never let go, since the
  readouts painted before the first keystroke satisfied the count that was
  meant to prove the clock had started again. Both sabotages fail it now; only
  the first did before.
- keytest's job-control shim reader splits the line the shim sends it without
  checking it first, so a shim that died before naming the clock took the whole
  run down with `IndexError: list index out of range` -- about one run in ten
  here. It now says what it was handed instead. The race itself, a shim that
  fails to start, is not fixed.
- `go.mod` asks for `ziptz v0.1.2`, the version pip resolves `ziptz-us` to. It
  had been pinned at v0.1.0 since the split: the tables are identical between
  those tags, so nothing was wrong, but a release whose two ports name
  different versions of the same library is drift waiting to be a bug.
- `tools/placecheck.py`'s country checks compare clocks rather than zone names,
  and read the labels out of `iso3166.tab` instead of repeating its spellings.
  They had encoded one machine's tzdata: `GB` and `Japan` land on
  backward-compatibility links where a build installs them and on the country's
  own zone where it does not, which is the same clock and would have failed the
  check on the second kind of machine.
- **A place that spans zones names every one of them.** The message stopped at
  eight and counted the rest -- `Asia/Chita (and 3 more)` -- which is a list
  you cannot choose from, since the three it withheld were three of the
  answers. Russia's eleven and Canada's ten are all there now, and so is every
  zone behind an ambiguous city name.
- **A country says where it landed too, by code or by name.** `clock DE` draws
  `CEST (DE)` where it drew `CEST`, and `clock Germany` now works at all, along
  with every other name in the tz database's `iso3166.tab` -- an `&` may be
  written `and`. A country that spans zones still asks you to pick, in
  whichever spelling you typed: `United States spans 8 time zones`. Adding the
  label changes what an existing command line draws wherever a country code is
  on it.
- **A state can also be its ISO 3166-2 code**, `US-CA`, labelled with the full
  name it stands for: `PDT (California)`. The bare two letters are not taken
  and cannot be -- 32 of the 56 already mean something else here, and 26 of
  those a different clock, `CA` being Canada -- so a short form that worked for
  some states and silently drew another country for others is the thing this
  avoids.
- difftest's table parity compared only rows whose name and zone fit the
  character classes it spelled out, so a row holding a hyphen or a space --
  `America/Port-au-Prince`, or a code's full name -- was dropped from both
  sides at once and never compared, while the check still said the tables
  match. It takes every row of two quoted strings now, the key list included.
- `tools/placecheck.py` checks the codes too, describes what it checks
  accurately, and reports a state row with no capital recorded rather than
  crashing on it.
- **A city off the end of an IANA name is labelled the same way**, so
  `clock Berlin` reads `CEST (Berlin)` where it used to read `CEST`. That
  changes what an existing command line draws, which by this file's own rule
  makes the next release a minor one.
- A space in a place's name does the work of its underscore: `"New York"` is
  `New_York`. A list of places too long for its cell ends `...)` instead of
  stopping partway through a name.
- `tools/placecheck.py` runs every state and city through the real resolver,
  and fails on a row that does not load, cannot be reached, or is out of order;
  `--geonames` re-checks every zone against GeoNames. difftest's table parity
  matched only upper-case names, so until now it covered the alias tables and
  nothing else.
- **`tools/errcover.py` runs again.** The ziptz split removed the lines in
  `tools/difftest.sh` that ran it, together with the summary and exit status
  0.2.1 put back -- and put back without it. So since then nothing has checked
  that every error message the clock can print is printed by some case, while
  the README said difftest does. It is back, and passes.
- The README's zone table said it listed the forms in the order they are tried,
  and did not: `local` and ZIP codes are tried first, and a city off the end of
  an IANA name after country codes. It does now.
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
