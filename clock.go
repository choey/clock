//go:build !windows

// Live terminal clock: one analog face per time zone, digital underneath.
//
// Faces are drawn on a braille canvas (2x4 dots per cell). Refreshes every 19ms,
// so the second hand sweeps smoothly rather than stepping. Press q (or Ctrl+C)
// to quit.
package main

import (
	"errors"
	"fmt"
	"math"
	"os"
	"os/signal"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"
	"unsafe"
)

const (
	hideCursor = "\x1b[?25l"
	showCursor = "\x1b[?25h"
	clearEOL   = "\x1b[K"
	clearBelow = "\x1b[J"
	home       = "\x1b[H"
	enterAlt   = "\x1b[?1049h"
	leaveAlt   = "\x1b[?1049l"

	// 19ms, not 20: coprime to 10, so the millisecond ones digit cycles through
	// all ten values instead of sitting still. Reads as a live clock.
	tick = 19 * time.Millisecond

	defaultRowsN     = 11   // face height, in terminal rows, at --scale 1
	defaultCellRatio = 2.1  // cell height / width; braille dots are square at 2
	gap              = 3    // fewest blank columns between adjacent faces
	vgap             = 1    // fewest blank rows between rows of faces
	markerR          = 0.70 // numeral distance from the centre, as a fraction of the radius
	handTaper        = 0.15 // fraction of a thick hand's length that narrows to a point at the tip

	defaultPerRow = 3  // faces per row before wrapping
	maxPerRow     = 64 // an upper bound so a typo can't ask for a million faces

	// minRowsN keeps a face big enough for the hour numerals to have somewhere
	// to sit; maxRowsN is just a guard against a typo asking for a giant canvas.
	minRowsN = 4
	maxRowsN = 200
)

const usage = `clock - analog terminal clocks

usage: clock [-n N | --per-row N] [--color[=WHEN]] [--day[=WHEN]] [-q | --quiet]
             [--halign WHERE] [--valign WHERE] [--hpad SPACE] [--vpad SPACE]
             [--cell-ratio N] [--scale N] [ZONES]

  ZONES              comma-separated zone names, described below; default is
                      your local zone
  -n, --per-row N    clocks per row before wrapping, reduced to fit; auto
                      (default) picks whatever grows --scale auto the most
  --color[=WHEN]     colour the hands: always, auto (default), never or off
  --no-color         same as --color=never
  --day[=WHEN]       weekday on the readout: always, auto (default), never
  --no-day           same as --day=never
  -q, --quiet        skip the "press q to quit" hint shown at startup
  --halign WHERE     the grid across the window: left, center (default), right
  --valign WHERE     the grid down the window: top, center (default), bottom
  --hpad SPACE       between clocks: even (default), or a share of the width
                      like 10%
  --vpad SPACE       between rows: even (default), or a share of the height
                      like 5%
  --cell-ratio N     font cell height / width (default 2.1); raise if the
                      face looks squished, lower if it bulges sideways
  --scale N          resize every face by this factor; auto (default) fills
                      the window, at minimum padding
  -h, --help         this message

A zone is an IANA name (Europe/Berlin), the city off the end of one where
that is unambiguous (Berlin, Jakarta), a regional abbreviation (ET CT MT PT
AKT HT BST IST JST AET ...), a 2-letter country code (JP, GB), or a US ZIP
code (94110). ET/CT/MT/PT follow daylight saving, so they read EST or EDT
depending on the date; EST/EDT/PST/PDT and the rest are the fixed offsets,
which never shift.

The hands are coloured on a terminal and plain when redirected; NO_COLOR
turns the colour off everywhere. Auto puts a weekday on the readouts only
when the clocks on screen disagree about the date. An even fill spreads the
clocks over the whole window; --hpad 10% sets the gaps instead, as a share of
the window, and then the alignment decides where the grid sits. CLOCK_CELL_RATIO
sets the same thing as --cell-ratio, for when it wants to be set once per
terminal rather than typed every time; the flag wins if both are given.

Space holds the frame still, for a screenshot, and h or ? opens the key list.
Press q or Ctrl+C to quit.

examples:
  clock
  clock ET,PT,UTC
  clock -n 2 ET,PT,UTC
  clock Berlin,Jakarta
  clock Europe/Berlin,Asia/Tokyo,94110 --per-row 2
`

// haligns and valigns are where the grid can sit when it does not fill the
// window, and what --halign and --valign accept. Same order as clock.py's
// tables, and the wording of the error they raise comes off these lists.
var (
	whens = []string{"always", "auto", "never"}
	// colorWhens takes "off" too, alongside "never": both disable colour,
	// but "off" is the more obvious word for it.
	colorWhens = []string{"always", "auto", "never", "off"}
	haligns    = []string{"left", "center", "right"}
	valigns    = []string{"top", "center", "bottom"}
)

// needs is the example each of the four layout flags gives when handed no
// value at all.
var needs = map[string]string{
	"halign":     "--halign center",
	"valign":     "--valign center",
	"hpad":       "--hpad 10%",
	"vpad":       "--vpad 5%",
	"cell-ratio": "--cell-ratio 2.6",
	"scale":      "--scale 1.5",
}

// hotkeys is the key list h or ? puts up in a modal. In the order the keys
// are reached for rather than alphabetically, and kept in the same order as
// clock.py's table.
var hotkeys = []struct{ key, what string }{
	{"space", "hold the frame"},
	{"h ?", "toggle this list"},
	{"q", "quit, or Ctrl+C"},
}

// hotkeyCol is where the descriptions start, so the keys get a gutter.
const hotkeyCol = 8

// flashText is said once, in the same modal the key list uses, and then
// dropped: a clock that has taken the whole screen owes the reader a way back
// out, but only until it is read. -q/--quiet skips it outright.
const (
	flashText = "Press q or Ctrl+C to quit"
	flashFor  = 3 * time.Second
)

// cellRatio is how tall a terminal cell is relative to its width. It is the
// only knob that decides whether the face is round, and it varies by font and
// line spacing. Set for real in run(), from --cell-ratio or CLOCK_CELL_RATIO;
// nothing reads it before then, so the default here is just a placeholder.
// Raise it if the face looks squished, lower it if it bulges sideways.
var cellRatio = defaultCellRatio

// rowsN is the face height in terminal rows, set for real in run() from
// --scale; nothing reads it before then.
var rowsN = defaultRowsN

// colsN is the face width in terminal columns, set alongside rowsN and cellRatio.
var colsN = int(math.Floor(defaultRowsN*defaultCellRatio + 0.5))

// envCellRatio reads CLOCK_CELL_RATIO, falling back to the default on
// anything unusable. Unlike --cell-ratio, an environment variable might be
// stale or set for some other program, so a bad value is not a user error --
// it is simply ignored, the same way an unset one is.
func envCellRatio() float64 {
	v, err := strconv.ParseFloat(os.Getenv("CLOCK_CELL_RATIO"), 64)
	if err != nil || math.IsNaN(v) || math.IsInf(v, 0) || v <= 0 {
		return defaultCellRatio
	}
	return v
}

// freezeLayout is the one instant format CLOCK_FREEZE accepts. Exactly six
// fractional digits, exactly UTC: Python's datetime stops at microseconds, and
// pinning the format keeps both implementations rejecting the same strings.
const freezeLayout = "2006-01-02T15:04:05.000000Z"

// freeze pins the clock to a fixed instant: the hands never move, and space
// has nothing to hold back. Redirected it draws that one frame and exits,
// which is what lets the Go and Python renders be diffed byte for byte; on a
// terminal it stays up, with h and q still live, so the frame can be looked at
// and photographed. Dev hook, not in --help.
func freeze() (time.Time, bool, error) {
	v := os.Getenv("CLOCK_FREEZE")
	if v == "" {
		return time.Time{}, false, nil
	}
	t, err := time.Parse(freezeLayout, v)
	// Insist on the canonical spelling, not merely a parseable one. Both
	// parsers are loose in their own directions -- Go takes a one-digit month,
	// Python takes fewer than six fractional digits -- and the round trip is
	// the one cheap check that pins them to the same set of strings.
	if err != nil || t.Format(freezeLayout) != v {
		return time.Time{}, false, fmt.Errorf(
			"CLOCK_FREEZE wants an instant like 2026-07-15T09:53:07.123456Z, got \"%s\"", v)
	}
	return t, true, nil
}

// Hour numerals, every one two characters wide. A cell spans 2 dots, so an
// even-width string centres on a cell boundary while an odd-width one centres
// half a cell off it: "12" stacks exactly over "06", but never over "6".
var markers = []struct {
	hour int
	text string
}{{0, "12"}, {3, "03"}, {6, "06"}, {9, "09"}}

// Which hand a cell belongs to, and so which colour it takes. Higher is on
// top: the hands stack shortest-first, the reverse of the order they are drawn
// in, because a longer hand covers a shorter one along its whole length while
// the short one can only ever hide a slice. Left the other way round, the hour
// hand -- the one you most want to find -- vanishes under the minute hand for
// minutes at a time.
const (
	layerNone uint8 = iota
	layerSecond
	layerMinute
	layerHour
)

// handSGR is the foreground code each layer paints with: red second hand as on
// a real dial, then cyan and yellow, which stay legible on a light and a dark
// terminal alike. Plain 8-colour codes, so they follow whatever palette the
// terminal is themed with. defaultFG puts the foreground back and nothing else.
var handSGR = [4]string{"", "\x1b[31m", "\x1b[36m", "\x1b[33m"}

const defaultFG = "\x1b[39m"

