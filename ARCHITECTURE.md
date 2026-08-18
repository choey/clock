# Architecture

Notes for anyone hacking on the implementation, not needed just to run
`clock` — see [README.md](README.md) for that.

## Rendering

**Braille canvas.** Each terminal cell carries a 2x4 grid of braille dots
(U+2800 and up), giving the faces roughly 4x the resolution of the character
grid. Hour numerals are overlaid as real characters instead — a cell holds
braille or text, never both, so a numeral hides whatever dots share its cell.

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
finished frame as plain text, after the grid is drawn — never mixed into a
face's own rows. Colour is turned off for any frame a modal is going onto, so
the overlay never has to reason about resuming a hand's ANSI colour on the far
side of the box it just drew over.

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
