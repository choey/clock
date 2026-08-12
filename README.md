# clock

A terminal clock: one analog face per time zone, its name and digital readout
underneath, wrapped into a grid. Two independent implementations — Python and
Go — that render byte-for-byte identical output.

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
          EDT
     11:07:23.400
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
| `PST` `PDT` `EDT` `CST` `CDT` `MDT` `AKST` `AKDT` `HDT` | that exact offset | a fixed clock that never shifts |
| `JP` `GB` `DE` | that country's zone | 2-letter ISO code, via `zone.tab` |
| `94110` `941` | the zone that ZIP is in | US only |
| `local` | your system zone | |

The abbreviations only fill gaps the tz database leaves, which is why `EST` and
`ET` are different clocks and both are right: **`EST` is the fixed −05:00 zone
that never shifts**, while **`ET` is the eastern US, which does**. Same for
`MST` vs `MT` and `HST` vs `HT`. `GMT` and `CET` are likewise left alone —
aliasing `GMT` to `Europe/London` would make it read `BST` every July.

`PST`, `PDT`, `EDT` and the rest name an offset rather than a place — nowhere
is on `PDT` in January — so the tz database has nothing to look up. They become
fixed-offset clocks instead, which is exactly what the names mean. Put a pair
side by side in July and the difference shows:

```
$ clock PST,PT,PDT
          PST                     PDT/PT
     12:00:00.000              13:00:00.000
```

`PST` stays on −08:00 while `PT` has moved to `PDT` — and `PT` and `PDT`, being
the same clock in July, have merged into one face. In January they separate
again, with `PT` back on `PST`.

Two of these carry a judgement call. `CST` is the US Central reading, −06:00,
not China — for China use `CN` or `Asia/Shanghai`. And `HDT` is −09:00, the
Aleutian daylight zone, since Hawaii itself never leaves `HST`.

A country that genuinely spans zones asks you to pick:

```
$ clock US
clock: US spans 8 time zones; name one: America/New_York, America/Chicago, ...
```

Countries whose zones merely agree — Germany lists both `Europe/Berlin` and the
`Europe/Busingen` enclave — collapse to one and resolve without complaint.

### Duplicates

Two zones showing the same wall clock are one face, whatever you called them:
`PDT,PDT`, `UK,BST` and `ET,America/New_York` each draw once. The face is
labelled with the abbreviation, and when more than one spelling collapsed onto
it, with those spellings too — `UK,BST` reads `BST/UK`, while `PDT,PDT` was
never ambiguous and stays plain `PDT`.

Whether two zones agree is a property of the instant, not of the zones, so the
grouping is redone every frame. `PT` and `PDT` are one face in July and two in
January, and a clock left running across the boundary splits itself as it
happens.

### ZIP code accuracy

Give all five digits and the answer is exact. The 3-digit prefix table stores
one zone per prefix, decided by majority of the ZIP codes under it, so a prefix
straddling a zone boundary rounds to whichever side holds more — 30 prefixes do,
covering 233 ZIP codes that the majority gets wrong. Those 233 are named
individually in a second table, which a 5-digit lookup consults first:

```
$ clock 79835        # Canutillo, TX — El Paso County
   MDT               # right: the exception table names it
$ clock 798          # the prefix alone
   CDT               # the majority, which is Van Horn's side of the line
```

Verified against the source: of 33,791 ZIP codes, 233 (0.69%) resolve wrongly
from the prefix alone, and **none** resolve wrongly from all five digits.

The worst case a prefix gets wrong is `96799`, American Samoa, an hour behind
Honolulu; `86504` and `865` differ only in summer, since the Navajo Nation
observes daylight saving where the rest of Arizona does not.

One gap remains. The tables come from Census ZCTA centroids, which exist only
for ZIP codes with a delivery area — PO-box and single-building ZIPs have none,
so they are absent from both tables and fall back to their prefix's majority.
Those are the only 5-digit ZIPs that can still come out wrong.

`tools/genzips.py` regenerates both tables and prints every straddling prefix
with its vote breakdown.

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
call. On a terminal the clock takes the alternate screen (`ESC[?1049h`) and
paints each frame from its top corner with `ESC[H`; `ESC[K` on each row clears
a longer previous line and `ESC[J` at the end clears a taller previous frame,
so the grid reshapes cleanly when the window changes. Keystrokes are swallowed
(cbreak, `ISIG` left on) so a stray Return can't scroll the frame.

Painting from a fixed corner rather than rewinding with `ESC[<n>A` is what
makes a resize safe. A rewind counts the rows *written*, assuming each occupies
one physical line — but narrow the window and the terminal rewraps the frame
already on screen, so those rows become two lines each, the rewind lands inside
the old frame, and its upper half is left behind smeared into the new one.
`ESC[J` only ever clears downwards, so it cannot mop that up. Owning a screen
makes the frame's position independent of whatever happened to the last one.

Piped output has no resize to survive and keeps the rewind, which is also what
lets `tools/difftest.sh` compare the two implementations byte for byte.

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
height = rows * (ROWS + 2) + (rows - 1)
```

so one clock is 23x13, three across is 75x13, four zones at `-n 2` is 49x27,
and five zones at `-n 2` is 49x41.

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

Because the clock runs on the alternate screen, quitting restores whatever was
on screen before it and the last frame does not linger — the same as `less` or
`vim`. Redirect to a file to keep a frame.

A `kill -9` skips the terminal restore and leaves echo off and the alternate
screen active; `stty sane` and `printf '\033[?1049l'` fix it.

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

The Census archive it downloads is cached in `tools/cache/` (gitignored) and
reused on every later run, so only the first regeneration touches the network.
Pass a path to read a local copy instead.

### When to regenerate

Almost never, and *not* for daylight-saving changes. The tables store zone
names, not offsets or rules, so the answer to "is Denver on MDT today" comes
from whatever tzdata the machine running the clock has. A state dropping
daylight saving, or the country abolishing the switch, arrives with an OS
update and needs nothing here.

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
zipZones/ZIP_ZONES in both clocks.
```

The ZIP data derives from US Census ZCTA Gazetteer centroids (a US Government
work, public domain) resolved through
[timezonefinder](https://github.com/jannikmi/timezonefinder), whose boundaries
come from timezone-boundary-builder (ODbL).