// hands, drawn shortest-first -- which is not the order they stack in; see
// the layer constants above
var hands = []struct {
	length float64 // as a fraction of the radius
	thick  bool    // two dots thick?
	layer  uint8   // which colour its cells take
}{{0.50, true, layerHour}, {0.75, true, layerMinute}, {0.88, false, layerSecond}}

// braille dot bit for (x % 2, y % 4); the block starts at U+2800
var dotBits = [2][4]uint8{{0x01, 0x02, 0x04, 0x40}, {0x08, 0x10, 0x20, 0x80}}

// canvas is a dot grid that renders to braille cells, 2 dots wide by 4 tall
// each. Alongside the dots each cell keeps the topmost layer that dotted it,
// which is what the colouring reads. Colour is per cell and dots are not: a
// cell holds up to eight of them, so where two hands share a cell the cell
// takes the upper hand's colour and a few of the lower hand's dots come along.
type canvas struct {
	w, h, cols int
	cells      []uint8
	layers     []uint8
}

func newCanvas(w, h int) *canvas {
	cols := (w + 1) / 2
	return &canvas{
		w: w, h: h, cols: cols,
		cells:  make([]uint8, cols*((h+3)/4)),
		layers: make([]uint8, cols*((h+3)/4)),
	}
}

// snap quantises a dot coordinate to 1e-9 before anything rounds it to a grid
// position. Go computes Sin/Cos in software while Python calls the platform
// libm; the two agree to well under an ulp but not bit for bit, e.g.
// cos(5.562579797474062) is ...96505642 here and ...96494540 there. Unsnapped,
// a dot whose true position sits within 1e-16 of a half-dot boundary rounds
// into different cells in the two renders — which it does, on the rim, every
// single frame. 1e-9 swallows that disagreement and is still far finer than
// the quarter-cell grid it feeds.
func snap(v float64) float64 {
	return math.Floor(v*1e9+0.5) / 1e9
}

func (c *canvas) set(fx, fy float64, layer uint8) {
	// Floor(v + 0.5), not Round(): Round is half-away-from-zero but Python's
	// round() is half-to-even, which would split the two renders apart
	x, y := int(math.Floor(snap(fx)+0.5)), int(math.Floor(snap(fy)+0.5))
	if x < 0 || y < 0 || x >= c.w || y >= c.h {
		return
	}
	c.cells[(y/4)*c.cols+x/2] |= dotBits[x%2][y%4]
	if layer > c.layers[(y/4)*c.cols+x/2] {
		c.layers[(y/4)*c.cols+x/2] = layer
	}
}

func (c *canvas) line(x0, y0, x1, y1 float64, layer uint8) {
	// snap the endpoints too: steps comes off a rounded difference, and a
	// one-ulp wobble there changes the whole dot sequence, not just one dot
	x0, y0, x1, y1 = snap(x0), snap(y0), snap(x1), snap(y1)
	steps := int(math.Floor(math.Max(math.Abs(x1-x0), math.Abs(y1-y0)) + 0.5))
	if steps < 1 {
		steps = 1
	}
	for i := 0; i <= steps; i++ {
		t := float64(i) / float64(steps)
		c.set(x0+(x1-x0)*t, y0+(y1-y0)*t, layer)
	}
}

func (c *canvas) rows() []string {
	out := make([]string, len(c.cells)/c.cols)
	var b strings.Builder
	for r := range out {
		b.Reset()
		for x := 0; x < c.cols; x++ {
			b.WriteRune(rune(0x2800 + int(c.cells[r*c.cols+x])))
		}
		out[r] = b.String()
	}
	return out
}

// colorize wraps each run of same-layer cells in that hand's colour. Runs
// rather than cells: a hand lies along a dozen cells at a stretch, and one
// escape per cell would multiply what a frame writes for no visible
// difference. Rows end back on the default foreground, so the gutter between
// two faces, and whatever the terminal paints past the end of the line, stay
// the colour they were.
func colorize(row string, layers []uint8) string {
	var b strings.Builder
	current := layerNone
	for i, cell := range []rune(row) {
		if layers[i] != current {
			current = layers[i]
			if current == layerNone {
				b.WriteString(defaultFG)
			} else {
				b.WriteString(handSGR[current])
			}
		}
		b.WriteRune(cell)
	}
	if current != layerNone {
		b.WriteString(defaultFG)
	}
	return b.String()
}

// face renders one analog face for t, returning its cell rows. Every face is
// colsN cells wide and rowsN tall.
func face(t time.Time, color bool) []string {
	rx, ry := float64(colsN), float64(2*rowsN)
	// Horizontally the centre sits on a cell boundary, vertically in the middle
	// of a row. The dot grid mirrors about both, which is what makes 09 and 03
	// land the same distance from the rim.
	cx, cy := rx-0.5, ry-0.5
	c := newCanvas(2*colsN, 4*rowsN)

	// spoke draws a radial segment from r0 to r1, as fractions of the radius.
	// A thick spoke drawn with point set narrows over its last handTaper share
	// to a single dot at r1, instead of ending in a flat, two-dot-wide butt.
	spoke := func(angle, r0, r1 float64, thick, point bool, layer uint8) {
		sin, cos := math.Sin(angle), math.Cos(angle)
		x0, y0 := cx+rx*r0*sin, cy-ry*r0*cos
		x1, y1 := cx+rx*r1*sin, cy-ry*r1*cos
		// two dots thick straddles the axis, so the spoke centres on it
		offs := []float64{0}
		if thick {
			offs = []float64{-0.5, 0.5}
		}
		tip := r1
		if thick && point {
			tip = r1 - (r1-r0)*handTaper
		}
		tx, ty := cx+rx*tip*sin, cy-ry*tip*cos
		for _, off := range offs {
			dx, dy := off*cos, off*sin
			// the offset shrinks to nothing at the tip, not the base: that is
			// what tapers the two edges together into a point
			c.line(x0+dx, y0+dy, tx, ty, layer)
		}
		if thick && point {
			c.line(tx, ty, x1, y1, layer)
		}
	}

	// rim: sample densely enough that adjacent dots touch
	steps := int(math.Floor(4*math.Pi*math.Max(rx, ry) + 0.5))
	for i := 0; i < steps; i++ {
		a := 2 * math.Pi * float64(i) / float64(steps)
		c.set(cx+rx*math.Sin(a), cy-ry*math.Cos(a), layerNone)
	}

	// hour ticks, the quarters longer and thicker so they sit on the axes
	for h := 0; h < 12; h++ {
		major := h%3 == 0
		inner := 0.90
		if major {
			inner = 0.80
		}
		spoke(2*math.Pi*float64(h)/12, inner, 1.0, major, false, layerNone)
	}

	// hands: fractional seconds drive the sweep
	frac := float64(t.Nanosecond()) / 1e9
	sec, min, hr := float64(t.Second()), float64(t.Minute()), float64(t.Hour()%12)
	turns := [3]float64{
		(hr + min/60 + sec/3600) / 12,
		(min + (sec+frac)/60) / 60,
		(sec + frac) / 60,
	}
	for i, h := range hands {
		spoke(2*math.Pi*turns[i], 0, h.length, h.thick, true, h.layer)
	}

	rows := c.rows()

	// hour numerals, overlaid as real characters: a cell holds braille or text
	// but never both, so a numeral hides whatever dots share its cell
	for _, mk := range markers {
		a := 2 * math.Pi * float64(mk.hour) / 12
		x := snap(cx + rx*markerR*math.Sin(a))
		y := snap(cy - ry*markerR*math.Cos(a))
		// centre an n-char string on x: it spans 2n dots, so its left edge
		// wants to sit at x - n, snapped to the nearest cell boundary
		col := int(math.Floor((x-float64(len(mk.text))+0.5)/2 + 0.5))
		row := int(math.Floor(y+0.5)) / 4
		if row < 0 || row >= len(rows) || col < 0 || col > c.cols-len(mk.text) {
			continue
		}
		cells := []rune(rows[row])
		copy(cells[col:], []rune(mk.text))
		rows[row] = string(cells)
		// the numeral took the cell's dots with it, so drop their colour
		for i := col; i < col+len(mk.text); i++ {
			c.layers[row*c.cols+i] = layerNone
		}
	}

	if color {
		for r := range rows {
			rows[r] = colorize(rows[r], c.layers[r*c.cols:(r+1)*c.cols])
		}
	}
	return rows
}

// errHelp asks the caller to print the usage text and stop, successfully.
var errHelp = errors.New("help requested")

// parseCount reads a positive whole number, strictly. Not strconv.Atoi, which
// takes a leading "+", and emphatically not Python's int(), which also takes
// surrounding space, underscores and non-ASCII digits: the two parsers have to
// accept exactly the same strings.
func parseCount(s, what string, max int) (int, error) {
	bad := fmt.Errorf("%s wants a whole number from 1 to %d, got \"%s\"", what, max, s)
	if s == "" || len(s) > len(strconv.Itoa(max)) {
		return 0, bad
	}
	n := 0
	for _, c := range []byte(s) {
		if c < '0' || c > '9' {
			return 0, bad
		}
		n = n*10 + int(c-'0')
	}
	if n < 1 || n > max {
		return 0, bad
	}
	return n, nil
}

// parseChoice reads one of a short list of words, or rejects it by name. The
// message is built from the list, so a flag cannot come to accept a word its
// own error text does not offer.
func parseChoice(flag, val string, choices []string) (string, error) {
	for _, c := range choices {
		if val == c {
			return val, nil
		}
	}
	names := strings.Join(choices[:len(choices)-1], ", ") + " or " + choices[len(choices)-1]
	return "", fmt.Errorf("--%s wants %s, got \"%s\"", flag, names, val)
}

