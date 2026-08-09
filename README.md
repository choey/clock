# clock

A terminal clock: analog faces for UTC and local time, digital readout
underneath. Two independent implementations — Python and Go — that render
byte-for-byte identical output.

```
⠀⠀⠀⠀⠀⣠⡤⠒⠊⠉⠉⢹⡏⠉⠉⠑⠒⢤⣄⠀⠀⠀⠀⠀
⠀⠀⢀⡴⠋⠀⠈⠀⠀⠀⠀12⠀⠀⠀⠀⠁⠀⠙⢦⡀⠀⠀
⠀⣠⡋⠀⠀⠀⠀⠀⠀⠀⠀⢸⡇⠀⠀⠀⠀⠀⠀⠀⠀⢙⣄⠀
⣰⠁⠈⠀⠀⠀⠀⠀⠀⠀⠀⢸⡇⠀⠀⠀⠀⠀⠀⠀⠀⠁⠈⣆
⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸
⡷⠶⠆09⠀⠀⠀⠀⠀⠀⠸⠷⠶⠶⠶⠶⠶⠆03⠰⠶⢾
⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸
⠹⡀⢀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⡀⢀⠏
⠀⠙⣅⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣨⠋⠀
⠀⠀⠈⠳⣄⠀⢀⠀⠀⠀⠀06⠀⠀⠀⠀⡀⠀⣠⠞⠁⠀⠀
⠀⠀⠀⠀⠀⠉⠓⠤⢄⣀⣀⣸⣇⣀⣀⡠⠤⠚⠋⠀⠀⠀⠀⠀
    UTC: 03:00:00.000
```

## Running

```sh
python3 clock.py
go run clock.go
```

Press `q` (or Ctrl+C) to quit.

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

**One instant per frame.** Each frame samples the clock once and derives the
other zone from it, so the two faces can never disagree by a millisecond at a
rollover.

**Repaint.** The whole frame is built as a single string and written in one
call, then the next frame rewinds over it with `ESC[<n>A`. Keystrokes are
swallowed (cbreak, `ISIG` left on) so a stray Return can't scroll the frame
out from under the rewind.

## Tuning

`CLOCK_CELL_RATIO` is your font's cell height / width, and is the only knob
that decides whether the face is round. Braille dots are square at exactly 2;
most fonts sit near 2.2, which is the default. Raise it if the face looks
squished, lower it if it bulges sideways.

```sh
CLOCK_CELL_RATIO=2.6 python3 clock.py
```

`ROWS`/`rowsN` sets the face height in terminal rows; the width follows from
the cell ratio.

## Terminal requirements

A font with braille coverage, and a window at least as wide as one frame
(51 columns at the default size). Narrower and lines wrap, which desyncs the
rewind and garbles the display — there is no width check. A `kill -9` skips
the terminal restore and leaves echo off; `stty sane` fixes it.
