// Live terminal clock: analog faces for UTC and local time, digital underneath.
//
// Faces are drawn on a braille canvas (2x4 dots per cell). Refreshes every 19ms,
// so the second hand sweeps smoothly rather than stepping. Press q (or Ctrl+C)
// to quit.
package main

import (
	"fmt"
	"math"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const (
	hideCursor = "\x1b[?25l"
	showCursor = "\x1b[?25h"
	clearEOL   = "\x1b[K"

	// 19ms, not 20: coprime to 10, so the millisecond ones digit cycles through
	// all ten values instead of sitting still. Reads as a live clock.
	tick = 19 * time.Millisecond

	rowsN            = 11   // face height, in terminal rows
	defaultCellRatio = 2.1  // cell height / width; braille dots are square at 2
	gap              = 3    // blank columns between the two faces
	markerR          = 0.70 // numeral distance from the centre, as a fraction of the radius
)

// cellRatio is how tall a terminal cell is relative to its width. It is the
// only knob that decides whether the face is round, and it varies by font and
// line spacing. Override without editing: CLOCK_CELL_RATIO=2.7
// Raise it if the face looks squished, lower it if it bulges sideways.
var cellRatio = func() float64 {
	if v, err := strconv.ParseFloat(os.Getenv("CLOCK_CELL_RATIO"), 64); err == nil && v > 0 {
		return v
	}
	return defaultCellRatio
}()

// colsN is the face width in terminal columns.
var colsN = int(math.Floor(rowsN*cellRatio + 0.5))

// Hour numerals, every one two characters wide. A cell spans 2 dots, so an
// even-width string centres on a cell boundary while an odd-width one centres
// half a cell off it: "12" stacks exactly over "06", but never over "6".
var markers = []struct {
	hour int
	text string
}{{0, "12"}, {3, "03"}, {6, "06"}, {9, "09"}}

// hands, drawn shortest-first
var hands = []struct {
	length float64 // as a fraction of the radius
	thick  bool    // two dots thick?
}{{0.50, true}, {0.75, true}, {0.88, false}}

// braille dot bit for (x % 2, y % 4); the block starts at U+2800
var dotBits = [2][4]uint8{{0x01, 0x02, 0x04, 0x40}, {0x08, 0x10, 0x20, 0x80}}

// canvas is a dot grid that renders to braille cells, 2 dots wide by 4 tall each.
type canvas struct {
	w, h, cols int
	cells      []uint8
}

func newCanvas(w, h int) *canvas {
	cols := (w + 1) / 2
	return &canvas{w: w, h: h, cols: cols, cells: make([]uint8, cols*((h+3)/4))}
}

func (c *canvas) set(fx, fy float64) {
	// Floor(v + 0.5), not Round(): Round is half-away-from-zero but Python's
	// round() is half-to-even, which would split the two renders apart
	x, y := int(math.Floor(fx+0.5)), int(math.Floor(fy+0.5))
	if x < 0 || y < 0 || x >= c.w || y >= c.h {
		return
	}
	c.cells[(y/4)*c.cols+x/2] |= dotBits[x%2][y%4]
}

func (c *canvas) line(x0, y0, x1, y1 float64) {
	steps := int(math.Floor(math.Max(math.Abs(x1-x0), math.Abs(y1-y0)) + 0.5))
	if steps < 1 {
		steps = 1
	}
	for i := 0; i <= steps; i++ {
		t := float64(i) / float64(steps)
		c.set(x0+(x1-x0)*t, y0+(y1-y0)*t)
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

// face renders one analog face for t, returning its cell rows and cell width.
func face(t time.Time) ([]string, int) {
	rx, ry := float64(colsN), float64(2*rowsN)
	// Horizontally the centre sits on a cell boundary, vertically in the middle
	// of a row. The dot grid mirrors about both, which is what makes 09 and 03
	// land the same distance from the rim.
	cx, cy := rx-0.5, ry-0.5
	c := newCanvas(2*colsN, 4*rowsN)

	// spoke draws a radial segment from r0 to r1, as fractions of the radius.
	spoke := func(angle, r0, r1 float64, thick bool) {
		sin, cos := math.Sin(angle), math.Cos(angle)
		x0, y0 := cx+rx*r0*sin, cy-ry*r0*cos
		x1, y1 := cx+rx*r1*sin, cy-ry*r1*cos
		// two dots thick straddles the axis, so the spoke centres on it
		offs := []float64{0}
		if thick {
			offs = []float64{-0.5, 0.5}
		}
		for _, off := range offs {
			dx, dy := off*cos, off*sin
			c.line(x0+dx, y0+dy, x1+dx, y1+dy)
		}
	}

	// rim: sample densely enough that adjacent dots touch
	steps := int(math.Floor(4*math.Pi*math.Max(rx, ry) + 0.5))
	for i := 0; i < steps; i++ {
		a := 2 * math.Pi * float64(i) / float64(steps)
		c.set(cx+rx*math.Sin(a), cy-ry*math.Cos(a))
	}

	// hour ticks, the quarters longer and thicker so they sit on the axes
	for h := 0; h < 12; h++ {
		major := h%3 == 0
		inner := 0.90
		if major {
			inner = 0.80
		}
		spoke(2*math.Pi*float64(h)/12, inner, 1.0, major)
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
		spoke(2*math.Pi*turns[i], 0, h.length, h.thick)
	}

	rows := c.rows()

	// hour numerals, overlaid as real characters: a cell holds braille or text
	// but never both, so a numeral hides whatever dots share its cell
	for _, mk := range markers {
		a := 2 * math.Pi * float64(mk.hour) / 12
		x := cx + rx*markerR*math.Sin(a)
		y := cy - ry*markerR*math.Cos(a)
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
	}

	return rows, c.cols
}

// dial is one labelled face: a zone abbreviation and the instant to show.
type dial struct {
	label string
	t     time.Time
}

func center(s string, w int) string {
	if len(s) >= w {
		return s
	}
	left := (w - len(s)) / 2
	return strings.Repeat(" ", left) + s + strings.Repeat(" ", w-len(s)-left)
}

// frame composes side-by-side faces with a digital readout under each.
func frame(dials []dial) []string {
	faces := make([][]string, len(dials))
	width := 0
	for i, d := range dials {
		faces[i], width = face(d.t)
	}
	sep := strings.Repeat(" ", gap)

	rows := make([]string, 0, len(faces[0])+1)
	for r := range faces[0] {
		parts := make([]string, len(faces))
		for i := range faces {
			parts[i] = faces[i][r]
		}
		rows = append(rows, strings.Join(parts, sep))
	}

	digits := make([]string, len(dials))
	for i, d := range dials {
		digits[i] = center(fmt.Sprintf("%s: %s", d.label, d.t.Format("15:04:05.000")), width)
	}
	return append(rows, strings.Join(digits, sep))
}

func ioctlTermios(fd, req uintptr, t *syscall.Termios) error {
	if _, _, err := syscall.Syscall(syscall.SYS_IOCTL, fd, req, uintptr(unsafe.Pointer(t))); err != 0 {
		return err
	}
	return nil
}

// quietTerminal swallows keystrokes so they can't scroll the frame out from
// under us, and returns the restore. It clears ECHO/ECHONL/ICANON but leaves
// ISIG set, so Ctrl+C still signals. Restoring with TIOCSETAF flushes whatever
// was typed during the run, so stray keys can't land in the shell afterwards.
// Falls back to a no-op when stdin isn't a terminal.
func quietTerminal() func() {
	fd := os.Stdin.Fd()
	var saved syscall.Termios
	if ioctlTermios(fd, syscall.TIOCGETA, &saved) != nil {
		return func() {}
	}
	quiet := saved
	quiet.Lflag &^= syscall.ECHO | syscall.ECHONL | syscall.ICANON
	if ioctlTermios(fd, syscall.TIOCSETA, &quiet) != nil {
		return func() {}
	}
	return func() {
		restore := saved
		ioctlTermios(fd, syscall.TIOCSETAF, &restore)
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

func main() {
	sigs := make(chan os.Signal, 1)
	signal.Notify(sigs, os.Interrupt, syscall.SIGTERM)

	defer quietTerminal()()
	keys := readKeys()

	fmt.Print(hideCursor)
	defer fmt.Print(showCursor)

	t := time.NewTicker(tick)
	defer t.Stop()

	height := 0
	for {
		now := time.Now()
		name, _ := now.Zone()
		rows := frame([]dial{{"UTC", now.UTC()}, {name, now}})

		// rewind over the rows drawn last time, then repaint in one write
		var b strings.Builder
		if height > 0 {
			fmt.Fprintf(&b, "\x1b[%dA", height)
		}
		for _, r := range rows {
			b.WriteString(r + clearEOL + "\n")
		}
		fmt.Print(b.String())
		height = len(rows)

		// wait out the tick, consuming keys without repainting for each one
		for waiting := true; waiting; {
			select {
			case <-t.C:
				waiting = false
			case <-sigs:
				return
			case k := <-keys:
				if k == 'q' || k == 'Q' {
					return
				}
			}
		}
	}
}