// parsePad reads a padding: -1 for the even fill, or a percentage 0-100. Takes
// "10" as readily as "10%", and nothing else -- no sign, no decimal point, no
// space, since clock.py hand-scans the same digits.
func parsePad(flag, val string) (int, error) {
	if val == "even" {
		return -1, nil
	}
	bad := fmt.Errorf("--%s wants even or a share like 10%%, got \"%s\"", flag, val)
	digits := strings.TrimSuffix(val, "%")
	if digits == "" || len(digits) > 3 {
		return 0, bad
	}
	n := 0
	for _, c := range []byte(digits) {
		if c < '0' || c > '9' {
			return 0, bad
		}
		n = n*10 + int(c-'0')
	}
	if n > 100 {
		return 0, bad
	}
	return n, nil
}

// parseRatio reads a positive, finite decimal for --cell-ratio. Delegates to
// strconv.ParseFloat rather than a hand-rolled scan, the same as
// CLOCK_CELL_RATIO already does: this knob shapes one face, not a zone or a
// count, and does not carry the same cross-language byte-for-byte stakes.
func parseRatio(val string) (float64, error) {
	v, ok := positiveFloat(val)
	if !ok {
		return 0, fmt.Errorf("--cell-ratio wants a positive number, e.g. --cell-ratio 2.6, got \"%s\"", val)
	}
	return v, nil
}

// parseScale reads a positive, finite decimal for --scale.
func parseScale(val string) (float64, error) {
	v, ok := positiveFloat(val)
	if !ok {
		return 0, fmt.Errorf("--scale wants auto or a positive number, e.g. --scale 1.5, got \"%s\"", val)
	}
	return v, nil
}

// positiveFloat is what --cell-ratio and --scale share: neither shapes a
// zone or a count, so unlike those this delegates to strconv.ParseFloat
// rather than a hand-rolled scan, and does not carry the same
// cross-language byte-for-byte stakes.
func positiveFloat(val string) (float64, bool) {
	v, err := strconv.ParseFloat(val, 64)
	if err != nil || math.IsNaN(v) || math.IsInf(v, 0) || v <= 0 {
		return 0, false
	}
	return v, true
}

// geometry is what the four layout flags collect: where the grid sits when it
// does not fill the window, and how much space goes between the clocks. A pad
// of -1 is the even fill.
type geometry struct {
	halign, valign string
	hpad, vpad     int
}

// parseArgs reads the command line: one optional zone list, and the flags in
// any position. Hand-rolled rather than package flag, which insists every
// flag precede the first positional -- "clock ET,PT -n 2" would silently
// ignore the -n. clock.py runs the same algorithm for the same reason.
func parseArgs(argv []string) (int, string, string, string, geometry, bool, float64, float64, bool, bool, error) {
	perRow := defaultPerRow // only used when an explicit -n/--per-row overrides perRowAuto below
	colorWhen := "auto"
	dayWhen := "" // unset: run() picks it, since a pinned clock differs
	geo := geometry{halign: "center", valign: "center", hpad: -1, vpad: -1}
	quiet := false
	cellRatioFlag := 0.0 // unset: run() falls back to CLOCK_CELL_RATIO, then the default
	scaleFlag := 0.0     // unset unless a specific --scale overrides scaleAuto below
	scaleAuto := true    // the default: run() re-solves rowsN every frame to fill the window
	perRowAuto := true   // the default: run() also searches per-row counts, to maximise rowsN
	var positional []string
	endOfFlags := false

	for i := 0; i < len(argv); i++ {
		a := argv[i]
		switch {
		case endOfFlags:
			positional = append(positional, a)
		case a == "--":
			endOfFlags = true
		case a == "-h" || a == "--help":
			return 0, "", "", "", geo, false, 0, 0, false, false, errHelp
		case strings.HasPrefix(a, "--"):
			name, val, haveVal := strings.Cut(a[2:], "=")
			switch name {
			case "per-row":
				if !haveVal {
					i++
					if i >= len(argv) {
						return 0, "", "", "", geo, false, 0, 0, false, false, errors.New("--per-row needs a number, e.g. --per-row 2")
					}
					val = argv[i]
				}
				if val == "auto" {
					perRowAuto = true
					break
				}
				n, err := parseCount(val, "--per-row", maxPerRow)
				if err != nil {
					return 0, "", "", "", geo, false, 0, 0, false, false, err
				}
				perRow = n
				perRowAuto = false
			case "color":
				// Bare --color means always, and takes no separate argument:
				// "clock --color ET" names a zone list, exactly as ls and git
				// read the same flag. The value only ever follows an "=".
				colorWhen = "always"
				if haveVal {
					w, err := parseChoice("color", val, colorWhens)
					if err != nil {
						return 0, "", "", "", geo, false, 0, 0, false, false, err
					}
					colorWhen = w
				}
			case "no-color":
				if haveVal {
					return 0, "", "", "", geo, false, 0, 0, false, false, errors.New("--no-color takes no value")
				}
				colorWhen = "never"
			case "day":
				dayWhen = "always"
				if haveVal {
					w, err := parseChoice("day", val, whens)
					if err != nil {
						return 0, "", "", "", geo, false, 0, 0, false, false, err
					}
					dayWhen = w
				}
			case "no-day":
				if haveVal {
					return 0, "", "", "", geo, false, 0, 0, false, false, errors.New("--no-day takes no value")
				}
				dayWhen = "never"
			case "quiet":
				if haveVal {
					return 0, "", "", "", geo, false, 0, 0, false, false, errors.New("--quiet takes no value")
				}
				quiet = true
			case "halign", "valign", "hpad", "vpad", "cell-ratio", "scale":
				// These six want a value, and take it either way round, as
				// --per-row does: there is no bare form to be ambiguous with.
				if !haveVal {
					i++
					if i >= len(argv) {
						return 0, "", "", "", geo, false, 0, 0, false, false, fmt.Errorf(
							"--%s needs a value, e.g. %s", name, needs[name])
					}
					val = argv[i]
				}
				switch name {
				case "halign":
					w, err := parseChoice(name, val, haligns)
					if err != nil {
						return 0, "", "", "", geo, false, 0, 0, false, false, err
					}
					geo.halign = w
				case "valign":
					w, err := parseChoice(name, val, valigns)
					if err != nil {
						return 0, "", "", "", geo, false, 0, 0, false, false, err
					}
					geo.valign = w
				case "cell-ratio":
					r, err := parseRatio(val)
					if err != nil {
						return 0, "", "", "", geo, false, 0, 0, false, false, err
					}
					cellRatioFlag = r
				case "scale":
					if val == "auto" {
						scaleAuto = true
						break
					}
					s, err := parseScale(val)
					if err != nil {
						return 0, "", "", "", geo, false, 0, 0, false, false, err
					}
					scaleFlag = s
					scaleAuto = false
				default:
					n, err := parsePad(name, val)
					if err != nil {
						return 0, "", "", "", geo, false, 0, 0, false, false, err
					}
					if name == "hpad" {
						geo.hpad = n
					} else {
						geo.vpad = n
					}
				}
			default:
				return 0, "", "", "", geo, false, 0, 0, false, false, fmt.Errorf("unknown option: --%s", name)
			}
		case a == "-q":
			quiet = true
		case len(a) > 1 && strings.HasPrefix(a, "-"):
			if a[1] != 'n' {
				return 0, "", "", "", geo, false, 0, 0, false, false, fmt.Errorf("unknown option: %s", a)
			}
			rest := a[2:]
			switch {
			case rest == "":
				i++
				if i >= len(argv) {
					return 0, "", "", "", geo, false, 0, 0, false, false, errors.New("-n needs a number, e.g. -n 2")
				}
				rest = argv[i]
			case rest[0] == '=':
				return 0, "", "", "", geo, false, 0, 0, false, false, errors.New("-n takes its value as \"-n N\" or \"-nN\", not \"-n=N\"")
			}
			if rest == "auto" {
				perRowAuto = true
				continue
			}
			n, err := parseCount(rest, "-n", maxPerRow)
			if err != nil {
				return 0, "", "", "", geo, false, 0, 0, false, false, err
			}
			perRow = n
			perRowAuto = false
		default:
			positional = append(positional, a)
		}
	}

	if len(positional) > 1 {
		return 0, "", "", "", geo, false, 0, 0, false, false, fmt.Errorf("expected one comma-separated zone list, got %d: %s",
			len(positional), strings.Join(positional, " "))
	}
	if len(positional) == 0 {
		return perRow, "", colorWhen, dayWhen, geo, quiet, cellRatioFlag, scaleFlag, scaleAuto, perRowAuto, nil
	}
	return perRow, positional[0], colorWhen, dayWhen, geo, quiet, cellRatioFlag, scaleFlag, scaleAuto, perRowAuto, nil
}

// A resolved clock face is just its location. No label is stored: it comes off
// the instant at render time, so a face drawn either side of a daylight-saving
// change relabels itself from EST to EDT without being rebuilt.

