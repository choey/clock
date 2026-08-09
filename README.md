# clock

A terminal clock: one analog face per time zone, digital readout underneath,
wrapped into a grid. Two independent implementations — Python and Go — that
render byte-for-byte identical output.

```
⠀⠀⠀⠀⢀⣠⡴⠒⠋⠉⠉⣿⠉⠉⠙⠒⢦⣄⡀⠀⠀⠀⠀
⠀⠀⢀⡴⠋⠀⠁⠀⠀⠀⠀12⠀⠀⠀⠈⠀⠙⢦⡀⠀⠀
⠀⣰⡋⠀⠀⠀⠀⠀⠀⡀⠀⠀⠀⠀⠀⠀⡠⡢⠀⠀⢙⣆⠀
⣰⠁⠈⠀⠀⠀⠀⠀⠘⡵⡀⠀⠀⠀⡠⡪⠊⠀⠀⠀⠁⠈⣆
⡇⠀⠀⠀⠀⠀⠀⠀⠀⠈⢷⡀⡠⡪⠊⠀⠀⠀⠀⠀⠀⠀⢸
⡷⠶09⠀⠀⠀⠀⠀⠀⠈⠿⡊⠀⠀⠀⠀⠀⠀03⠶⢾
⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠢⡀⠀⠀⠀⠀⠀⠀⠀⢸
⠹⡀⢀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⢄⠀⠀⠀⠀⡀⢀⠏
⠀⠱⡅⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠑⢄⠀⠀⢨⠎⠀
⠀⠀⠈⠢⣄⠀⡀⠀⠀⠀⠀06⠀⠀⠀⢀⠈⣠⠔⠁⠀⠀
⠀⠀⠀⠀⠀⠙⠳⠤⣀⣀⣀⣿⣀⣀⣀⠤⠞⠋⠀⠀⠀⠀⠀
   EDT: 11:07:23.400
```

## Running

```sh
python3 clock.py
go run .
```

Press `q` (or Ctrl+C) to quit.

Go needs `go run .`, not `go run clock.go`: the three termios ioctl requests
are the one thing that differs between the BSDs and Linux, so they live in
build-tagged `term_*.go` files, and naming a single file skips them.

## Usage

```
clock [-n N | --per-row N] [ZONES]
```

With no arguments you get one clock, in your local zone. Otherwise `ZONES` is a
comma-separated list, and `-n` caps how many sit side by side before the grid
wraps to a new row (default 3).

Flags may go before or after the zone list — all four of these are the same
command:

```sh
clock ET,PT,UTC -n 2
clock -n 2 ET,PT,UTC
clock ET,PT,UTC --per-row 2
clock --per-row 2 ET,PT,UTC
```

## Zone names

Resolved in this order, first match winning:

| You type | You get | |
|---|---|---|
| `ET` `CT` `MT` `PT` | `America/New_York` and friends | follows daylight saving, so it reads `EST` in winter and `EDT` in summer |
| `AKT` `HT` `BST` `UK` `IST` `JST` `KST` `SGT` `HKT` `AET` `ACT` `AWT` `NZT` | the obvious place | same |
| `Europe/Berlin` `UTC` `EST` `MST` `HST` `GMT` `CET` `Etc/GMT+5` | itself | any name the tz database knows |
| `JP` `GB` `DE` | that country's zone | 2-letter ISO code, via `zone.tab` |
| `94110` `941` | the zone that ZIP is in | US only |
| `local` | your system zone | |

The abbreviations only fill gaps the tz database leaves, which is why `EST` and
`ET` are different clocks and both are right: **`EST` is the fixed −05:00 zone
that never shifts**, while **`ET` is the eastern US, which does**. Same for
`MST` vs `MT` and `HST` vs `HT`. `GMT` and `CET` are likewise left alone —
aliasing `GMT` to `Europe/London` would make it read `BST` every July.

`EDT`, `PST`, `PDT`, `CST` and the rest name an offset rather than a place, so
they are not zones at all; the clock says so and points at the regional form.

A country that genuinely spans zones asks you to pick:

```
$ clock US
clock: US spans 8 time zones; name one: America/New_York, America/Chicago, ...
```

Countries whose zones merely agree — Germany lists both `Europe/Berlin` and the
`Europe/Busingen` enclave — collapse to one and resolve without complaint.

### ZIP code accuracy

The table stores one zone per 3-digit prefix, decided by majority of the ZIP
codes under it, so a prefix split across a zone boundary rounds to whichever
side holds more of them. Thirty prefixes are split; the ones most likely to
bite:

