# Architecture

Notes for anyone hacking on the implementation, not needed just to run
`clock` — see [README.md](README.md) for that.

## Rendering

**Braille canvas.** Each terminal cell carries a 2x4 grid of braille dots
(U+2800 and up), giving the faces roughly 4x the resolution of the character
grid. Hour numerals are overlaid as real characters instead — a cell holds
braille or text, never both, so a numeral hides whatever dots share its cell.

**Axis-safe major ticks.** The four major hour ticks (0, 3, 6, 9 — the ones
with a numeral beside them) are drawn two dots thick, exactly axis-aligned.
The obvious way to draw a thick spoke is a symmetric ±0.5-dot offset
straddling its centreline, and that is what every other thick spoke
(the hour and minute hands) uses. For a major tick specifically that is a
mistake: the centreline sits at a cell's exact half-integer coordinate, so
the two ±0.5 dots always land one on each side of it — and whether that
puts them in the *same* character row (or column) or splits them into two
depends on the face's size, specifically on `rowsN`/`colsN`'s parity. Split,
the tick reads as disconnected from the numeral beside it — something the
single fixed default size (11 rows, odd, never split) this project shipped
with for a long time happened to never trigger. `--scale auto` picking
arbitrary sizes made it visible. The fix keeps both dots in the same cell as
the numeral's own dot —
the centre dot, plus whichever neighbour doesn't cross a cell boundary —
rather than straddling symmetrically; the tick moves by at most half a dot
of true centring, invisible, in exchange for never looking disjointed. See
`spoke()`'s comments in clock.go/clock.py for the exact rule.

While fixing that, a second bug turned up in the same function: giving a
thick, non-tapered spoke (any tick) an offset endpoint at one end and a
bare, un-offset endpoint at the other — a leftover from how hand-tapering
was bolted on — made every tick a wedge narrowing to a point at the rim
instead of a flat-ended band. Invisible in practice, since the rim's own
dot circle overdraws the last fraction of it, but fixed alongside the
alignment issue since both live in the same few lines.

**Layered hands.** Every dot remembers which hand put it there, and a cell
takes the colour of the topmost hand with a dot in it — shortest hand on top,
the reverse of the order they are drawn in. Escapes go per run of same-coloured
cells rather than per cell, and each row ends back on the default foreground,
so the gutters between faces stay uncoloured and nothing leaks past the frame.

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

**Modals.** The key list and the startup quit hint are stamped onto the
finished, already-coloured frame after the grid is drawn — never mixed into a
face's own rows. The splice is ANSI-aware: it walks each row it touches,
tracks the last colour escape seen, and drops in the box's plain text between
a reset and whatever colour was active on the far side, so a hand's colour
survives everywhere outside the box itself, including where the box actually
lands mid-hand.

## Auto scale

`--scale auto`, the default, re-solves `ROWS` (and `COLS`, through it) every
frame by searching candidate sizes from `MAX_ROWS_N` down to `MIN_ROWS_N` and
taking the first one where `fit_per_row` and `fit_height` both still succeed.
That search relies on a monotonicity argument rather than trying every
`(ROWS, per_row)` combination:

- `fit_per_row` already returns the *largest* `per_row` that fits a given
  `COLS`, so for a fixed `ROWS` there is nothing better to try.
- As `ROWS` grows, `COLS` grows with it (through the cell-ratio formula), so
  `fit_per_row`'s own max-fit can only fall or hold, never rise -- meaning the
  `per_row` a candidate settles on is non-increasing in `ROWS`.
- Fewer faces per row means the same face count needs as many or more rows of
  clocks, and each of those rows is itself taller (`ROWS` grew) -- so the
  total height a candidate needs is non-decreasing in `ROWS`.

So whether a given `ROWS` fits is true for small values and, once it turns
false, stays false for every larger one: a single descending scan finds the
true largest fit, without needing to search `per_row` separately. Both
`fit_per_row`/`fit_height` read `COLS`/`ROWS` as module state rather than
taking them as arguments, so the search mutates them directly per candidate
and leaves the winner in place for the ordinary per-frame layout code -- run
right after it -- to pick up.