// zoneAliases fills the gaps the tz database leaves, and only those gaps.
// EST, MST, HST, GMT, CET and EET are real zones with fixed, DST-free
// meanings, so they are looked up verbatim instead: aliasing GMT to
// Europe/London would make it read BST every July, which is simply wrong.
// Sorted, and kept in the same order as clock.py's table.
var zoneAliases = []struct{ name, zone string }{
	{"ACT", "Australia/Adelaide"},
	{"AET", "Australia/Sydney"},
	{"AKT", "America/Anchorage"},
	{"AWT", "Australia/Perth"},
	{"BST", "Europe/London"},
	{"CT", "America/Chicago"},
	{"ET", "America/New_York"},
	{"HKT", "Asia/Hong_Kong"},
	{"HT", "Pacific/Honolulu"},
	{"IST", "Asia/Kolkata"},
	{"JST", "Asia/Tokyo"},
	{"KST", "Asia/Seoul"},
	{"MT", "America/Denver"},
	{"NZT", "Pacific/Auckland"},
	{"PT", "America/Los_Angeles"},
	{"SGT", "Asia/Singapore"},
	{"UK", "Europe/London"},
}

// zoneFixed catches the half of each daylight-saving pair that names an offset
// rather than a place: nowhere is on PDT in January, so these cannot be looked
// up in the tz database. Each becomes a fixed-offset clock that never shifts,
// which is precisely what the name means -- PST is Los Angeles in winter, and
// stays there in July while PT moves to PDT. EST, MST and HST are absent
// because the tz database already carries them as fixed zones, and CST is the
// US reading; China is CN or Asia/Shanghai.
var zoneFixed = []struct {
	name   string
	offset int
}{
	{"AKDT", -8 * 3600},
	{"AKST", -9 * 3600},
	{"CDT", -5 * 3600},
	{"CST", -6 * 3600},
	{"EDT", -4 * 3600},
	{"HDT", -9 * 3600},
	{"MDT", -6 * 3600},
	{"PDT", -7 * 3600},
	{"PST", -8 * 3600},
}

func aliasNames() string {
	names := make([]string, len(zoneAliases))
	for i, a := range zoneAliases {
		names[i] = a.name
	}
	return strings.Join(names, " ")
}

func allDigits(s string) bool {
	for _, c := range []byte(s) {
		if c < '0' || c > '9' {
			return false
		}
	}
	return s != ""
}

func isCountryCode(s string) bool {
	if len(s) != 2 {
		return false
	}
	return s[0] >= 'A' && s[0] <= 'Z' && s[1] >= 'A' && s[1] <= 'Z'
}

// zoneTab returns the contents of the tz database's country table, and whether
// it was found at all. Absent on stripped-down systems, so never fatal.
func zoneTab() (string, bool) {
	for _, dir := range []string{
		os.Getenv("TZDIR"),
		"/usr/share/zoneinfo",
		"/usr/share/lib/zoneinfo",
		"/usr/lib/locale/TZ",
	} {
		if dir == "" {
			continue
		}
		if data, err := os.ReadFile(dir + "/zone.tab"); err == nil {
			return string(data), true
		}
	}
	return "", false
}

// countryZones lists a country's zones in file order, which is the tz
// database's own idea of most-populous-first rather than anything alphabetical.
func countryZones(cc string) ([]string, bool) {
	data, found := zoneTab()
	if !found {
		return nil, false
	}
	var out []string
	for _, line := range strings.Split(data, "\n") {
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		f := strings.Split(line, "\t")
		if len(f) >= 3 && f[0] == cc {
			out = append(out, f[2])
		}
	}
	return out, true
}

// countryZone resolves a 2-letter country code, collapsing zones that agree.
// Germany lists Europe/Berlin and Europe/Busingen, an enclave that has kept
// the same time since 1970, so DE is not genuinely ambiguous; the US is.
func countryZone(cc string, at time.Time) (*time.Location, error) {
	names, found := countryZones(cc)
	if !found {
		return nil, fmt.Errorf(
			"cannot resolve the country code \"%s\": no zone.tab under /usr/share/zoneinfo", cc)
	}
	var locs []*time.Location
	var kept []string
	seen := map[string]bool{}
	for _, n := range names {
		loc, err := time.LoadLocation(n)
		if err != nil {
			continue
		}
		abbr, off := at.In(loc).Zone()
		key := fmt.Sprintf("%s|%d", abbr, off)
		if !seen[key] {
			seen[key] = true
			locs = append(locs, loc)
			kept = append(kept, n)
		}
	}
	switch {
	case len(locs) == 0:
		return nil, nil // not a country code we know; caller falls through
	case len(locs) == 1:
		return locs[0], nil
	}
	shown := kept
	tail := ""
	if len(shown) > 8 {
		tail = fmt.Sprintf(" (and %d more)", len(shown)-8)
		shown = shown[:8]
	}
	return nil, fmt.Errorf("%s spans %d time zones; name one: %s%s",
		cc, len(kept), strings.Join(shown, ", "), tail)
}

// zipRuns maps US ZIP prefixes to time zones, run-length encoded as fixed
// four-byte records "NNNc": the 3-digit prefix a run starts at, then a zone
// letter from zipZones. A run reaches to the next record's prefix, and the
// last run to 999; "-" marks prefixes the Postal Service has not assigned.
//
// Derived from US Census ZCTA centroids (public domain) via
// timezone-boundary-builder (ODbL). Generated by tools/genzips.py into both
// clock.go and clock.py in one pass -- never edit it by hand, or the two
// implementations will drift.
const zipRuns = "000-006R010E055-056E090-100E192-193E213-214E269-270E311-312E324C326E332-333E340-341E343-344E345-346E348-349E350C353-354C374E375-376E380C398E399-400E419-420C425E427C428-430E459-460E463C465E476C478E500C509-510C517-520C529-530C533-534C536-537C552-553C555-556C568-570C576M578-580C586M587C589-590M600C621-622C632-633C642-644C649-650C659-660C663-664C682-683C691M692C693M694-700C702-703C709-710C715-716C732-734C742-743C771-772C799M817-820M835P836M838P839-840M842-843M848-850Z854-855Z858-859Z861-863Z865M866-870M872-873M876-877M885-890P892-893P896-897P899-900P901-902P909-910P929-930P938-939P942-943P962-967H969G970P979M980P987-988P995A" // zip-runs: generated by tools/genzips.py

// zipZones maps the one-letter codes in zipRuns to zones. Phoenix is separate
// from Denver because Arizona does not observe daylight saving, and Adak from
// Anchorage because the western Aleutians run an hour further behind.
var zipZones = map[byte]string{
	'A': "America/Anchorage",
	'C': "America/Chicago",
	'D': "America/Adak",
	'E': "America/New_York",
	'G': "Pacific/Guam",
	'H': "Pacific/Honolulu",
	'M': "America/Denver",
	'P': "America/Los_Angeles",
	'R': "America/Puerto_Rico",
	'S': "Pacific/Pago_Pago",
	'Z': "America/Phoenix",
}

// zipExceptions are the ZIPs a 3-digit prefix gets wrong. A prefix that
// straddles a zone boundary has to round to one side, so every ZIP on the
// losing side is an hour or more out; these records name each of them exactly.
// Consulted only when all five digits were given, since three cannot say which
// ZIP is meant.
//
// A record is "PPPcNN" -- prefix, zone letter from zipZones, and how many
// two-digit suffixes follow -- then that many suffixes, ascending:
//
//	373C38 01 02 07 ...        (spaces for clarity only)
//
// Writing the prefix once per group rather than once per ZIP is what keeps this
// smaller than a flat table of five-digit records. The stride varies, so this is
// a short forward scan rather than a binary search -- there are only thirty-odd
// groups, and it runs once per zone at startup, never per frame.
//
// Sourced from the same Census ZCTA centroids as zipRuns, so it covers every
// ZIP that has a ZCTA; PO-box and single-building ZIPs have none, and still
// fall back to their prefix. Generated by tools/genzips.py into both clock.py
// and clock.go in one pass -- never edit it by hand.
const zipExceptions = "324E0156368E06546367697077373E380203070809101112151617212223252629313233363741435051535461626369707379818591374C0119377C0123401C1311151940434445465270717678426C03022942427E12011618243233404858768488465C03313234475C1614152023253137505152747677798688479C082243485163647778498C230102121521313447485258637073747677818687929396499C12020311152027353847596869575M1032374347515253677477576C0431324648585M052933626469586C023138588M0138677M06333541586162678M0436577879690M082123273033374145691C18012023303235384243515763656667697071692M0411161819798M1021353637383947495153835M0422424749860M1303162031333435404445475354865Z0102967S0199979P03040507995D024647" // zip-exceptions: generated by tools/genzips.py

// zipLookup finds the zone for a 3-digit ZIP prefix, "" if unassigned. Binary
// search over fixed-width records; the string comparison is exact because
// zero-padded 3-digit decimals sort lexicographically the way they sort
// numerically, so no integer parsing is involved on either side of the port.
func zipLookup(p3 string) string {
	lo, hi, hit := 0, len(zipRuns)/4-1, -1
	for lo <= hi {
		mid := (lo + hi) / 2
		if zipRuns[mid*4:mid*4+3] <= p3 {
			hit = mid
			lo = mid + 1
		} else {
			hi = mid - 1
		}
	}
	if hit < 0 {
		return ""
	}
	return zipZones[zipRuns[hit*4+3]]
}

// zipException is the zone for one exact 5-digit ZIP, or "" if the prefix gets
// it right.
func zipException(zip5 string) string {
	prefix, suffix := zip5[:3], zip5[3:]
	for i := 0; i < len(zipExceptions); {
		group := zipExceptions[i : i+3]
		letter := zipExceptions[i+3]
		count := int(zipExceptions[i+4]-'0')*10 + int(zipExceptions[i+5]-'0')
		body := i + 6
		if group == prefix {
			for k := 0; k < count; k++ {
				if zipExceptions[body+k*2:body+k*2+2] == suffix {
					return zipZones[letter]
				}
			}
			// fall through: a prefix may own more than one group
		} else if group > prefix {
			break // records are sorted, so no later group can match
		}
		i = body + count*2
	}
	return ""
}