- `798` — El Paso is Mountain, but the rest of the prefix is Central and wins
  by a single ZIP code. Use `MT` or `79901`.
- `860` `865` — the Navajo Nation observes daylight saving and the rest of
  Arizona does not; both prefixes round to their majority.
- `967` — `96799`, American Samoa, resolves to Honolulu rather than Pago Pago.
- `373` — Tennessee's Central/Eastern line, decided 39 votes to 38.
- `324` `401` `426` `427` `465` `475` `479` `498` `499` `575`–`588` `677`–`692`
  `835` `979` `995` — other boundary prefixes.

`tools/genzips.py` prints the full list every time it regenerates the table.

## How it works

**Braille canvas.** Each terminal cell carries a 2x4 grid of braille dots
(U+2800 and up), giving the faces roughly 4x the resolution of the character
grid. Hour numerals are overlaid as real characters instead — a cell holds
braille or text, never both, so a numeral hides whatever dots share its cell.

**19ms refresh, not 20.** 19 is coprime to 10, so the millisecond ones digit
cycles through all ten values. At a flat 20ms it never moves at all, and 25ms
would only ever show 0 and 5.

**Smooth sweep.** The second hand takes fractional seconds, so it glides
rather than stepping once a second.

**One instant per frame.** Each frame samples the clock once and converts that
single instant into every zone on screen, so no two faces can disagree by a
millisecond at a rollover.

**Snapped coordinates.** Dot positions are quantised to 1e-9 before they are
rounded onto the grid. Go computes sin/cos in software while Python calls the
platform libm; they agree to well under an ulp, but not bit for bit, and
without the snap a dot sitting near a half-dot boundary lands in different
cells in the two renders.

**Repaint.** The whole frame is built as a single string and written in one
call, then the next frame rewinds over it with `ESC[<n>A`. `ESC[K` on each row
clears a longer previous line and `ESC[J` at the end clears a taller previous
frame, so the grid reshapes cleanly when the window is resized. Keystrokes are
swallowed (cbreak, `ISIG` left on) so a stray Return can't scroll the frame
out from under the rewind.

## Tuning

`CLOCK_CELL_RATIO` is your font's cell height / width, and is the only knob
that decides whether the face is round. Braille dots are square at exactly 2;
most fonts sit near 2.1, which is the default. Raise it if the face looks
squished, lower it if it bulges sideways.

```sh
CLOCK_CELL_RATIO=2.6 python3 clock.py
```

`ROWS`/`rowsN` sets the face height in terminal rows; the width follows from
the cell ratio, as `floor(ROWS * CELL_RATIO + 0.5)` — 23 columns at the
defaults. A whole frame is then

```
width  = perRow * COLS + (perRow - 1) * GAP
height = rows * (ROWS + 1) + (rows - 1)
```

so one clock is 23x12, three across is 75x12, four zones at `-n 2` is 49x25,
and five zones at `-n 2` is 49x38.

## Terminal requirements

macOS and Linux. Windows is not supported — it has no termios and no `select`
on stdin — and both implementations say so and exit rather than failing
obscurely; use WSL.

You need a font with braille coverage, and a window big enough for one face
(23 columns at the default size). The clock measures the terminal every frame
and quietly lowers `--per-row` to whatever fits, so a narrow window rewraps
instead of garbling. If not even one face fits, or the grid is taller than the
window, it says which and stops — note that a too-tall grid is fixed by
*raising* `--per-row`, the opposite of a too-narrow one. When it can't measure
at all, e.g. piped to a file, it renders exactly what you asked for.

A `kill -9` skips the terminal restore and leaves echo off; `stty sane` fixes
it.

## Development

`tools/difftest.sh` proves the two implementations agree. It pins both to a
fixed instant with `CLOCK_FREEZE` (an undocumented dev hook: an instant like
`2026-07-15T09:53:07.123456Z`, which draws exactly one frame and exits) and to
a fixed window with `COLUMNS`/`LINES`, then compares stdout, stderr and exit
status across a few hundred argument lists, terminal sizes and cell ratios. It
also cross-compiles for Linux, macOS and Windows, and diffs the generated
tables out of the two files.

```sh
tools/difftest.sh -v
```

`tools/genzips.py` regenerates the ZIP table, writing `clock.go` and `clock.py`
in the same pass — which is what keeps the two literals from drifting. Never
edit them by hand.

```sh
pip install timezonefinder
tools/genzips.py
```

The ZIP data derives from US Census ZCTA Gazetteer centroids (a US Government
work, public domain) resolved through
[timezonefinder](https://github.com/jannikmi/timezonefinder), whose boundaries
come from timezone-boundary-builder (ODbL).