**`-n auto`.** This is also why `-n auto` needs no search of its own: it
just passes `num_faces` as `want_per_row` -- no real cap, since `fit_per_row`
already clamps `want` to `num_faces` -- and lets the existing scan above do
the rest. The first bullet is the reason that works: at any candidate `ROWS`,
`fit_per_row` already settles on the per-row count that minimises the chunks
(and so the height) that `ROWS` needs, so there is never a smaller cap that
would have let a *larger* `ROWS` fit -- only ever one that forces more chunks
than necessary. A first attempt at this search per-row counts explicitly, one
`autoScale` pass per count from `num_faces` down to 1, taking whichever pair
gave the largest `ROWS`; it gave the same answer every time, because it was
solving a problem the existing search already solved by construction.

**Preferring an odd size.** Once a candidate fits, the search does not take
it immediately -- it looks up to `SYMMETRY_WINDOW` rows smaller for one where
both `ROWS` and `COLS` are odd, and only falls back to the largest fit itself
if none turns up. This is not cosmetic. `cy` and `cx` (the face's true
centre, in dot coordinates) are always exactly `K + 0.5` for some integer
`K` -- see the axis-safe tick comment on `spoke()`. An *odd* `ROWS` (or
`COLS`) puts that centre at the exact middle of a character cell; an *even*
one puts it exactly on the boundary between two cells, where the major
3/9 o'clock (or 12/6 o'clock) tick's two dots cannot be placed symmetrically
inside a single cell no matter what `spoke()` does -- it can only pick the
closest same-cell pair, which ends up pressed against one edge of the cell
rather than centred. That asymmetry is small -- under a dot -- and invisible
in terminals that draw a text glyph and a braille glyph the same way. It
is not invisible everywhere: Ghostty synthesises braille glyphs itself,
independent of the font, to guarantee they tile edge-to-edge, while the hour
numerals still go through the font's own glyph metrics -- and depending on
the font, those two placement systems can disagree by enough that the
edge-pressed tick reads as visibly detached from its numeral, while the
centred (odd) case does not. Reported and diagnosed against a live Ghostty
session: `--scale` values landing on an even `rowsN` looked wrong, odd ones
did not, with zero exceptions across five tested values.

## ZIP resolution

See [ZIP code accuracy](README.md#zip-code-accuracy) in the README for what
this guarantees; this is how it's built. A lookup tries an exact 5-digit match
first and falls back to the 3-digit prefix, and both come from two tables
`tools/genzips.py` generates into `clock.go` and `clock.py` in the same pass —
never edit them by hand, or the two ports drift.

**`zipRuns`** is the prefix table: fixed 4-byte records, `"NNNc"` — the
3-digit prefix a run starts at, then a zone letter. A run reaches to the next
record's prefix, the last one to 999, and `-` marks a prefix the Postal
Service hasn't assigned. Zero-padded 3-digit decimals sort lexicographically
the same way they sort numerically, so the lookup is a binary search over the
raw string, no integer parsing needed on either side of the port.

**`zipExceptions`** is the 233 ZIPs the prefix table gets wrong, consulted
first when all five digits are given (three digits alone can't say which ZIP
is meant). Records are `"PPPcNN"` — prefix, zone letter, then a count of how
many 2-digit suffixes follow — followed by that many suffixes, ascending:

```
373C38 01 02 07 ...        (spaces for clarity only)
```

Writing the prefix once per group rather than once per ZIP is what keeps this
smaller than a flat table of 5-digit records. The stride varies group to
group, so this is a linear forward scan rather than a binary search — there
are only about thirty groups, and it runs once per zone at startup, never per
frame.

**`zipZones`** maps each single-letter code used in both tables to one real
IANA zone. That folding — many actual zones collapsing onto one representative
letter because they currently agree — is `genzips.py`'s `CANONICAL` table, and
is exactly what can go stale if a zone's rules diverge from its letter's; see
[When to regenerate](README.md#when-to-regenerate) for that.

Both tables derive from US Census ZCTA Gazetteer centroids (public domain),
resolved through [timezonefinder](https://github.com/jannikmi/timezonefinder),
whose boundaries come from timezone-boundary-builder (ODbL).