func zipZone(token string) (*time.Location, error) {
	prefix := token
	if len(token) == 5 {
		prefix = token[:3]
	} else if len(token) != 3 {
		return nil, fmt.Errorf(
			"\"%s\" is not a US ZIP code; give all five digits, or the first three", token)
	}
	// an exact ZIP beats its prefix's majority; three digits have only the
	// majority to go on
	name := ""
	if len(token) == 5 {
		name = zipException(token)
	}
	if name == "" {
		name = zipLookup(prefix)
	}
	if name == "" {
		return nil, fmt.Errorf("no US time zone is recorded for ZIP codes starting %s", prefix)
	}
	loc, err := time.LoadLocation(name)
	if err != nil {
		return nil, fmt.Errorf("ZIP %s means %s, which this system's time zone database lacks", prefix, name)
	}
	return loc, nil
}

// suffixZones is the zones whose name ends with the token as a whole path
// segment: Europe/Berlin for "Berlin", and America/Indiana/Indianapolis for
// either "Indianapolis" or "Indiana/Indianapolis". Whole segments only, so
// "Berl" finds nothing and "York" does not answer for "New_York".
//
// Read out of zone.tab, the same file the country codes come from, which lists
// the canonical zones and leaves out the backward-compatibility links -- so
// "Eastern" is not a name here, and US/Eastern still resolves the ordinary
// way, in full.
func suffixZones(token string) ([]string, bool) {
	data, found := zoneTab()
	if !found {
		return nil, false
	}
	want := "/" + strings.ToLower(token)
	var out []string
	for _, line := range strings.Split(data, "\n") {
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		fields := strings.Split(line, "\t")
		if len(fields) >= 3 && strings.HasSuffix(strings.ToLower(fields[2]), want) {
			out = append(out, fields[2])
		}
	}
	return out, true
}

// suffixZone is the one zone named by its tail alone, or nil when nothing
// matches. Ambiguity is refused rather than guessed at: every city in the tz
// database is unique today, but nothing promises it stays that way, and two
// clocks an ocean apart is not a choice to make on the reader's behalf.
func suffixZone(token string) (*time.Location, error) {
	names, found := suffixZones(token)
	if !found || len(names) == 0 {
		return nil, nil
	}
	if len(names) > 1 {
		shown, tail := names, ""
		if len(shown) > 8 {
			tail = fmt.Sprintf(" (and %d more)", len(shown)-8)
			shown = shown[:8]
		}
		return nil, fmt.Errorf("%s names %d zones; name one in full: %s%s",
			token, len(names), strings.Join(shown, ", "), tail)
	}
	loc, err := time.LoadLocation(names[0])
	if err != nil {
		return nil, nil
	}
	return loc, nil
}

func unknownZone(token string) error {
	return fmt.Errorf("unknown zone \"%s\"; use an IANA name (Europe/Berlin), "+
		"a city off the end of one (Berlin, Jakarta), an abbreviation (%s), "+
		"a 2-letter country code (JP), or a US ZIP code",
		token, aliasNames())
}

// resolveZone turns one token into a location. Order matters: the alias table
// is consulted before the tz database only for names the database lacks, the
// fixed-offset table only after it so that real zones win, and
// "local" and "" are intercepted because Go and Python disagree about both --
// LoadLocation("Local") works where ZoneInfo("Local") raises, and
// LoadLocation("") quietly returns UTC where ZoneInfo("") raises.
func resolveZone(token string, at time.Time) (*time.Location, error) {
	if strings.HasPrefix(token, "/") || strings.Contains(token, "..") {
		return nil, fmt.Errorf("\"%s\" is not a zone name", token)
	}
	if strings.EqualFold(token, "local") {
		return time.Local, nil
	}
	if allDigits(token) {
		return zipZone(token)
	}
	up := strings.ToUpper(token)
	for _, a := range zoneAliases {
		if a.name == up {
			loc, err := time.LoadLocation(a.zone)
			if err != nil {
				return nil, fmt.Errorf("%s means %s, which this system's time zone database lacks", up, a.zone)
			}
			return loc, nil
		}
	}
	if loc, err := time.LoadLocation(token); err == nil {
		return loc, nil
	}
	for _, f := range zoneFixed {
		if f.name == up {
			return time.FixedZone(f.name, f.offset), nil
		}
	}
	if isCountryCode(up) {
		loc, err := countryZone(up, at)
		if err != nil {
			return nil, err
		}
		if loc != nil {
			return loc, nil
		}
	}
	// Last, so a city can never shadow a name the database itself answers to.
	named, err := suffixZone(token)
	if err != nil {
		return nil, err
	}
	if named != nil {
		return named, nil
	}
	return nil, unknownZone(token)
}

// request is one zone as it was asked for: the token the user typed, and where
// it landed. The token is carried along because mergeZones labels a face with
// the spellings that asked for it, not just the zone it landed on.
type request struct {
	token string
	loc   *time.Location
}

// dial is one face after merging: the name written over it, and its zone.
type dial struct {
	label string
	loc   *time.Location
}

// resolveZones turns the comma-separated list into requests, left to right.
func resolveZones(list string, at time.Time) ([]request, error) {
	if list == "" {
		return []request{{"", time.Local}}, nil
	}
	tokens := strings.Split(list, ",")
	out := make([]request, 0, len(tokens))
	for _, tok := range tokens {
		tok = strings.Trim(tok, " \t")
		if tok == "" {
			return nil, fmt.Errorf("empty zone in \"%s\"", list)
		}
		loc, err := resolveZone(tok, at)
		if err != nil {
			return nil, err
		}
		out = append(out, request{tok, loc})
	}
	return out, nil
}

// zoneLabel is the name written over one face: just the abbreviation, unless
// more than one spelling collapsed onto this face -- then each spelling that
// reads differently is named too, because that is the only place the ambiguity
// is visible. PDT,PDT asked the same question twice and gets one plain answer.
func zoneLabel(abbr string, tokens []string) string {
	if len(tokens) < 2 {
		return abbr
	}
	parts := []string{abbr}
	for _, t := range tokens {
		if !strings.EqualFold(t, abbr) {
			parts = append(parts, t)
		}
	}
	return strings.Join(parts, "/")
}

// mergeZones collapses zones that show the same wall clock at now into one
// face. Keyed on abbreviation and offset, the same test countryZone uses:
// however two tokens were spelled, and whether or not one resolves onto the
// other, they are one clock if they read alike. That is a property of the
// instant, not of the zones -- PDT and PT are one clock in July and two in
// January -- so this regroups every frame rather than once at startup, and a
// grid crossing a daylight-saving boundary splits itself as it happens.
func mergeZones(zones []request, now time.Time) []dial {
	type group struct {
		abbr   string
		tokens []string
		loc    *time.Location
	}
	var groups []*group
	index := map[string]*group{}

	for _, z := range zones {
		t := now.In(z.loc)
		abbr, off := t.Zone()
		key := fmt.Sprintf("%s|%d", abbr, off)
		g, seen := index[key]
		if !seen {
			g = &group{abbr: abbr, loc: z.loc}
			index[key] = g
			groups = append(groups, g)
		}
		if z.token == "" {
			continue
		}
		dup := false
		for _, prev := range g.tokens {
			if strings.EqualFold(prev, z.token) {
				dup = true
				break
			}
		}
		if !dup {
			g.tokens = append(g.tokens, z.token)
		}
	}

	out := make([]dial, len(groups))
	for i, g := range groups {
		out[i] = dial{zoneLabel(g.abbr, g.tokens), g.loc}
	}
	return out
}

// utcOffset is how far loc sits from UTC at now, in seconds east.
func utcOffset(now time.Time, loc *time.Location) int {
	_, off := now.In(loc).Zone()
	return off
}

// orderFaces sorts the faces into the order their clocks read, earliest
// first: left to right, then top to bottom. Sorted on the offset, which is the
// same thing: every face renders one instant, so the time one reads is that
// instant plus its offset, and the westernmost zone is the one furthest
// behind. Faces that share an offset keep the order they were typed in --
// SliceStable, not Slice, and Python's sorted() is stable for the same reason
// -- which is how UTC and GMT, two faces because they are labelled
// differently, stay where you put them.
//
// Redone every frame, like the merging: an offset is a property of the
// instant, so a zone entering daylight saving slides a place along.
func orderFaces(faces []dial, now time.Time) []dial {
	sort.SliceStable(faces, func(i, j int) bool {
		return utcOffset(now, faces[i].loc) < utcOffset(now, faces[j].loc)
	})
	return faces
}

// center pads s to w columns, the extra space going on the right. Counts runes,
// not bytes, to match Python's "{:^w}" — zone labels are ASCII today, but the
// alias table is hand-maintained and the two must not drift.
func center(s string, w int, extraLeft bool) string {
	n := utf8.RuneCountInString(s)
	if n >= w {
		return s
	}
	left := (w - n) / 2
	if extraLeft {
		left = (w - n + 1) / 2
	}
	return strings.Repeat(" ", left) + s + strings.Repeat(" ", w-n-left)
}

// truncate cuts s to n runes, counting runes rather than bytes so it cannot
// split a multi-byte character -- and so it agrees with Python's slicing.
func truncate(s string, n int) string {
	if n < 0 {
		n = 0
	}
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

// dayNames is Sunday-first, indexed by time.Weekday. A table rather than a
// formatted day, because Python's strftime("%a") follows the locale -- "lun."
// in a French shell -- where Go's Format is fixed English. Hard-coding it
// keeps the two renders identical on every machine.
var dayNames = [7]string{"Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"}

// dayCols is the width of "Mon 05:02:41.901", against the bare readout's 12.
// A face narrower than this would shove the columns to its right out of true,
// so a very low CLOCK_CELL_RATIO loses the weekday rather than the alignment.
const dayCols = 16

// calDay is a calendar date, comparable with ==.
type calDay struct {
	year  int
	month time.Month
	day   int
}

func dayOf(t time.Time) calDay {
	y, m, d := t.Date()
	return calDay{y, m, d}
}

// showWeekday reports whether the readouts carry a weekday. Under auto, only
// when the faces on screen disagree about the date: a weekday under every
// clock is noise when they all fall on the same one. Nothing off screen is
// consulted, so the clock never asserts a date for a zone it is not drawing --
// name "local" in the list to compare against your own day.
//
// Up to three dates can be on screen at once, since UTC-12 to UTC+14 spans 26
// hours and so crosses two midnights.
//
// A face too narrow to hold the weekday loses it whatever the setting says,
// since the alternative is a grid out of true.
func showWeekday(faces []dial, now time.Time, when string) bool {
	if when == "never" || colsN < dayCols || len(faces) == 0 {
		return false
	}
	if when == "always" {
		return true
	}
	first := dayOf(now.In(faces[0].loc))
	for _, d := range faces {
		if dayOf(now.In(d.loc)) != first {
			return true
		}
	}
	return false
}

// digital renders the readout under one face: 12 characters, or 16 with a
// weekday on it. In a grid an over-long line would shove every column to its
// right out of true, which is what dayCols guards.
func digital(t time.Time, weekday bool) string {
	clock := t.Format("15:04:05.000")
	if !weekday {
		return clock
	}
	return dayNames[int(t.Weekday())] + " " + clock
}

// chunkCount is how many rows of faces perRow produces.
func chunkCount(n, perRow int) int {
	return (n + perRow - 1) / perRow
}

// frameHeight counts the rows a grid occupies: each chunk is a face, its zone
// name and its digital line, with vgap blank rows between chunks.
func frameHeight(chunks, vgap int) int {
	return chunks*(rowsN+2) + vgap*(chunks-1)
}

// layout is what spread() works out for one frame: the blank columns between
// two faces, the margin in front of the first of a row, the blank rows between
// rows of faces, and the blank rows above the grid.
type layout struct {
	gap, extra, left  int  // columns between faces, the one widened gutter, margin
	vgap, vextra, top int  // and the same down the window
	extraLeft         bool // which way a label that will not centre leans
}

// gapFloor is the gap a layout will not go below. An even fill starts from the
// least the grid can be packed to and grows; a percentage is that share of the
// whole span, and is the whole answer. A span of 0 is a window that could not
// be measured, where a percentage of nothing is nothing useful, so the least
// stands.
func gapFloor(span, percent, least int) int {
	if percent < 0 || span <= 0 {
		return least
	}
	return span * percent / 100
}

// spread lays count blocks of size across span, returning the gap between two
// of them and the margin in front of the first.
//
// An even fill counts the margins as gaps too -- count blocks make count + 1
// spaces, two of them against the edges -- and gives each an equal share of
// what the blocks leave. Sharing between the blocks alone would hand every
// spare column to the gutters and press the outer clocks flat against the
// borders, which is the one arrangement nobody wants.
//
// The share stops at the size of a block: past that the clocks read as
// scattered rather than as a group, so on a wide window the extra goes to the
// margins and the clocks stay a cluster in the middle. It never drops below
// least either, so a window just big enough for the grid gets the packed
// layout rather than a squeeze.
//
// A percentage fixes the gap outright and leaves everything else to the
// margin, so the alignment has something to work with. An unmeasurable span
// keeps the packed layout this clock had before any of it was adjustable:
// least gap, no margin.
//
// Comes back as (gap, extra, margin, leaned). A centred layout cannot halve an
// odd slack into two margins, but a gutter can swallow the odd column instead:
// extra widens one gutter by one, and the margins come out equal. That only
// works where there is a gutter, so a single clock still has to lean, and
// leaned says it did -- the caller's cue to lean the other way on the next
// rounding, so the two cancel rather than adding up.
func spread(count, size, span, least, percent int, align string) (int, int, int, bool) {
	gap := gapFloor(span, percent, least)
	if span <= 0 {
		return gap, 0, 0, false
	}
	if percent < 0 {
		share := 0
		if free := span - count*size; free > 0 {
			share = free / (count + 1)
		}
		gap = share
		if gap < least {
			gap = least
		}
		if gap > size {
			gap = size
		}
	}
	slack := span - count*size - gap*(count-1)
	if slack < 0 {
		slack = 0
	}
	extra := 0
	// A padding asked for by name is left exactly as asked for; only the even
	// fill, which chose this gap itself, may nudge one gutter.
	if align == "center" && percent < 0 && count > 1 && slack%2 == 1 {
		extra, slack = 1, slack-1
	}
	switch align {
	case "center":
		return gap, extra, slack / 2, slack%2 == 1
	case "right", "bottom":
		return gap, 0, slack, false
	}
	return gap, 0, 0, false
}

// ljust pads s out to w columns on the right, counting runes as Python's
// "{:<w}" does. Never truncates.
func ljust(s string, w int) string {
	if n := w - utf8.RuneCountInString(s); n > 0 {
		return s + strings.Repeat(" ", n)
	}
	return s
}

// helpRows is the key list, one row per key.
func helpRows() []string {
	rows := make([]string, len(hotkeys))
	for i, k := range hotkeys {
		rows[i] = ljust(k.key, hotkeyCol) + k.what
	}
	return rows
}

// frame draws the whole grid: faces left to right, wrapping every perRow. A
// short last row is left-aligned so the column gutters stay lined up.
func frame(faces []dial, now time.Time, perRow int, color bool, dayWhen string, lay layout) []string {
	indent := strings.Repeat(" ", lay.left)
	weekday := showWeekday(faces, now, dayWhen)
	chunks := chunkCount(len(faces), perRow)

	// row joins one line of a chunk. The widened gutter is always the last of
	// a full row rather than whatever the last one happens to be, so a short
	// row's gutters still line up with the row above.
	row := func(parts []string) string {
		var b strings.Builder
		b.WriteString(indent)
		for i, part := range parts {
			if i > 0 {
				n := lay.gap
				if lay.extra > 0 && i-1 == perRow-2 {
					n++
				}
				b.WriteString(strings.Repeat(" ", n))
			}
			b.WriteString(part)
		}
		return b.String()
	}
	rows := make([]string, 0, lay.top+frameHeight(chunks, lay.vgap))
	for i := 0; i < lay.top; i++ {
		rows = append(rows, "")
	}

	for ci := 0; ci < chunks; ci++ {
		hi := (ci + 1) * perRow
		if hi > len(faces) {
			hi = len(faces)
		}
		chunk := faces[ci*perRow : hi]

		if ci > 0 {
			n := lay.vgap
			if lay.vextra > 0 && ci == chunks-1 {
				n++
			}
			for i := 0; i < n; i++ {
				rows = append(rows, "")
			}
		}

		times := make([]time.Time, len(chunk))
		drawn := make([][]string, len(chunk))
		for i, d := range chunk {
			times[i] = now.In(d.loc)
			drawn[i] = face(times[i], color)
		}
		for r := 0; r < rowsN; r++ {
			parts := make([]string, len(drawn))
			for i := range drawn {
				parts[i] = drawn[i][r]
			}
			rows = append(rows, row(parts))
		}
		labels := make([]string, len(chunk))
		digits := make([]string, len(chunk))
		for i, d := range chunk {
			labels[i] = center(truncate(d.label, colsN), colsN, lay.extraLeft)
			digits[i] = center(digital(times[i], weekday), colsN, lay.extraLeft)
		}
		rows = append(rows, row(labels))
		rows = append(rows, row(digits))
	}
	return rows
}

// isTerminal reports whether f is a character device, i.e. a terminal rather
// than a pipe or a file. Matches Python's sys.stdout.isatty() for the cases
// that matter here.
func isTerminal(f *os.File) bool {
	info, err := f.Stat()
	return err == nil && info.Mode()&os.ModeCharDevice != 0
}

// modalBox draws content inside a one-line border, used for both the key
// list and the startup quit hint so the two read as the same kind of thing:
// a modal overlaid on the clocks, not part of the grid underneath it.
func modalBox(content []string) []string {
	width := 0
	for _, c := range content {
		if w := utf8.RuneCountInString(c); w > width {
			width = w
		}
	}
	box := make([]string, 0, len(content)+2)
	box = append(box, "┌"+strings.Repeat("─", width+2)+"┐")
	for _, c := range content {
		box = append(box, "│ "+ljust(c, width)+" │")
	}
	return append(box, "└"+strings.Repeat("─", width+2)+"┘")
}

// centerModal places a modal in the middle of a termCols x termRows window.
// ok is false when it does not fit, the same trade the key list already made
// against a narrow window: no modal beats a wrapped or clipped one. A window
// that cannot be measured has nowhere settled to put one, so that is also a
// no.
func centerModal(box []string, termCols, termRows int) (top, left int, ok bool) {
	width, height := utf8.RuneCountInString(box[0]), len(box)
	if termCols <= 0 || termRows <= 0 || width > termCols || height > termRows {
		return 0, 0, false
	}
	return (termRows - height) / 2, (termCols - width) / 2, true
}

// overlayModal stamps box onto rows at (top, left), extending rows with
// blank lines and padding short ones with spaces so the box always lands
// intact regardless of what the grid drew there. rows must be plain text --
// no ANSI -- which run guarantees by turning colour off for any frame a
// modal is going to be stamped onto, so a modal never has to reason about
// resuming a hand's colour on the far side of it.
func overlayModal(rows []string, box []string, top, left int) []string {
	out := make([]string, len(rows))
	copy(out, rows)
	for len(out) < top+len(box) {
		out = append(out, "")
	}
	for i, line := range box {
		r := top + i
		out[r] = spliceRow(out[r], left, utf8.RuneCountInString(line), line)
	}
	return out
}

// spliceRow overwrites the visible columns [col, col+width) of row with
// insert -- plain text, never coloured itself -- while preserving whatever
// ANSI colour the row carried outside that span, and resuming it correctly
// on the far side. row may already be full of colour escapes (a face's hand
// can pass under where a modal lands) or may have none at all (--color=never,
// or a redirected frame); either way nothing outside [col, col+width) changes.
func spliceRow(row string, col, width int, insert string) string {
	runes := []rune(row)
	n := len(runes)
	var before, after strings.Builder
	active := "" // the last SGR escape seen so far, "" meaning none yet
	startActive, endActive := "", ""
	startCaptured, endCaptured := false, false
	visible := 0
	for i := 0; i < n; {
		if runes[i] == '\x1b' {
			j := i + 1
			for j < n && runes[j] != 'm' {
				j++
			}
			if j < n {
				j++ // include the 'm'
			}
			seq := string(runes[i:j])
			active = seq
			switch {
			case visible < col:
				before.WriteString(seq)
			case visible >= col+width:
				after.WriteString(seq)
			}
			i = j
			continue
		}
		if !startCaptured && visible >= col {
			startCaptured, startActive = true, active
		}
		if !endCaptured && visible >= col+width {
			endCaptured, endActive = true, active
		}
		switch {
		case visible < col:
			before.WriteRune(runes[i])
		case visible >= col+width:
			after.WriteRune(runes[i])
		}
		visible++
		i++
	}
	if !startCaptured {
		startActive = active
	}
	if !endCaptured {
		endActive = active
	}
	if visible < col {
		before.WriteString(strings.Repeat(" ", col-visible))
	}
	result := before.String()
	if startActive != "" && startActive != defaultFG {
		result += defaultFG
	}
	result += insert
	if after.Len() > 0 {
		if endActive != "" {
			result += endActive
		}
		result += after.String()
	}
	return result
}

// fold breaks text onto lines of at most width, on spaces where it can be. A
// word with nowhere to break -- a window narrower than "--per-row" -- is cut
// instead, since the alternative is a line that wraps itself and scrolls the
// screen out from under the next repaint.
func fold(text string, width int) []string {
	var rows []string
	line := ""
	for _, word := range strings.Split(text, " ") {
		for width > 0 && utf8.RuneCountInString(word) > width {
			if line != "" {
				rows = append(rows, line)
				line = ""
			}
			r := []rune(word)
			rows = append(rows, string(r[:width]))
			word = string(r[width:])
		}
		switch {
		case line == "":
			line = word
		case utf8.RuneCountInString(line)+1+utf8.RuneCountInString(word) <= width:
			line += " " + word
		default:
			rows = append(rows, line)
			line = word
		}
	}
	if line != "" {
		rows = append(rows, line)
	}
	return rows
}

// complaint is the frame that says why there are no clocks, when the window is
// too small to hold them. Folded to the window and cut to it, and placed the
// way the quit hint is: centred with the clocks, hard left under any other
// alignment.
func complaint(text string, termCols, termRows int, halign string) []string {
	width := termCols
	if width <= 0 {
		width = utf8.RuneCountInString(text)
	}
	rows := fold(text, width)
	if termRows > 0 && len(rows) > termRows {
		rows = rows[:termRows]
	}
	if halign == "center" {
		for i, r := range rows {
			rows[i] = strings.Repeat(" ", (width-utf8.RuneCountInString(r))/2) + r
		}
	}
	if termRows > 0 {
		out := make([]string, 0, termRows)
		for i := 0; i < (termRows-len(rows))/2; i++ {
			out = append(out, "")
		}
		return append(out, rows...)
	}
	return rows
}

// useColor reports whether to colour the hands. auto colours a terminal and
// leaves a pipe or a file plain, so a redirected frame stays the plain text
// the difftest compares. NO_COLOR is the cross-tool convention for "never,
// from the environment"; an explicit --color=always overrules it, since that
// is the point of saying always.
func useColor(when string) bool {
	if when != "auto" {
		return when == "always"
	}
	return isTerminal(os.Stdout) && os.Getenv("NO_COLOR") == ""
}

// winsize is the TIOCGWINSZ payload; package syscall declares the ioctl but
// not, on darwin, the struct.
type winsize struct {
	Row, Col, Xpixel, Ypixel uint16
}

// envCount reads a positive count from the environment, 0 when unset or junk.
func envCount(name string) int {
	n, err := parseCount(os.Getenv(name), name, 100000)
	if err != nil {
		return 0
	}
	return n
}

// termSize reports the terminal as (columns, rows); 0 means "could not tell".
// COLUMNS and LINES win when set, both because that is the shell convention
// and because it gives the diff harness a way to pin the layout. Deliberately
// not Python's shutil.get_terminal_size, which invents 80x24 when it cannot
// tell -- a pipe has to stay distinguishable from an 80-column window.
func termSize() (int, int) {
	cols, rows := envCount("COLUMNS"), envCount("LINES")
	if cols == 0 || rows == 0 {
		var ws winsize
		if ioctl(os.Stdout.Fd(), syscall.TIOCGWINSZ, unsafe.Pointer(&ws)) == nil {
			if cols == 0 {
				cols = int(ws.Col)
			}
			if rows == 0 {
				rows = int(ws.Row)
			}
		}
	}
	return cols, rows
}

// fitPerRow reduces the requested faces-per-row to what the window can hold.
// Wrapping is what actually breaks the display: a wrapped line desynchronises
// the cursor rewind and the frame smears.
func fitPerRow(want, n, termCols, gap int) (int, error) {
	if want > n {
		want = n
	}
	if termCols <= 0 {
		return want, nil // not a terminal: honour what was asked for
	}
	maxFit := (termCols + gap) / (colsN + gap)
	if maxFit < 1 {
		return 0, fmt.Errorf(
			"terminal is %d columns wide and one clock face needs %d; widen the window, or lower --cell-ratio",
			termCols, colsN)
	}
	if want > maxFit {
		want = maxFit
	}
	return want, nil
}

// fitHeight rejects a grid taller than the window. Too tall scrolls, and
// scrolling desynchronises the rewind exactly as wrapping does -- but here the
// fix is to raise --per-row, not lower it.
func fitHeight(chunks, termRows, vgap int) error {
	h := frameHeight(chunks, vgap)
	if termRows > 0 && h > termRows {
		return fmt.Errorf(
			"%d rows of clocks need %d lines and this terminal has %d; raise --per-row, or name fewer zones",
			chunks, h, termRows)
	}
	return nil
}

// autoScale is what --scale auto resolves to every frame: the largest rowsN
// (and its matching colsN) that lets numFaces fit cols x lines at no more
// than wantPerRow per row, using no more than hpad/vpad's own minimum gap on
// each axis -- the same floor fitPerRow and fitHeight already enforce, so a
// maximised face never asks for less room than an explicit --hpad would once
// drawn. Larger rowsN can only ever need as much or more space (a wider face
// fits no more per row, and a taller one needs no fewer lines), so the first
// size that fits, searched from the top down, is the largest one that does.
//
// -n auto passes numFaces itself as wantPerRow -- no cap at all, in effect,
// since fitPerRow already clamps want to numFaces on its own -- rather than
// searching per-row counts separately. fitPerRow always uses the most faces
// a row can hold up to the cap, which is also the fewest chunks (and so the
// least height) any per-row choice at that rowsN could need, so an uncapped
// want already finds whichever per-row count each candidate rowsN fits best
// through, without a second search: capping lower could only ever force more
// chunks than that rowsN needed, never fewer.
//
// Sets the package-level rowsN and colsN to the winner; if nothing in range
// fits, it leaves them at minRowsN so the fitPerRow/fitHeight call right
// after this one reports why.
func autoScale(wantPerRow, numFaces, cols, lines, hpad, vpad int) {
	for n := maxRowsN; n >= minRowsN; n-- {
		rowsN = n
		colsN = int(math.Floor(float64(n)*cellRatio + 0.5))
		perRow, err := fitPerRow(wantPerRow, numFaces, cols, gapFloor(cols, hpad, gap))
		if err != nil {
			continue
		}
		if fitHeight(chunkCount(numFaces, perRow), lines, gapFloor(lines, vpad, vgap)) == nil {
			return
		}
	}
	rowsN = minRowsN
	colsN = int(math.Floor(float64(minRowsN)*cellRatio + 0.5))
}

func ioctl(fd, req uintptr, p unsafe.Pointer) error {
	if _, _, err := syscall.Syscall(syscall.SYS_IOCTL, fd, req, uintptr(p)); err != 0 {
		return err
	}
	return nil
}

func ioctlTermios(fd, req uintptr, t *syscall.Termios) error {
	return ioctl(fd, req, unsafe.Pointer(t))
}

// quietTerminal swallows keystrokes so they can't scroll the frame out from
// under us, and returns the restore. It clears ECHO/ECHONL/ICANON but leaves
// ISIG set, so Ctrl+C still signals. Restoring with the flushing variant drops
// whatever was typed during the run, so stray keys can't land in the shell
// afterwards. Falls back to a no-op when stdin isn't a terminal.
func quietTerminal() func() {
	fd := os.Stdin.Fd()
	var saved syscall.Termios
	if ioctlTermios(fd, tcGet, &saved) != nil {
		return func() {}
	}
	quiet := saved
	quiet.Lflag &^= syscall.ECHO | syscall.ECHONL | syscall.ICANON
	if ioctlTermios(fd, tcSet, &quiet) != nil {
		return func() {}
	}
	return func() {
		restore := saved
		ioctlTermios(fd, tcSetFlush, &restore)
	}
}

// readKeys pumps stdin into a channel. Reads block, so this needs its own
// goroutine; it ends at EOF, which is immediate when stdin isn't a terminal.
func readKeys() <-chan byte {
	keys := make(chan byte, 64)
	go func() {
		buf := make([]byte, 64)
		for {
			n, err := os.Stdin.Read(buf)
			if err != nil {
				return
			}
			for _, b := range buf[:n] {
				select {
				case keys <- b:
				default: // full: the clock only cares about q, so drop the rest
				}
			}
		}
	}()
	return keys
}

// run does everything that can fail up front, before the terminal is touched:
// os.Exit skips deferred restores, so nothing may return an error once the
// cursor is hidden or cbreak mode is on.
func run() error {
	frozen, pinned, err := freeze()
	if err != nil {
		return err
	}
	wantPerRow, zoneList, colorWhen, dayWhen, geo, quiet, cellRatioFlag, scaleFlag, scaleAuto, perRowAuto, err := parseArgs(os.Args[1:])
	if errors.Is(err, errHelp) {
		fmt.Print(usage)
		return nil
	}
	if err != nil {
		return err
	}
	color := useColor(colorWhen)

	// --cell-ratio wins over CLOCK_CELL_RATIO, which wins over the default.
	if cellRatioFlag > 0 {
		cellRatio = cellRatioFlag
	} else {
		cellRatio = envCellRatio()
	}

	// --scale resizes the whole face, keeping the same shape: rowsN moves and
	// colsN follows it, through the cell-ratio arithmetic above. --scale auto
	// instead re-solves both every frame, in the main loop, against whatever
	// the terminal measures to.
	if !scaleAuto {
		scale := 1.0
		if scaleFlag > 0 {
			scale = scaleFlag
		}
		rowsN = int(math.Floor(defaultRowsN*scale + 0.5))
		if rowsN < minRowsN || rowsN > maxRowsN {
			return fmt.Errorf(
				"--scale %g makes each face %d rows tall; want %d to %d rows, roughly --scale %.2f to --scale %.2f",
				scale, rowsN, minRowsN, maxRowsN,
				float64(minRowsN)/defaultRowsN, float64(maxRowsN)/defaultRowsN)
		}
		colsN = int(math.Floor(float64(rowsN)*cellRatio + 0.5))
	}

	// -n auto's whole point is choosing whatever per-row count lets --scale
	// auto grow the face furthest; with a fixed --scale there is no face
	// size left for it to affect, so it falls back to the plain default cap.
	if perRowAuto && !scaleAuto {
		wantPerRow = defaultPerRow
		perRowAuto = false
	}

	// A pinned clock is a still of one instant, and an undated still records
	// half of it, so the weekday goes under every face unless --day says
	// otherwise. Live, auto keeps it for the clocks that actually disagree.
	if dayWhen == "" {
		dayWhen = "auto"
		if pinned {
			dayWhen = "always"
		}
	}

	startedAt := time.Now()
	if pinned {
		startedAt = frozen
	}
	zones, err := resolveZones(zoneList, startedAt)
	if err != nil {
		return err
	}

	sigs := make(chan os.Signal, 1)
	signal.Notify(sigs, os.Interrupt, syscall.SIGTERM)

	defer quietTerminal()()
	keys := readKeys()

	// On a terminal, take the alternate screen and paint from its top corner.
	// Relative rewind cannot survive a resize: the terminal rewraps the frame
	// already on screen, so rows that were one physical line become two, the
	// ESC[nA lands inside the old frame, and its upper half is left behind --
	// clearBelow only ever clears downwards. Homing to a screen we own makes
	// the frame's position independent of what happened to the last one. Piped
	// output keeps the rewind, which costs nothing there and keeps the byte
	// stream the difftest compares unchanged.
	fullScreen := isTerminal(os.Stdout)

	// A pinned clock redirected to a file is the diff harness: one frame and
	// out. On a terminal there is someone watching, so it stays up instead --
	// quitting would restore the screen and take the frame with it.
	oneShot := pinned && !fullScreen

	if fullScreen {
		fmt.Print(enterAlt)
		defer fmt.Print(leaveAlt)
	}
	fmt.Print(hideCursor)
	defer fmt.Print(showCursor)

	t := time.NewTicker(tick)
	defer t.Stop()

	height := 0
	// held is the instant the display is holding, and holding says whether it
	// is. Holding repaints as usual rather than idling, so a resize still
	// reflows the grid -- it is the clock that stops, not the drawing.
	var held time.Time
	holding := false
	helpOn := false
	// Off the wall clock, not the frame's: a clock pinned with CLOCK_FREEZE
	// never advances, and the hint still has to give up after three seconds.
	flashUntil := time.Now().Add(flashFor)
	for {
		now := time.Now()
		if pinned {
			now = frozen
		}
		if holding {
			now = held
		}

		// re-measure every frame rather than trapping SIGWINCH: one ioctl per
		// 19ms is nothing beside redrawing the faces, it also picks up a
		// changed COLUMNS, and it keeps this loop the same shape as the
		// Python one, where a signal handler would interact with sleep()
		// under PEP 475 and drift out of step.
		cols, lines := termSize()
		faces := orderFaces(mergeZones(zones, now), now)

		if scaleAuto {
			if perRowAuto {
				wantPerRow = defaultPerRow // overridden below whenever there is a window to measure
			}
			if cols > 0 && lines > 0 {
				if perRowAuto {
					wantPerRow = len(faces) // no real cap: see autoScale's own comment
				}
				autoScale(wantPerRow, len(faces), cols, lines, geo.hpad, geo.vpad)
			} else {
				// Nothing measurable to fill, so there is nothing to solve --
				// same as any other window that cannot be measured.
				rowsN = defaultRowsN
				colsN = int(math.Floor(defaultRowsN*cellRatio + 0.5))
			}
		}

		var rows []string
		perRow, err := fitPerRow(wantPerRow, len(faces), cols, gapFloor(cols, geo.hpad, gap))
		chunks := 0
		if err == nil {
			chunks = chunkCount(len(faces), perRow)
			err = fitHeight(chunks, lines, gapFloor(lines, geo.vpad, vgap))
		}
		// The key list and the startup hint are the same kind of thing --
		// a modal laid over the clocks -- so only one shows at a time, and
		// the key list, being asked for, wins over a hint that is already
		// redundant with the "q" line in it.
		var content []string
		switch {
		case err == nil && fullScreen && helpOn:
			content = helpRows()
		case !quiet && fullScreen && time.Now().Before(flashUntil):
			content = []string{flashText}
		}
		var modal []string
		var modalTop, modalLeft int
		showModal := false
		if content != nil {
			modal = modalBox(content)
			modalTop, modalLeft, showModal = centerModal(modal, cols, lines)
		}

		if err != nil {
			// A window dragged smaller than the clocks need is something the
			// reader can undo, so say what is wrong and keep measuring: the
			// next frame that fits draws itself. Redirected output has no
			// window to resize and still fails outright, which is what the
			// diff harness compares.
			if !fullScreen {
				return err
			}
			rows = complaint(err.Error(), cols, lines, geo.halign)
		} else {
			// Any lean left over from the grid is answered by the labels
			// leaning the other way, so the frame comes out no more than a
			// column off centre -- and with a gutter to swallow the odd
			// column, dead centre.
			var lay layout
			lay.gap, lay.extra, lay.left, lay.extraLeft = spread(perRow, colsN, cols, gap, geo.hpad, geo.halign)
			lay.vgap, lay.vextra, lay.top, _ = spread(chunks, rowsN+2, lines, vgap, geo.vpad, geo.valign)
			rows = frame(faces, now, perRow, color, dayWhen, lay)
		}
		if showModal {
			rows = overlayModal(rows, modal, modalTop, modalLeft)
		}

		// Repaint in one write. clearEOL wipes a longer previous line,
		// clearBelow a taller previous frame, so the grid reshapes itself
		// when the window changes.
		var b strings.Builder
		if fullScreen {
			// no trailing newline: a frame exactly as tall as the window
			// would otherwise scroll itself off by one line
			b.WriteString(home)
			for i, r := range rows {
				if i > 0 {
					b.WriteString("\n")
				}
				b.WriteString(r + clearEOL)
			}
		} else {
			if height > 0 {
				fmt.Fprintf(&b, "\x1b[%dA", height)
			}
			for _, r := range rows {
				b.WriteString(r + clearEOL + "\n")
			}
		}
		b.WriteString(clearBelow)
		fmt.Print(b.String())
		height = len(rows)

		if oneShot {
			return nil
		}

		// wait out the tick, consuming keys without repainting for each one
		for waiting := true; waiting; {
			select {
			case <-t.C:
				waiting = false
			case <-sigs:
				return nil
			case k := <-keys:
				switch k {
				case 'q', 'Q':
					return nil
				case ' ':
					holding, held = !holding, now
				case 'h', 'H', '?':
					helpOn = !helpOn
				}
			}
		}
	}
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "clock: "+err.Error())
		os.Exit(1)
	}
}
