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
	"io/fs"
	"math"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"
	"unsafe"

	"github.com/choey/ziptz"
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

	// symmetryWindow is how many rows below the largest fit autoScale will
	// give up looking for one where both rowsN and colsN are odd -- see its
	// own comment for why that is worth a few rows of size.
	symmetryWindow = 8
)

// version is the release this source belongs to. pyclock.py and pyproject.toml
// carry the same string, and difftest holds all three together: a clock that
// cannot say what it is turns every bug report into a round trip, and one that
// says the wrong thing is worse than one that says nothing at all.
const version = "0.4.3"

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
  --version          print the version and exit

A zone is an IANA name (Europe/Berlin), a city (Berlin, Seattle), a US state
(Arizona, or its code as US-AZ), a country (Germany, or its code as DE), a
regional abbreviation (ET CT MT PT AKT HT BST IST JST AET ...), or a US ZIP
code (94110). Each place is labelled with the zone it landed in, as
MST (Arizona) or CEST (DE); write a name with a space in quotes,
"New Mexico", or with underscores, New_Mexico. The bare two letters are a
country and never a state: CA is Canada. ET/CT/MT/PT follow daylight saving,
so they read EST or EDT depending on the date; EST/EDT/PST/PDT and the rest
are the fixed offsets, which never shift.

The hands are coloured on a terminal and plain when redirected; NO_COLOR
turns the colour off everywhere. Auto puts a weekday on the readouts only
when the clocks on screen disagree about the date. An even fill spreads the
clocks over the whole window; --hpad 10% sets the gaps instead, as a share of
the window, and then the alignment decides where the grid sits. CLOCK_CELL_RATIO
sets the same thing as --cell-ratio, for when it wants to be set once per
terminal rather than typed every time; the flag wins if both are given.

Space holds the frame still, for a screenshot, h or ? opens the key list, and
r resizes and aligns the clocks while they run. S saves those sizes and the
zones on screen to ~/.config/clock/config, which every clock reads at startup;
CLOCK_CONFIG points somewhere else, and set but empty means no file at all.
Press q or Ctrl+C to quit.

examples:
  clock
  clock ET,PT,UTC
  clock -n 2 ET,PT,UTC
  clock Berlin,Jakarta
  clock Arizona,Boise,"Salt Lake City"
  clock Europe/Berlin,Asia/Tokyo,94110 --per-row 2
`

// haligns and valigns are where the grid can sit when it does not fill the
// window, and what --halign and --valign accept. Same order as pyclock.py's
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
// pyclock.py's table.
var hotkeys = []struct{ key, what string }{
	{"space", "hold the frame"},
	{"h ?", "toggle this list"},
	{"r", "resize and align the clocks"},
	{"S", "save these sizes and zones"},
	{"q", "quit, or Ctrl+C"},
}

// hotkeyCol is where the descriptions start, so the keys get a gutter.
const hotkeyCol = 8

// tunables is what r puts up: the knobs that can be changed while the clock
// runs, in the order they are listed, and what each one takes -- the same
// words its flag's own error message offers, since a value typed here is read
// by exactly the parser that flag uses. Same order and wording as
// pyclock.py's table.
var tunables = []struct{ name, takes string }{
	{"halign", "left, center or right"},
	{"valign", "top, center or bottom"},
	{"hpad", "even, or a share like 10%"},
	{"vpad", "even, or a share like 5%"},
	{"per-row", "auto, or a count like 3"},
	{"scale", "auto, or a number like 1.5"},
	{"cell-ratio", "a number like 2.1"},
}

// Which tunable is which, and how many there are. The frame loop holds the
// six as the text they were given in rather than as parsed values: what the
// tuner shows, what the parsers read and what a saved command line would say
// are then one string, and no number is ever formatted back out -- Go's %g
// and Python's :g do not agree past six significant digits, and a value the
// reader typed is not ours to round anyway.
const (
	tuneHalign = iota
	tuneValign
	tuneHpad
	tuneVpad
	tunePerRow
	tuneScale
	tuneCellRatio
	numTunes
)

// tuneCol is where the values start, past the longest name plus a gutter.
const tuneCol = 12

// defaultCellRatioText is defaultCellRatio written out, for the tuner and for
// the flags it hands back. Written rather than formatted, so the two ports
// cannot disagree about how a float prints.
const defaultCellRatioText = "2.1"

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

// defaultTunes is the six knobs as an untouched clock has them: the flag
// defaults, with CLOCK_CELL_RATIO standing in for the ratio where it is
// usable. Unlike --cell-ratio, an environment variable might be stale or set
// for some other program, so a bad value there is not a user error -- it is
// simply ignored, the same way an unset one is.
func defaultTunes() [numTunes]string {
	vals := [numTunes]string{
		tuneHalign:    "center",
		tuneValign:    "center",
		tuneHpad:      "even",
		tuneVpad:      "even",
		tunePerRow:    "auto",
		tuneScale:     "auto",
		tuneCellRatio: defaultCellRatioText,
	}
	if v := os.Getenv("CLOCK_CELL_RATIO"); v != "" {
		if _, ok := positiveFloat(v); ok {
			vals[tuneCellRatio] = v
		}
	}
	return vals
}

// tuneIndex is which of the six a flag name is. Only ever asked about the six
// names tunables holds, so there is no not-found to answer.
func tuneIndex(name string) int {
	for i, t := range tunables {
		if t.name == name {
			return i
		}
	}
	return -1
}

// A day inside Python's datetime range at each end. The pinned instant is
// converted into every zone on screen, and a zone can sit 14 hours from UTC,
// so an instant on datetime.min itself overflows the moment pyclock.py shows it
// in Los Angeles -- where this implementation, whose time has no such bound,
// draws it without comment. A day of headroom is more than the 14 hours
// anywhere is away.
var (
	freezeFirst = time.Date(1, 1, 2, 0, 0, 0, 0, time.UTC)
	freezeLast  = time.Date(9999, 12, 30, 23, 59, 59, 999999000, time.UTC)
)

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
	t, ok := parseFreezeInstant(v)
	var zerr error
	if !ok {
		t, ok, zerr = parseFreezeClockZone(v, time.Now())
	}
	// A trailing word that reads as an attempted zone, and fails to resolve as
	// one, is worth resolveZone's own reason rather than the generic message
	// below: parseFreezeClockZone only returns an error once a date and a
	// clock have already parsed, so the word really was meant as a zone.
	if zerr != nil {
		return time.Time{}, false, fmt.Errorf("CLOCK_FREEZE: %s", zerr)
	}
	// Year 0 is a spelling rather than a range: Go's time has one and Python's
	// datetime does not, so datetime cannot construct it at all, and this is
	// the message that gets -- where parseFreezeInstant alone would have
	// accepted it.
	if !ok || t.Year() < 1 {
		return time.Time{}, false, fmt.Errorf(
			"CLOCK_FREEZE wants an instant like 2026-07-15T09:53:07.123456Z "+
				"(the fraction and its digit count are optional, down to none), "+
				"a clock time like 15:30 UTC or 15:30 PT (nearest day filled in), or a "+
				"dated one like 2026-07-22 15:30 UTC or 7/22 15:30 PT (nearest year "+
				"filled in when it's left out), got \"%s\"", v)
	}
	// The ends of the range, which nothing above can see. See freezeFirst.
	if t.Before(freezeFirst) || t.After(freezeLast) {
		return time.Time{}, false, fmt.Errorf(
			"CLOCK_FREEZE wants an instant from 0001-01-02 to 9999-12-30, got \"%s\"", v)
	}
	return t, true, nil
}

// freezeDigits reads s as an unsigned decimal integer, requiring every byte
// to be an ASCII digit and s to be non-empty. Hand-scanned like parseCount
// and parsePad, for the same reason: strconv.Atoi takes a leading sign where
// this must not, and the two ports have to accept exactly the same strings.
func freezeDigits(s string) (int, bool) {
	if s == "" {
		return 0, false
	}
	n := 0
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c < '0' || c > '9' {
			return 0, false
		}
		n = n*10 + int(c-'0')
	}
	return n, true
}

// parseFreezeInstant reads the strict ISO instant CLOCK_FREEZE accepts: a
// four-digit year, two-digit month and day, two-digit hour, minute and
// second, an optional one-to-six-digit fraction, and a literal Z. Hand-scanned
// rather than handed to time.Parse -- which takes a one-digit month, where
// strptime takes fewer than six fractional digits -- so that the shape
// accepted is controlled entirely in this file, and pyclock.py's version of
// this function can be made to agree with it deliberately rather than by
// coincidence.
//
// Rather than range-checking month, day, hour, minute and second by hand,
// the parsed fields are handed to time.Date and read back: an invalid date
// like day 30 of February does not error there, it normalizes to March 2,
// so the read-back values differ from what was typed and the round trip
// catches it exactly the way datetime's constructor, which raises instead of
// normalizing, catches it on the Python side.
func parseFreezeInstant(v string) (time.Time, bool) {
	if len(v) < 20 || v[len(v)-1] != 'Z' {
		return time.Time{}, false
	}
	core := v[:len(v)-1]
	fracDigits := ""
	switch {
	case len(core) == 19:
		// no fraction
	case len(core) >= 21 && len(core) <= 26 && core[19] == '.':
		fracDigits = core[20:]
		core = core[:19]
	default:
		return time.Time{}, false
	}
	if core[4] != '-' || core[7] != '-' || core[10] != 'T' || core[13] != ':' || core[16] != ':' {
		return time.Time{}, false
	}
	year, ok1 := freezeDigits(core[0:4])
	month, ok2 := freezeDigits(core[5:7])
	day, ok3 := freezeDigits(core[8:10])
	hour, ok4 := freezeDigits(core[11:13])
	minute, ok5 := freezeDigits(core[14:16])
	second, ok6 := freezeDigits(core[17:19])
	if !ok1 || !ok2 || !ok3 || !ok4 || !ok5 || !ok6 {
		return time.Time{}, false
	}
	frac := 0
	if fracDigits != "" {
		var ok bool
		frac, ok = freezeDigits(fracDigits)
		if !ok {
			return time.Time{}, false
		}
	}
	nanos := frac
	for i := len(fracDigits); i < 9; i++ {
		nanos *= 10
	}
	t := time.Date(year, time.Month(month), day, hour, minute, second, nanos, time.UTC)
	if t.Year() != year || int(t.Month()) != month || t.Day() != day ||
		t.Hour() != hour || t.Minute() != minute || t.Second() != second {
		return time.Time{}, false
	}
	return t, true
}

// parseFreezeClockZone reads a clock time in some zone, with an optional
// date ahead of it -- "15:30 UTC", "15:30 PT", "2026-07-22 15:30 UTC",
// "2026/07/22 15:30 UTC" or "7/22 15:30 PT" -- filling in whatever the date
// left out: no date at all leaves the day itself open, and a date with no
// year leaves the year open. What is left open resolves to whichever
// candidate, by the wall clock right now, lands closest to this instant --
// the reading needs no date, or no year, typed at all for the moment that is
// happening soon, whichever side of midnight or new year's it falls on, in
// that zone's own calendar. The zone is anything resolveZone accepts: an
// alias, an IANA name, a fixed offset abbreviation, a country code, a US
// state, a city -- though a place with a space in its name is written with
// underscores here, New_Mexico, since a space is what separates the date, the
// clock and the zone.
//
// ok is false and err nil when the value is not shaped like this format at
// all -- one space (a clock and a zone) or two (a date as well) -- so freeze
// can fall back to its own generic message. err is non-nil only once the
// shape is unmistakably this one and the trailing word fails to resolve as a
// zone, since resolveZone's own reason is worth more than a generic one at
// that point.
func parseFreezeClockZone(v string, now time.Time) (time.Time, bool, error) {
	tokens := strings.Split(v, " ")
	var datePart, clockPart, zonePart string
	switch len(tokens) {
	case 2:
		clockPart, zonePart = tokens[0], tokens[1]
	case 3:
		datePart, clockPart, zonePart = tokens[0], tokens[1], tokens[2]
	default:
		return time.Time{}, false, nil
	}
	hour, minute, second, ok := parseFreezeClock(clockPart)
	if !ok {
		return time.Time{}, false, nil
	}
	loc, _, err := resolveZone(zonePart, now)
	if err != nil {
		return time.Time{}, false, err
	}
	if datePart == "" {
		return freezeClosestDay(now, loc, hour, minute, second), true, nil
	}
	year, month, day, hasYear, ok := parseFreezeDate(datePart)
	if !ok {
		return time.Time{}, false, nil
	}
	if hasYear {
		t, ok := freezeDate(loc, year, month, day, hour, minute, second)
		return t, ok, nil
	}
	t, ok := freezeClosestYear(now, loc, month, day, hour, minute, second)
	return t, ok, nil
}

// parseFreezeClock reads just the HH[:MM[:SS]] half of parseFreezeClockUTC.
func parseFreezeClock(s string) (hour, minute, second int, ok bool) {
	fields := strings.Split(s, ":")
	if len(fields) > 3 || len(fields[0]) == 0 || len(fields[0]) > 2 {
		return 0, 0, 0, false
	}
	hour, ok = freezeDigits(fields[0])
	if !ok || hour > 23 {
		return 0, 0, 0, false
	}
	if len(fields) >= 2 {
		if len(fields[1]) != 2 {
			return 0, 0, 0, false
		}
		if minute, ok = freezeDigits(fields[1]); !ok || minute > 59 {
			return 0, 0, 0, false
		}
	}
	if len(fields) == 3 {
		if len(fields[2]) != 2 {
			return 0, 0, 0, false
		}
		if second, ok = freezeDigits(fields[2]); !ok || second > 59 {
			return 0, 0, 0, false
		}
	}
	return hour, minute, second, true
}

// parseFreezeDate reads the date ahead of a clock time -- "2026-07-22",
// "2026/07/22", "7/22" or "8/22/26" -- returning its fields and whether a
// year was given. The two separators inside one date must be the same
// character; mixing them, as in "2026-07/22", falls out of the two-field
// case rather than being caught on purpose, since the year then reads as a
// two-digit month and is refused for being neither.
//
// Three fields read year-month-day when the first is four digits -- the only
// length a year is ever spelled with here -- and month-day-year otherwise,
// with the year itself two digits (2000 added) or four. A first field of
// three digits is neither and is refused either way.
func parseFreezeDate(s string) (year, month, day int, hasYear, ok bool) {
	sep := byte(0)
	for i := 0; i < len(s); i++ {
		if s[i] == '-' || s[i] == '/' {
			sep = s[i]
			break
		}
	}
	if sep == 0 {
		return 0, 0, 0, false, false
	}
	fields := strings.Split(s, string(sep))
	if len(fields) == 2 {
		if len(fields[0]) == 0 || len(fields[0]) > 2 || len(fields[1]) == 0 || len(fields[1]) > 2 {
			return 0, 0, 0, false, false
		}
		month, ok1 := freezeDigits(fields[0])
		day, ok2 := freezeDigits(fields[1])
		if !ok1 || !ok2 {
			return 0, 0, 0, false, false
		}
		return 0, month, day, false, true
	}
	if len(fields) != 3 {
		return 0, 0, 0, false, false
	}
	if len(fields[0]) == 4 {
		if len(fields[1]) == 0 || len(fields[1]) > 2 || len(fields[2]) == 0 || len(fields[2]) > 2 {
			return 0, 0, 0, false, false
		}
		year, ok1 := freezeDigits(fields[0])
		month, ok2 := freezeDigits(fields[1])
		day, ok3 := freezeDigits(fields[2])
		if !ok1 || !ok2 || !ok3 {
			return 0, 0, 0, false, false
		}
		return year, month, day, true, true
	}
	if len(fields[0]) == 1 || len(fields[0]) == 2 {
		if len(fields[1]) == 0 || len(fields[1]) > 2 {
			return 0, 0, 0, false, false
		}
		if len(fields[2]) != 2 && len(fields[2]) != 4 {
			return 0, 0, 0, false, false
		}
		month, ok1 := freezeDigits(fields[0])
		day, ok2 := freezeDigits(fields[1])
		year, ok3 := freezeDigits(fields[2])
		if !ok1 || !ok2 || !ok3 {
			return 0, 0, 0, false, false
		}
		if len(fields[2]) == 2 {
			year += 2000
		}
		return year, month, day, true, true
	}
	return 0, 0, 0, false, false
}

// freezeValidCalendarDate reports whether year-month-day is a real date,
// independent of any zone -- day 30 of February is not, whatever clock time
// or zone rides along with it. time.Date normalizes such a day forward
// rather than erroring, so the fields are read back and compared to what was
// typed; parseFreezeInstant catches the same thing the same way.
func freezeValidCalendarDate(year, month, day int) bool {
	t := time.Date(year, time.Month(month), day, 0, 0, 0, 0, time.UTC)
	return t.Year() == year && int(t.Month()) == month && t.Day() == day
}

// freezeDate builds one specific instant in loc, rejecting a day that does
// not exist in that month before asking what UTC instant it names in that
// zone.
func freezeDate(loc *time.Location, year, month, day, hour, minute, second int) (time.Time, bool) {
	if !freezeValidCalendarDate(year, month, day) {
		return time.Time{}, false
	}
	return freezeLocalToUTC(loc, year, month, day, hour, minute, second), true
}

// freezeClosestDay resolves an undated clock time in loc to whichever of
// yesterday, today or tomorrow -- by loc's own calendar, not UTC's -- lands
// closest to now. A tie favors today: today is checked first and only a
// strictly closer candidate replaces it.
func freezeClosestDay(now time.Time, loc *time.Location, hour, minute, second int) time.Time {
	y, m, d := now.In(loc).Date()
	best := freezeLocalToUTC(loc, y, int(m), d, hour, minute, second)
	bestDiff := freezeAbs(best.Sub(now))
	for _, days := range [2]int{-1, 1} {
		// Calendar arithmetic only, in a fixed UTC-flagged time.Time used
		// purely as a date calculator; the zone that matters is applied after,
		// by freezeLocalToUTC.
		cd := time.Date(y, m, d, 0, 0, 0, 0, time.UTC).AddDate(0, 0, days)
		cand := freezeLocalToUTC(loc, cd.Year(), int(cd.Month()), cd.Day(), hour, minute, second)
		if diff := freezeAbs(cand.Sub(now)); diff < bestDiff {
			best, bestDiff = cand, diff
		}
	}
	return best
}

// freezeYearDeltas is how far from now's year freezeClosestYear looks for a
// year the given month and day exist in. Only February 29 can be missing
// from a year at all, and the longest it is ever missing for is eight years
// -- 1900 was not a leap year, between 1896 and 1904, which both were -- so
// searching this far always finds a February 29 if the true closest one lies
// outside the plain +-1 year that every other date is already found within.
var freezeYearDeltas = [17]int{0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5, -6, 6, -7, 7, -8, 8}

// freezeClosestYear resolves a clock time in loc on a month and day with no
// year to whichever year, among freezeYearDeltas away from now's year in
// loc's own calendar, lands closest to now -- skipping a year the day does
// not exist in, which for any month and day but February 29 is none of them.
// ok is false only if no year in range has the day, which for every month
// and day but February 29 means the date does not exist regardless of year.
func freezeClosestYear(now time.Time, loc *time.Location, month, day, hour, minute, second int) (time.Time, bool) {
	y := now.In(loc).Year()
	var best time.Time
	var bestDiff time.Duration
	found := false
	for _, delta := range freezeYearDeltas {
		cy := y + delta
		if !freezeValidCalendarDate(cy, month, day) {
			continue
		}
		cand := freezeLocalToUTC(loc, cy, month, day, hour, minute, second)
		if diff := freezeAbs(cand.Sub(now)); !found || diff < bestDiff {
			best, bestDiff, found = cand, diff, true
		}
	}
	return best, found
}

// freezeLocalToUTC converts a wall-clock reading -- already known to be a
// real calendar date -- in loc to the UTC instant it names, deciding a
// daylight-saving edge case by hand rather than leaning on time.Date's own
// choice: time.Date and Python's zoneinfo do not agree with each other on a
// reading a spring-forward skips entirely, so this is the one place that
// disagreement could reach the frame drawn, and both ports implement this
// same explicit rule instead.
//
// The offset a full day before and a full day after settle it. Equal, there
// is no transition anywhere near this reading and the obvious instant is the
// answer -- true on all but at most two calls a year, per zone. Unequal, one
// transition sits somewhere in that two-day window; both candidate instants,
// one built from each offset, are checked by converting back into loc and
// comparing against what was asked for. Both matching is a fall-back reading
// that happened twice, resolved to the earlier of the two -- the offset
// still in effect right up to the transition. Neither matching is a
// spring-forward reading that never happened at all, resolved as though the
// spring-forward had already gone -- the later offset.
func freezeLocalToUTC(loc *time.Location, year, month, day, hour, minute, second int) time.Time {
	naive := time.Date(year, time.Month(month), day, hour, minute, second, 0, time.UTC)
	_, offPrev := naive.Add(-24 * time.Hour).In(loc).Zone()
	_, offNext := naive.Add(24 * time.Hour).In(loc).Zone()
	if offPrev == offNext {
		return naive.Add(-time.Duration(offPrev) * time.Second)
	}
	candPrev := naive.Add(-time.Duration(offPrev) * time.Second)
	candNext := naive.Add(-time.Duration(offNext) * time.Second)
	validPrev := freezeReproduces(candPrev, loc, year, month, day, hour, minute, second)
	validNext := freezeReproduces(candNext, loc, year, month, day, hour, minute, second)
	switch {
	case validPrev && validNext:
		if candPrev.Before(candNext) {
			return candPrev
		}
		return candNext
	case validPrev:
		return candPrev
	default:
		return candNext
	}
}

// freezeReproduces reports whether t, read back in loc, is exactly the wall
// clock freezeLocalToUTC was asked to convert.
func freezeReproduces(t time.Time, loc *time.Location, year, month, day, hour, minute, second int) bool {
	lt := t.In(loc)
	return lt.Year() == year && int(lt.Month()) == month && lt.Day() == day &&
		lt.Hour() == hour && lt.Minute() == minute && lt.Second() == second
}

// freezeAbs is time.Duration's absolute value, which the standard library
// does not offer.
func freezeAbs(d time.Duration) time.Duration {
	if d < 0 {
		return -d
	}
	return d
}

// envWhole reads one whole number out of the environment, or the default if it
// is unset.
//
// Unlike CLOCK_CELL_RATIO, a bad value here is a hard error rather than
// something to shrug off: these are the diff harness's knobs, and a typo that
// quietly fell back to the default would leave a test claiming to cover a
// sequence it never drew. Nine digits at most, so that Atoi and Python's
// unbounded int accept exactly the same strings.
func envWhole(name string, lowest, def int) (int, error) {
	v := os.Getenv(name)
	if v == "" {
		return def, nil
	}
	n, err := strconv.Atoi(v)
	if err == nil && allDigits(v) && len(v) <= 9 && n >= lowest {
		return n, nil
	}
	return 0, fmt.Errorf(
		"%s wants a whole number of %d or more, at most nine digits, got \"%s\"", name, lowest, v)
}

// sequence is how many frames a pinned clock draws, and how far the instant
// moves between them.
//
// CLOCK_FRAMES draws that many instead of one, stepping the pinned instant by
// CLOCK_STEP milliseconds each time -- one tick by default, so the sequence
// advances exactly as a live clock would. It is what lets the harness compare
// what a single frame cannot show: the second hand sweeping, the rewind that
// repaints over the frame before it, and the faces regrouping as a zone
// crosses a daylight-saving boundary.
//
// Only where a pinned clock already draws and exits, which is redirected; on a
// terminal one frame still stays up, so this cannot animate what is meant to
// hold still. Dev hook, not in --help.
func sequence(pinned bool) (int, time.Duration, error) {
	frames, err := envWhole("CLOCK_FRAMES", 1, 1)
	if err != nil {
		return 0, 0, err
	}
	step, err := envWhole("CLOCK_STEP", 0, int(tick/time.Millisecond))
	if err != nil {
		return 0, 0, err
	}
	if !pinned && (frames != 1 || os.Getenv("CLOCK_STEP") != "") {
		return 0, 0, fmt.Errorf(
			"CLOCK_FRAMES and CLOCK_STEP need CLOCK_FREEZE, the instant they step from")
	}
	return frames, time.Duration(step) * time.Millisecond, nil
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

// asciiLower lowercases A-Z and leaves everything else exactly as it is.
//
// Zone names, country codes and the abbreviations are all ASCII, so none of
// the matching here wants Unicode's rules -- which is as well, since the two
// languages do not have the same ones. Go applies the simple case mappings
// where Python applies the full ones, and they part company on U+0130, the
// Turkish dotted capital I: Go gives it a plain i, Python an i and a combining
// dot. So "Istanbul" spelt with one drew a clock here and was refused as an
// unknown zone there.
func asciiLower(s string) string {
	b := []byte(s)
	for i, c := range b {
		if c >= 'A' && c <= 'Z' {
			b[i] = c + 'a' - 'A'
		}
	}
	return string(b)
}

// asciiUpper uppercases a-z and leaves everything else alone; see asciiLower.
func asciiUpper(s string) string {
	b := []byte(s)
	for i, c := range b {
		if c >= 'a' && c <= 'z' {
			b[i] = c - 'a' + 'A'
		}
	}
	return string(b)
}

// asciiEqualFold is strings.EqualFold with the same ASCII-only reach.
func asciiEqualFold(a, b string) bool {
	return asciiLower(a) == asciiLower(b)
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
	// to a single dot at r1, instead of ending in a flat, two-dot-wide butt --
	// that is a hand. A thick spoke without point is a plain parallel-sided
	// band the same width all the way to r1 -- that is always one of the four
	// major hour ticks (h = 0, 3, 6, 9), always exactly axis-aligned, always
	// beside a numeral.
	spoke := func(angle, r0, r1 float64, thick, point bool, layer uint8) {
		sin, cos := math.Sin(angle), math.Cos(angle)
		x0, y0 := cx+rx*r0*sin, cy-ry*r0*cos
		x1, y1 := cx+rx*r1*sin, cy-ry*r1*cos

		if thick && point {
			tip := r1 - (r1-r0)*handTaper
			tx, ty := cx+rx*tip*sin, cy-ry*tip*cos
			for _, off := range []float64{-0.5, 0.5} {
				dx, dy := off*cos, off*sin
				// the offset shrinks to nothing at the tip, not the base:
				// that is what tapers the two edges together into a point
				c.line(x0+dx, y0+dy, tx, ty, layer)
			}
			c.line(tx, ty, x1, y1, layer)
			return
		}
		if !thick {
			c.line(x0, y0, x1, y1, layer)
			return
		}

		// A symmetric +-0.5 offset here would straddle a character cell
		// boundary about half the time -- whichever side of a 2-or-4-dot
		// cell the true centre's neighbouring dot falls on -- splitting the
		// tick's two lines into different rows or columns and making it
		// look disjointed from the numeral beside it. Landing both dots in
		// the same cell as the numeral's own dot instead costs at most half
		// a dot of true centring, invisible, for a tick that always reads
		// as attached to its numeral, which is not.
		horizontal := math.Abs(cos) < 0.5
		cellSize, center := 4, cy
		if !horizontal {
			cellSize, center = 2, cx
		}
		step := -1.0
		if int(math.Floor(center+0.5))%cellSize == 0 {
			step = 1
		}
		for _, s := range []float64{0, step} {
			if horizontal {
				c.line(x0, y0+s, x1, y1+s, layer)
			} else {
				c.line(x0+s, y0, x1+s, y1, layer)
			}
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

// errVersion asks the caller to print the version and stop, successfully.
var errVersion = errors.New("version requested")

// errPipe says the reader went away mid-frame -- `clock | head`. It is not a
// failure to report, only a reason to stop; main turns it back into the signal
// death a filter is expected to end with, once the defers have given the
// terminal back. See main.
var errPipe = errors.New("broken pipe")

// pipeStatus is what both ports exit with when the reader goes away: 128 plus
// SIGPIPE, which is what a shell reports for a filter that died of it.
const pipeStatus = 141

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
// space, since pyclock.py hand-scans the same digits.
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
		return 0, fmt.Errorf("--cell-ratio wants a positive number up to 1000000, "+
			"e.g. --cell-ratio 2.6, got \"%s\"", val)
	}
	return v, nil
}

// parseScale reads a positive, finite decimal for --scale.
func parseScale(val string) (float64, error) {
	v, ok := positiveFloat(val)
	if !ok {
		return 0, fmt.Errorf("--scale wants auto or a positive number up to 1000000, "+
			"e.g. --scale 1.5, got \"%s\"", val)
	}
	return v, nil
}

// numberChars is what a number may be spelled with here, which is the
// intersection of what the two languages read rather than what either offers.
// ParseFloat takes a hexadecimal float, "0x1p2", where Python's float() does
// not; float() takes surrounding whitespace and non-ASCII digits -- "\u0661"
// and "\uff11" are both a one to it -- where ParseFloat takes neither. Every
// one of those was a clock drawn by one implementation and a complaint printed
// by the other. What is left after this is read identically by both,
// underscores and exponents included, so the parse itself can still be each
// language's own.
const numberChars = "0123456789+-._eE"

// numberMax is a ceiling on the two knobs that scale a face, which is not
// about taste: the face's width is an integer derived from them, and Go's
// integers are 64 bits where Python's are unbounded. At --cell-ratio 1e19 one
// clock face needed 9223372036854775807 columns here and
// 400000000000000000000 there -- the same refusal, in two different numbers. A
// million is past any font's aspect ratio and any terminal's width, and leaves
// the arithmetic identical either side.
const numberMax = 1000000

// positiveFloat is what --cell-ratio, --scale and CLOCK_CELL_RATIO share: the
// same question asked three times, so a value one of them takes cannot be a
// value another refuses.
func positiveFloat(val string) (float64, bool) {
	if val == "" {
		return 0, false
	}
	for i := 0; i < len(val); i++ {
		if strings.IndexByte(numberChars, val[i]) < 0 {
			return 0, false
		}
	}
	v, err := strconv.ParseFloat(val, 64)
	if err != nil || math.IsNaN(v) || math.IsInf(v, 0) || v <= 0 || v > numberMax {
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

// settings is the six tunables parsed: what the frame loop actually lays out
// with. Read back from the text with readTunes whenever the text changes --
// at startup, and after every value the tuner takes.
type settings struct {
	geo        geometry
	cellRatio  float64
	scaleAuto  bool
	rows       int // the face height a fixed --scale asks for; unused when scaleAuto
	perRow     int // the cap an explicit -n asks for; unused when perRowAuto
	perRowAuto bool
}

// rowsForScale turns a --scale into a face height, and refuses one that would
// leave the numerals nowhere to sit or ask for a canvas the size of a wall.
// The same answer at startup and under the tuner: a scale typed into the
// tuner is refused in the words the flag would have used.
func rowsForScale(scale float64) (int, error) {
	rows := int(math.Floor(defaultRowsN*scale + 0.5))
	if rows < minRowsN || rows > maxRowsN {
		return 0, fmt.Errorf(
			"--scale %s makes each face %d rows tall; want %d to %d rows, roughly --scale %.2f to --scale %.2f",
			trimFloat(scale), rows, minRowsN, maxRowsN,
			float64(minRowsN)/defaultRowsN, float64(maxRowsN)/defaultRowsN)
	}
	return rows, nil
}

// checkTune reads one tunable's text exactly as its flag reads it, and is the
// only gate the tuner has: what survives this is stored as text and read back
// by readTunes, which therefore cannot fail.
func checkTune(i int, val string) error {
	switch i {
	case tuneHalign:
		_, err := parseChoice("halign", val, haligns)
		return err
	case tuneValign:
		_, err := parseChoice("valign", val, valigns)
		return err
	case tuneHpad, tuneVpad:
		_, err := parsePad(tunables[i].name, val)
		return err
	case tunePerRow:
		if val == "auto" {
			return nil
		}
		_, err := parseCount(val, "--per-row", maxPerRow)
		return err
	case tuneScale:
		if val == "auto" {
			return nil
		}
		s, err := parseScale(val)
		if err != nil {
			return err
		}
		_, err = rowsForScale(s)
		return err
	default:
		_, err := parseRatio(val)
		return err
	}
}

// canonTune is how a value is written down once it has been accepted: a pad
// typed as "5" and one typed as "5%" are the same share, and a count typed as
// "007" is three faces a row, so the tuner, the saved file and the line it
// prints all say the one spelling. The decimals are left exactly as typed --
// rewriting 2.15 as 2.2 would be rounding a value nobody asked to round.
func canonTune(i int, val string) string {
	switch i {
	case tuneHpad, tuneVpad:
		n, err := parsePad(tunables[i].name, val)
		if err != nil || n < 0 {
			return val
		}
		return strconv.Itoa(n) + "%"
	case tunePerRow:
		if val == "auto" {
			return val
		}
		n, err := parseCount(val, "--per-row", maxPerRow)
		if err != nil {
			return val
		}
		return strconv.Itoa(n)
	}
	return val
}

// readTunes parses the six back into a settings. Every value has been through
// checkTune, so nothing here can fail; anything that did would be a value
// stored without being checked, which is a bug rather than a bad input.
func readTunes(vals [numTunes]string) settings {
	set := settings{
		geo:        geometry{hpad: -1, vpad: -1},
		cellRatio:  defaultCellRatio,
		scaleAuto:  true,
		perRow:     defaultPerRow,
		perRowAuto: true,
	}
	set.geo.halign = vals[tuneHalign]
	set.geo.valign = vals[tuneValign]
	set.geo.hpad, _ = parsePad("hpad", vals[tuneHpad])
	set.geo.vpad, _ = parsePad("vpad", vals[tuneVpad])
	if r, ok := positiveFloat(vals[tuneCellRatio]); ok {
		set.cellRatio = r
	}
	if vals[tunePerRow] != "auto" {
		if n, err := parseCount(vals[tunePerRow], "--per-row", maxPerRow); err == nil {
			set.perRow, set.perRowAuto = n, false
		}
	}
	if vals[tuneScale] != "auto" {
		if s, ok := positiveFloat(vals[tuneScale]); ok {
			set.scaleAuto = false
			set.rows, _ = rowsForScale(s)
		}
	}
	return set
}

// apply puts a settings into the globals the drawing code reads. A fixed
// scale sizes the face here and for good; --scale auto leaves rowsN to the
// per-frame search, which reads cellRatio itself.
func (set settings) apply() {
	cellRatio = set.cellRatio
	if !set.scaleAuto {
		rowsN = set.rows
		colsN = int(math.Floor(float64(rowsN)*cellRatio + 0.5))
	}
}

// trimFloat writes a float the way both ports write it, which is not what
// either language's shortest form does: %g and :g part company past six
// significant digits. Only the scale error needs it -- everything else the
// reader sees is the text they typed.
func trimFloat(v float64) string {
	s := strconv.FormatFloat(v, 'g', 6, 64)
	return s
}

// options is everything a command line sets, and so everything the
// preferences file can set too: the file is read by the same parser, into the
// same struct, before the command line is read into it on top. That is the
// whole of "the file is the front of your command line" -- last wins, with no
// second set of rules to keep in step with the first.
type options struct {
	zoneList  string
	colorWhen string
	dayWhen   string
	quiet     bool
	vals      [numTunes]string
}

// defaultOptions is a clock nobody has told anything: the flag defaults, and
// the six knobs as defaultTunes has them.
func defaultOptions() options {
	return options{
		colorWhen: "auto",
		dayWhen:   "", // unset: run() picks it, since a pinned clock differs
		vals:      defaultTunes(),
	}
}

// configPath is where S saves and startup reads. CLOCK_CONFIG names it
// outright -- set but empty means no preferences file at all, which is what
// every harness here runs with and what a script wanting the plain defaults
// can set. Otherwise the usual place, and nowhere at all if even HOME is
// unset.
func configPath() string {
	if v, ok := os.LookupEnv("CLOCK_CONFIG"); ok {
		return v
	}
	if dir := os.Getenv("XDG_CONFIG_HOME"); dir != "" {
		return filepath.Join(dir, "clock", "config")
	}
	if home := os.Getenv("HOME"); home != "" {
		return filepath.Join(home, ".config", "clock", "config")
	}
	return ""
}

// loadConfig reads the file into the argument list it is: one token per line,
// blank lines and # lines skipped. One token per line rather than a line of
// arguments is what keeps a zone list with a space in it -- Salt Lake City --
// from needing quoting rules that the two ports would then have to agree
// about.
//
// A file that is not there is not an error: it is a clock that has never been
// asked to save. One that cannot be read is, and says so in words of its own
// rather than the language's, since Go and Python spell that complaint
// differently and difftest compares the two.
func loadConfig(path string) ([]string, error) {
	if path == "" {
		return nil, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil, nil
		}
		return nil, fmt.Errorf("cannot read %s", path)
	}
	var tokens []string
	for _, line := range strings.Split(string(data), "\n") {
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		tokens = append(tokens, line)
	}
	return tokens, nil
}

// saveConfig writes the tokens back, through a temporary file in the same
// directory so a save that fails part way leaves the old file rather than
// half of a new one.
func saveConfig(path string, tokens []string) error {
	if path == "" {
		return errors.New("CLOCK_CONFIG is empty: nowhere to save")
	}
	fail := fmt.Errorf("cannot write %s", path)
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fail
	}
	tmp, err := os.CreateTemp(dir, "config-")
	if err != nil {
		return fail
	}
	body := configHeader + strings.Join(tokens, "\n")
	if len(tokens) > 0 {
		body += "\n"
	}
	_, err = tmp.WriteString(body)
	if cerr := tmp.Close(); err == nil {
		err = cerr
	}
	if err == nil {
		err = os.Rename(tmp.Name(), path)
	}
	if err != nil {
		os.Remove(tmp.Name())
		return fail
	}
	return nil
}

// configHeader says what the file is to whoever opens it, and that editing it
// is allowed -- it is a command line, and everything the command line takes it
// takes.
const configHeader = "# clock: written by S, read at startup. One argument per line.\n" +
	"# Delete this file to forget it; CLOCK_CONFIG= ignores it.\n"

// parseArgs reads the command line: one optional zone list, and the flags in
// any position. Hand-rolled rather than package flag, which insists every
// flag precede the first positional -- "clock ET,PT -n 2" would silently
// ignore the -n. pyclock.py runs the same algorithm for the same reason.
func parseArgs(argv []string, opt options) (out options, err error) {
	out = opt
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
			err = errHelp
			return
		case strings.HasPrefix(a, "--"):
			name, val, haveVal := strings.Cut(a[2:], "=")
			switch name {
			case "per-row":
				if !haveVal {
					i++
					if i >= len(argv) {
						err = errors.New("--per-row needs a number, e.g. --per-row 2")
						return
					}
					val = argv[i]
				}
				// Kept as text alongside the layout knobs, which is where
				// the tuner reads it from; the count is parsed here all the
				// same, so the complaint is --per-row's own.
				if val != "auto" {
					if _, err = parseCount(val, "--per-row", maxPerRow); err != nil {
						return
					}
				}
				out.vals[tunePerRow] = canonTune(tunePerRow, val)
			case "color":
				// Bare --color means always, and takes no separate argument:
				// "clock --color ET" names a zone list, exactly as ls and git
				// read the same flag. The value only ever follows an "=".
				out.colorWhen = "always"
				if haveVal {
					if out.colorWhen, err = parseChoice("color", val, colorWhens); err != nil {
						return
					}
				}
			case "no-color":
				if haveVal {
					err = errors.New("--no-color takes no value")
					return
				}
				out.colorWhen = "never"
			case "version":
				// Read where it is found, like --help: everything before it on
				// the command line still has to parse, everything after it is
				// never looked at.
				if haveVal {
					err = errors.New("--version takes no value")
					return
				}
				err = errVersion
				return
			case "day":
				out.dayWhen = "always"
				if haveVal {
					if out.dayWhen, err = parseChoice("day", val, whens); err != nil {
						return
					}
				}
			case "no-day":
				if haveVal {
					err = errors.New("--no-day takes no value")
					return
				}
				out.dayWhen = "never"
			case "quiet":
				if haveVal {
					err = errors.New("--quiet takes no value")
					return
				}
				out.quiet = true
			case "halign", "valign", "hpad", "vpad", "cell-ratio", "scale":
				// These six want a value, and take it either way round, as
				// --per-row does: there is no bare form to be ambiguous with.
				if !haveVal {
					i++
					if i >= len(argv) {
						err = fmt.Errorf(
							"--%s needs a value, e.g. %s", name, needs[name])
						return
					}
					val = argv[i]
				}
				// Checked by the flag's own parser and then kept as text, so
				// the tuner and a saved command line say what was typed.
				k := tuneIndex(name)
				if err = checkTune(k, val); err != nil {
					return
				}
				out.vals[k] = canonTune(k, val)
			default:
				err = fmt.Errorf("unknown option: --%s", name)
				return
			}
		case a == "-q":
			out.quiet = true
		case len(a) > 1 && strings.HasPrefix(a, "-"):
			if a[1] != 'n' {
				err = fmt.Errorf("unknown option: %s", a)
				return
			}
			rest := a[2:]
			switch {
			case rest == "":
				i++
				if i >= len(argv) {
					err = errors.New("-n needs a number, e.g. -n 2")
					return
				}
				rest = argv[i]
			case rest[0] == '=':
				err = errors.New("-n takes its value as \"-n N\" or \"-nN\", not \"-n=N\"")
				return
			}
			if rest != "auto" {
				if _, err = parseCount(rest, "-n", maxPerRow); err != nil {
					return
				}
			}
			out.vals[tunePerRow] = canonTune(tunePerRow, rest)
		default:
			positional = append(positional, a)
		}
	}

	if len(positional) > 1 {
		err = fmt.Errorf("expected one comma-separated zone list, got %d: %s",
			len(positional), strings.Join(positional, " "))
		return
	}
	if len(positional) == 1 {
		out.zoneList = positional[0]
	}
	return
}

// A resolved clock face is just its location. No label is stored: it comes off
// the instant at render time, so a face drawn either side of a daylight-saving
// change relabels itself from EST to EDT without being rebuilt.

// zoneAliases fills the gaps the tz database leaves, and only those gaps.
// EST, MST, HST, GMT, CET and EET are real zones with fixed, DST-free
// meanings, so they are looked up verbatim instead: aliasing GMT to
// Europe/London would make it read BST every July, which is simply wrong.
// Sorted, and kept in the same order as pyclock.py's table.
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

// usStates are the fifty states, DC, and the territories with ZIP codes, each
// by the zone its capital keeps, so a state that spans two means the capital's
// -- and the face says which it landed on, MST (Arizona), so a reader in the
// other part is told rather than misled. Washington is the state; the city is
// Washington DC. Two-letter codes are not taken, because CA, IN, DE and GA are
// countries already. New York, Puerto Rico and Guam are not in the table only
// because the tz database names zones after them, and the database is always
// asked first. Sorted, same order as pyclock.py's table.
var usStates = []struct{ name, zone string }{
	{"Alabama", "America/Chicago"},
	{"Alaska", "America/Juneau"},
	{"American Samoa", "Pacific/Pago_Pago"},
	{"Arizona", "America/Phoenix"},
	{"Arkansas", "America/Chicago"},
	{"California", "America/Los_Angeles"},
	{"Colorado", "America/Denver"},
	{"Connecticut", "America/New_York"},
	{"Delaware", "America/New_York"},
	{"District of Columbia", "America/New_York"},
	{"Florida", "America/New_York"},
	{"Georgia", "America/New_York"},
	{"Hawaii", "Pacific/Honolulu"},
	{"Idaho", "America/Boise"},
	{"Illinois", "America/Chicago"},
	{"Indiana", "America/Indiana/Indianapolis"},
	{"Iowa", "America/Chicago"},
	{"Kansas", "America/Chicago"},
	{"Kentucky", "America/New_York"},
	{"Louisiana", "America/Chicago"},
	{"Maine", "America/New_York"},
	{"Maryland", "America/New_York"},
	{"Massachusetts", "America/New_York"},
	{"Michigan", "America/Detroit"},
	{"Minnesota", "America/Chicago"},
	{"Mississippi", "America/Chicago"},
	{"Missouri", "America/Chicago"},
	{"Montana", "America/Denver"},
	{"Nebraska", "America/Chicago"},
	{"Nevada", "America/Los_Angeles"},
	{"New Hampshire", "America/New_York"},
	{"New Jersey", "America/New_York"},
	{"New Mexico", "America/Denver"},
	{"North Carolina", "America/New_York"},
	{"North Dakota", "America/Chicago"},
	{"Northern Mariana Islands", "Pacific/Saipan"},
	{"Ohio", "America/New_York"},
	{"Oklahoma", "America/Chicago"},
	{"Oregon", "America/Los_Angeles"},
	{"Pennsylvania", "America/New_York"},
	{"Rhode Island", "America/New_York"},
	{"South Carolina", "America/New_York"},
	{"South Dakota", "America/Chicago"},
	{"Tennessee", "America/Chicago"},
	{"Texas", "America/Chicago"},
	{"U.S. Virgin Islands", "America/St_Thomas"},
	{"US Virgin Islands", "America/St_Thomas"},
	{"Utah", "America/Denver"},
	{"Vermont", "America/New_York"},
	{"Virginia", "America/New_York"},
	{"Washington", "America/Los_Angeles"},
	{"Washington D.C.", "America/New_York"},
	{"Washington DC", "America/New_York"},
	{"West Virginia", "America/New_York"},
	{"Wisconsin", "America/Chicago"},
	{"Wyoming", "America/Denver"},
}

// commonCities are places people want a clock for that the tz database does
// not name a zone after -- Seattle, Mumbai, Munich. Each was checked against
// GeoNames, and a name shared by cities on different clocks is kept only when
// the largest on this clock is three times the size of any namesake on
// another: so Portland is Oregon, while San Jose, St. Louis, Barcelona and
// Venice are left out. Cities the tz database does name, Los Angeles and Hong
// Kong among them, are left to it. Sorted, same order as pyclock.py's table.
var commonCities = []struct{ name, zone string }{
	{"Abu Dhabi", "Asia/Dubai"},
	{"Abuja", "Africa/Lagos"},
	{"Albuquerque", "America/Denver"},
	{"Ankara", "Europe/Istanbul"},
	{"Atlanta", "America/New_York"},
	{"Austin", "America/Chicago"},
	{"Baltimore", "America/New_York"},
	{"Bangalore", "Asia/Kolkata"},
	{"Beijing", "Asia/Shanghai"},
	{"Bengaluru", "Asia/Kolkata"},
	{"Boston", "America/New_York"},
	{"Brasilia", "America/Sao_Paulo"},
	{"Busan", "Asia/Seoul"},
	{"Calgary", "America/Edmonton"},
	{"Canberra", "Australia/Sydney"},
	{"Cape Town", "Africa/Johannesburg"},
	{"Charlotte", "America/New_York"},
	{"Chengdu", "Asia/Shanghai"},
	{"Chennai", "Asia/Kolkata"},
	{"Christchurch", "Pacific/Auckland"},
	{"Cincinnati", "America/New_York"},
	{"Cleveland", "America/New_York"},
	{"Cologne", "Europe/Berlin"},
	{"Columbus", "America/New_York"},
	{"Dallas", "America/Chicago"},
	{"Delhi", "Asia/Kolkata"},
	{"Durban", "Africa/Johannesburg"},
	{"Edinburgh", "Europe/London"},
	{"El Paso", "America/Denver"},
	{"Florence", "Europe/Rome"},
	{"Fort Worth", "America/Chicago"},
	{"Frankfurt", "Europe/Berlin"},
	{"Geneva", "Europe/Zurich"},
	{"Glasgow", "Europe/London"},
	{"Guadalajara", "America/Mexico_City"},
	{"Guangzhou", "Asia/Shanghai"},
	{"Hamburg", "Europe/Berlin"},
	{"Hanoi", "Asia/Ho_Chi_Minh"},
	{"Houston", "America/Chicago"},
	{"Hyderabad", "Asia/Kolkata"},
	{"Islamabad", "Asia/Karachi"},
	{"Jacksonville", "America/New_York"},
	{"Jeddah", "Asia/Riyadh"},
	{"Kansas City", "America/Chicago"},
	{"Krakow", "Europe/Warsaw"},
	{"Kyoto", "Asia/Tokyo"},
	{"Lahore", "Asia/Karachi"},
	{"Las Vegas", "America/Los_Angeles"},
	{"Lyon", "Europe/Paris"},
	{"Manchester", "Europe/London"},
	{"Marseille", "Europe/Paris"},
	{"Mecca", "Asia/Riyadh"},
	{"Memphis", "America/Chicago"},
	{"Miami", "America/New_York"},
	{"Milan", "Europe/Rome"},
	{"Milwaukee", "America/Chicago"},
	{"Minneapolis", "America/Chicago"},
	{"Montreal", "America/Toronto"},
	{"Mumbai", "Asia/Kolkata"},
	{"Munich", "Europe/Berlin"},
	{"Naples", "Europe/Rome"},
	{"Nashville", "America/Chicago"},
	{"New Delhi", "Asia/Kolkata"},
	{"New Orleans", "America/Chicago"},
	{"New York City", "America/New_York"},
	{"Oklahoma City", "America/Chicago"},
	{"Omaha", "America/Chicago"},
	{"Orlando", "America/New_York"},
	{"Osaka", "Asia/Tokyo"},
	{"Ottawa", "America/Toronto"},
	{"Philadelphia", "America/New_York"},
	{"Pittsburgh", "America/New_York"},
	{"Portland", "America/Los_Angeles"},
	{"Pretoria", "Africa/Johannesburg"},
	{"Pune", "Asia/Kolkata"},
	{"Quebec City", "America/Toronto"},
	{"Raleigh", "America/New_York"},
	{"Rio de Janeiro", "America/Sao_Paulo"},
	{"Rotterdam", "Europe/Amsterdam"},
	{"Sacramento", "America/Los_Angeles"},
	{"Saint Petersburg", "Europe/Moscow"},
	{"Salt Lake City", "America/Denver"},
	{"San Antonio", "America/Chicago"},
	{"San Diego", "America/Los_Angeles"},
	{"San Francisco", "America/Los_Angeles"},
	{"Seattle", "America/Los_Angeles"},
	{"Seville", "Europe/Madrid"},
	{"Shenzhen", "Asia/Shanghai"},
	{"St. Petersburg", "Europe/Moscow"},
	{"Tampa", "America/New_York"},
	{"Tel Aviv", "Asia/Jerusalem"},
	{"The Hague", "Europe/Amsterdam"},
	{"Tucson", "America/Phoenix"},
	{"Wellington", "Pacific/Auckland"},
	{"Yokohama", "Asia/Tokyo"},
}

// usCodes are the ISO 3166-2 codes for the same places -- US-CA, US-NY -- and
// the only short form taken. The bare two letters cannot be: most already mean
// something else here, and most of those mean a different clock, since CA is
// Canada, IN India, DE Germany, and CT and MT are this clock's own Central and
// Mountain. Each code names a row in the table above, or a name the tz database
// answers to itself (US-NY, US-PR, US-GU), and the face is labelled with that
// full name rather than the code. Sorted, same order as pyclock.py's table.
var usCodes = []struct{ code, name string }{
	{"US-AK", "Alaska"},
	{"US-AL", "Alabama"},
	{"US-AR", "Arkansas"},
	{"US-AS", "American Samoa"},
	{"US-AZ", "Arizona"},
	{"US-CA", "California"},
	{"US-CO", "Colorado"},
	{"US-CT", "Connecticut"},
	{"US-DC", "District of Columbia"},
	{"US-DE", "Delaware"},
	{"US-FL", "Florida"},
	{"US-GA", "Georgia"},
	{"US-GU", "Guam"},
	{"US-HI", "Hawaii"},
	{"US-IA", "Iowa"},
	{"US-ID", "Idaho"},
	{"US-IL", "Illinois"},
	{"US-IN", "Indiana"},
	{"US-KS", "Kansas"},
	{"US-KY", "Kentucky"},
	{"US-LA", "Louisiana"},
	{"US-MA", "Massachusetts"},
	{"US-MD", "Maryland"},
	{"US-ME", "Maine"},
	{"US-MI", "Michigan"},
	{"US-MN", "Minnesota"},
	{"US-MO", "Missouri"},
	{"US-MP", "Northern Mariana Islands"},
	{"US-MS", "Mississippi"},
	{"US-MT", "Montana"},
	{"US-NC", "North Carolina"},
	{"US-ND", "North Dakota"},
	{"US-NE", "Nebraska"},
	{"US-NH", "New Hampshire"},
	{"US-NJ", "New Jersey"},
	{"US-NM", "New Mexico"},
	{"US-NV", "Nevada"},
	{"US-NY", "New York"},
	{"US-OH", "Ohio"},
	{"US-OK", "Oklahoma"},
	{"US-OR", "Oregon"},
	{"US-PA", "Pennsylvania"},
	{"US-PR", "Puerto Rico"},
	{"US-RI", "Rhode Island"},
	{"US-SC", "South Carolina"},
	{"US-SD", "South Dakota"},
	{"US-TN", "Tennessee"},
	{"US-TX", "Texas"},
	{"US-UT", "Utah"},
	{"US-VA", "Virginia"},
	{"US-VI", "US Virgin Islands"},
	{"US-VT", "Vermont"},
	{"US-WA", "Washington"},
	{"US-WI", "Wisconsin"},
	{"US-WV", "West Virginia"},
	{"US-WY", "Wyoming"},
}

// countrySynonyms are the names people write where iso3166.tab writes another:
// it says Britain (UK), Czech Republic, Myanmar (Burma), and spells four names
// with a character outside ASCII that has to be typed exactly. Everything else
// a person is likely to write is reached by rule in countrySpellings, so this
// stays a list of exceptions rather than a second copy of the database. It is
// consulted before the file, and so still answers on a machine that has no
// iso3166.tab at all. Sorted, same order as pyclock.py's table.
var countrySynonyms = []struct{ name, code string }{
	{"Aland Islands", "AX"},
	{"Burma", "MM"},
	{"Cote d'Ivoire", "CI"},
	{"Czechia", "CZ"},
	{"Great Britain", "GB"},
	{"Ivory Coast", "CI"},
	{"Myanmar", "MM"},
	{"USA", "US"},
	{"United Kingdom", "GB"},
	{"United States of America", "US"},
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

// tzFile returns one of the tz database's own tables, and whether it was found
// at all. Absent on stripped-down systems, so never fatal: what reads it says
// so instead.
func tzFile(name string) (string, bool) {
	for _, dir := range []string{
		os.Getenv("TZDIR"),
		"/usr/share/zoneinfo",
		"/usr/share/lib/zoneinfo",
		"/usr/lib/locale/TZ",
	} {
		if dir == "" {
			continue
		}
		if data, err := os.ReadFile(dir + "/" + name); err == nil {
			return string(data), true
		}
	}
	return "", false
}

// zoneTab is the table of countries and their zones.
func zoneTab() (string, bool) {
	return tzFile("zone.tab")
}

// countryByName reads iso3166.tab, the database's own list of countries, for
// the code a name stands for and the spelling to label it with. An "&" may be
// written "and", since the tab writes Antigua & Barbuda; nothing else is
// forgiven, so the four names holding a character outside ASCII -- Curacao,
// Reunion, Cote d'Ivoire and the Aland Islands, as the tab does not spell
// them -- have to be typed the way it does, for the reason in ARCHITECTURE's
// Case folding.
func countryByName(token string) (code, name string, ok bool) {
	key := placeKey(token)
	for _, s := range countrySynonyms {
		if placeKey(s.name) == key {
			return s.code, s.name, true
		}
	}
	data, found := tzFile("iso3166.tab")
	if !found {
		return "", "", false
	}
	for _, line := range strings.Split(data, "\n") {
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		f := strings.Split(line, "\t")
		if len(f) < 2 {
			continue
		}
		for _, spelling := range countrySpellings(f[1]) {
			if placeKey(spelling) == key {
				// The spelling that matched, not the file's: someone who
				// wrote South Korea is told South Korea, and is not asked to
				// read Korea (South) inside a pair of parentheses of its own.
				return f[0], spelling, true
			}
		}
	}
	return "", "", false
}

// countrySpellings is every way one of iso3166.tab's names might be written:
// its own, an "&" written out, an "St" written "Saint", and a trailing
// qualifier moved to the front, since the file writes Korea (South) where a
// person writes South Korea. The rules are ASCII and mechanical, so the two
// ports cannot drift over them, and one that invents a spelling nobody types
// -- "Burma Myanmar" -- costs a comparison and reaches nothing.
func countrySpellings(name string) []string {
	out := []string{name}
	add := func(s string) {
		for _, v := range out {
			if v == s {
				return
			}
		}
		out = append(out, s)
	}
	if strings.Contains(name, " & ") {
		add(strings.ReplaceAll(name, " & ", " and "))
	}
	for _, v := range append([]string(nil), out...) {
		if strings.HasPrefix(v, "St ") {
			add("Saint " + strings.TrimPrefix(v, "St "))
		}
	}
	for _, v := range append([]string(nil), out...) {
		if i := strings.Index(v, " ("); i > 0 && strings.HasSuffix(v, ")") {
			add(v[i+2:len(v)-1] + " " + v[:i])
		}
	}
	return out
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
func countryZone(cc, label string, at time.Time) (*time.Location, error) {
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
	// All of them, however many: the list is what the reader has to choose
	// from, and a count of the ones it withheld helps nobody choose.
	return nil, fmt.Errorf("%s spans %d time zones; name one: %s",
		label, len(kept), strings.Join(kept, ", "))
}

// suffixZones is the zones whose name ends with the token as a whole path
// segment: Europe/Berlin for "Berlin", and America/Indiana/Indianapolis for
// either "Indianapolis" or "Indiana/Indianapolis". Whole segments only, so
// "Berl" finds nothing and "York" does not answer for "New_York". A space
// reads as the underscore it stands for, so "New York" finds it all the same.
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
	want := "/" + strings.ReplaceAll(asciiLower(token), " ", "_")
	var out []string
	for _, line := range strings.Split(data, "\n") {
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		fields := strings.Split(line, "\t")
		if len(fields) >= 3 && strings.HasSuffix(asciiLower(fields[2]), want) {
			out = append(out, fields[2])
		}
	}
	return out, true
}

// suffixZone is the one zone named by its tail alone, or nil when nothing
// matches. Ambiguity is refused rather than guessed at: every city in the tz
// database is unique today, but nothing promises it stays that way, and two
// clocks an ocean apart is not a choice to make on the reader's behalf.
//
// The name it returns is placeName's: the part of the zone that was typed.
func suffixZone(token string) (*time.Location, string, error) {
	names, found := suffixZones(token)
	if !found || len(names) == 0 {
		return nil, "", nil
	}
	if len(names) > 1 {
		return nil, "", fmt.Errorf("%s names %d zones; name one in full: %s",
			token, len(names), strings.Join(names, ", "))
	}
	loc, err := time.LoadLocation(names[0])
	if err != nil {
		return nil, "", nil
	}
	return loc, placeName(names[0], token), nil
}

// placeName is the part of zone a token matched, written the way a person
// writes it: the zone's own capitals, and spaces for its underscores. Both
// "new_york" and "New York" show as New York, and Indiana/Indianapolis keeps
// both parts, since that is how much of the name was typed.
func placeName(zone, token string) string {
	parts := strings.Split(zone, "/")
	n := strings.Count(token, "/") + 1
	if n > len(parts) {
		n = len(parts)
	}
	return strings.ReplaceAll(strings.Join(parts[len(parts)-n:], "/"), "_", " ")
}

// placeKey is how a state or city is matched: ASCII case folded, and an
// underscore read as the space it stands for, so New_Mexico and "new mexico"
// both find New Mexico. Nothing else is forgiven -- not runs of spaces, since
// Go and Python do not agree about which characters are spaces.
func placeKey(s string) string {
	return strings.ReplaceAll(asciiLower(s), "_", " ")
}

// placeZone is a US state, its ISO 3166-2 code, or a common city, and the name
// to label it with -- the table's own spelling whatever case the token was
// typed in, and for a code the full name rather than the code.
func placeZone(token string) (*time.Location, string, error) {
	key := placeKey(token)
	named := ""
	for _, c := range usCodes {
		if placeKey(c.code) == key {
			named = c.name
			key = placeKey(named)
			break
		}
	}
	for _, table := range [][]struct{ name, zone string }{usStates, commonCities} {
		for _, p := range table {
			if placeKey(p.name) != key {
				continue
			}
			loc, err := time.LoadLocation(p.zone)
			if err != nil {
				return nil, "", fmt.Errorf("%s means %s, which this system's time zone database lacks", p.name, p.zone)
			}
			return loc, p.name, nil
		}
	}
	if named != "" {
		// US-NY, US-PR and US-GU name the three places the tz database
		// answers to itself, which is why they are not rows above.
		return suffixZone(named)
	}
	return nil, "", nil
}

func unknownZone(token string) error {
	return fmt.Errorf("unknown zone \"%s\"; use an IANA name (Europe/Berlin), "+
		"a city (Berlin, Seattle), a US state (Arizona, US-AZ), an abbreviation (%s), "+
		"a country (Germany, JP), or a US ZIP code",
		token, aliasNames())
}

// localZone is time.Local, once TZ is something both ports read the same way.
//
// Go asks the tz database for whatever TZ names and falls back to UTC when it
// has no such file; Python leaves the question to the C library, which also
// reads the POSIX rule form -- "PST8PDT,M3.2.0,M11.1.0", "<+07>-7", "GMT+5".
// So a POSIX rule makes one clock read Pacific and the other UTC, seven hours
// apart, both of them sure. There is no fixing that from here without writing
// a tzset the standard library does not export, so say so instead: this is the
// Windows message's argument, one environment variable down.
//
// Only a TZ that has to be looked up is checked. Unset, empty (which POSIX
// reads as UTC) and an absolute path all mean the same thing to both.
func localZone() (*time.Location, error) {
	tz, ok := os.LookupEnv("TZ")
	if !ok {
		return time.Local, nil
	}
	tz = strings.TrimPrefix(tz, ":")
	if tz == "" || strings.HasPrefix(tz, "/") {
		return time.Local, nil
	}
	if _, err := time.LoadLocation(tz); err != nil {
		return nil, fmt.Errorf("TZ=\"%s\" is not a zone name, and a POSIX TZ rule is not "+
			"something both clocks read alike; name a zone as an argument instead", tz)
	}
	return time.Local, nil
}

// resolveZone turns one token into a location, and the place it named when it
// named one -- a US state, a city from commonCities, or a city off the end of
// an IANA zone -- which is "" for every other kind of zone. Order matters: the
// alias table is consulted before the tz database only for names the database
// lacks, the fixed-offset table only after it so that real zones win, and
// "local" and "" are intercepted because Go and Python disagree about both --
// LoadLocation("Local") works where ZoneInfo("Local") raises, and
// LoadLocation("") quietly returns UTC where ZoneInfo("") raises. Places come
// last of all, the database's own tails before the tables here.
func resolveZone(token string, at time.Time) (*time.Location, string, error) {
	if strings.HasPrefix(token, "/") || strings.Contains(token, "..") {
		return nil, "", fmt.Errorf("\"%s\" is not a zone name", token)
	}
	if asciiEqualFold(token, "local") {
		loc, err := localZone()
		return loc, "", err
	}
	if allDigits(token) {
		loc, err := ziptz.Location(token)
		if err != nil {
			return nil, "", err
		}
		// A ZIP is a place like any other, and the one whose zone is least
		// guessable of all: 94110 says nothing about Los Angeles by itself.
		return loc, token, nil
	}
	up := asciiUpper(token)
	for _, a := range zoneAliases {
		if a.name == up {
			loc, err := time.LoadLocation(a.zone)
			if err != nil {
				return nil, "", fmt.Errorf("%s means %s, which this system's time zone database lacks", up, a.zone)
			}
			return loc, "", nil
		}
	}
	if loc, err := time.LoadLocation(token); err == nil {
		// A country the database also keeps a zone or a compatibility link
		// under -- Japan, Cuba, Singapore -- resolves there, as it always
		// did, and is labelled with the country all the same. GB and NZ are
		// links as well as codes, and are labelled like every other code
		// rather than being the two that are not.
		if _, name, ok := countryByName(token); ok {
			return loc, name, nil
		}
		if isCountryCode(up) {
			if names, found := countryZones(up); found && len(names) > 0 {
				return loc, up, nil
			}
		}
		return loc, "", nil
	}
	for _, f := range zoneFixed {
		if f.name == up {
			return time.FixedZone(f.name, f.offset), "", nil
		}
	}
	if isCountryCode(up) {
		loc, err := countryZone(up, up, at)
		if err != nil {
			return nil, "", err
		}
		if loc != nil {
			return loc, up, nil
		}
	}
	// Last, so a city can never shadow a name the database itself answers to:
	// the database's own tails first, then the states and cities written here.
	if loc, place, err := suffixZone(token); err != nil || loc != nil {
		return loc, place, err
	}
	if loc, place, err := placeZone(token); err != nil || loc != nil {
		return loc, place, err
	}
	// A country by name, last of all: Georgia is the state, and the country
	// is GE, because the table above is asked first.
	if code, name, ok := countryByName(token); ok {
		loc, err := countryZone(code, name, at)
		if err != nil {
			return nil, "", err
		}
		if loc != nil {
			return loc, name, nil
		}
	}
	return nil, "", unknownZone(token)
}

// request is one zone as it was asked for: the token the user typed, where it
// landed, and the place it named if it named one. The token is carried along
// because mergeZones labels a face with the spellings that asked for it, not
// just the zone it landed on; the place, for the parentheses after that label.
type request struct {
	token string
	loc   *time.Location
	place string
}

// dial is one face after merging: the name written over it, and its zone.
type dial struct {
	label string
	loc   *time.Location
}

// resolveZones turns the comma-separated list into requests, left to right.
func resolveZones(list string, at time.Time) ([]request, error) {
	if list == "" {
		loc, err := localZone()
		if err != nil {
			return nil, err
		}
		return []request{{"", loc, ""}}, nil
	}
	tokens := strings.Split(list, ",")
	out := make([]request, 0, len(tokens))
	for _, tok := range tokens {
		tok = strings.Trim(tok, " \t")
		if tok == "" {
			return nil, fmt.Errorf("empty zone in \"%s\"", list)
		}
		loc, place, err := resolveZone(tok, at)
		if err != nil {
			return nil, err
		}
		out = append(out, request{tok, loc, place})
	}
	return out, nil
}

// zoneLabel is the name written over one face: just the abbreviation, unless
// more than one spelling collapsed onto this face -- then each spelling that
// reads differently is named too, because that is the only place the ambiguity
// is visible. PDT,PDT asked the same question twice and gets one plain answer.
//
// A state or city follows in parentheses whatever else collapsed with it,
// since the zone it landed in is written nowhere else: Boise alone is
// MDT (Boise), and MT,Boise is MDT/MT (Boise). tokens is every spelling that
// asked for this face, plain the ones that named no place, and places the
// names of the ones that did.
func zoneLabel(abbr string, tokens, plain, places []string) string {
	label := abbr
	if len(tokens) >= 2 {
		parts := []string{abbr}
		for _, t := range plain {
			if !asciiEqualFold(t, abbr) {
				parts = append(parts, t)
			}
		}
		label = strings.Join(parts, "/")
	}
	if len(places) > 0 {
		label += " (" + strings.Join(places, ", ") + ")"
	}
	return label
}

// mergeZones collapses zones that show the same wall clock at now into one
// face. Keyed on abbreviation and offset, the same test countryZone uses:
// however two tokens were spelled, and whether or not one resolves onto the
// other, they are one clock if they read alike. That is a property of the
// instant, not of the zones -- PDT and PT are one clock in July and two in
// January -- so this regroups as the clock runs rather than once at startup,
// and a grid crossing a daylight-saving boundary splits itself as it happens.
// The loop calls it once a second, which is as often as its answer can change.
func mergeZones(zones []request, now time.Time) []dial {
	type group struct {
		abbr   string
		tokens []string
		plain  []string
		places []string
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
		if z.token == "" || containsFold(g.tokens, z.token) {
			continue
		}
		g.tokens = append(g.tokens, z.token)
		if z.place == "" {
			g.plain = append(g.plain, z.token)
		} else if !containsFold(g.places, z.place) {
			g.places = append(g.places, z.place)
		}
	}

	out := make([]dial, len(groups))
	for i, g := range groups {
		out[i] = dial{zoneLabel(g.abbr, g.tokens, g.plain, g.places), g.loc}
	}
	return out
}

// containsFold is whether list holds s, ASCII case folded.
func containsFold(list []string, s string) bool {
	for _, v := range list {
		if asciiEqualFold(v, s) {
			return true
		}
	}
	return false
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
// Redone as the clock runs, like the merging: an offset is a property of the
// instant, so a zone entering daylight saving slides a place along. Sorting on
// the offset rather than on the time each face reads is what keeps this from
// also changing at every midnight.
//
// It sorts the slice it is given, where Python's returns a new list; both are
// handed a slice mergeZones has just built, so nothing else can see either.
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

// fitLabel cuts a face's label to n runes the way truncate does, except that a
// label ending in a list of places keeps its closing parenthesis: in a narrow
// cell "MDT (Utah, Colorado, New Mexico)" ends "...)" instead of stopping
// partway through a name with the list left open. A cut that would keep none
// of the list is a plain truncation instead, since "MDT (...)" says less than
// the first letters of the place would.
func fitLabel(label string, n int) string {
	r := []rune(label)
	if len(r) <= n {
		return label
	}
	at := strings.Index(label, " (")
	if at < 0 || !strings.HasSuffix(label, ")") {
		return truncate(label, n)
	}
	open := utf8.RuneCountInString(label[:at]) + len(" (")
	keep := n - len("...)")
	if keep <= open {
		return truncate(label, n)
	}
	return strings.TrimRight(string(r[:keep]), " ,") + "...)"
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

// readoutCols is "02:53:07.123" -- what digital writes under every face, and
// the narrowest a face's column can be however small the face itself gets. A
// face is drawn to whatever colsN the scale asks for, but the readout
// underneath is a fixed twelve characters and cannot be shrunk, so the *cell* a
// face occupies is the wider of the two. Without this a narrow enough window
// lays out by face width and then writes a readout straight past the right
// edge -- which wraps, and a wrapped line desynchronises the rewind exactly as
// fitPerRow exists to prevent. The weekday is the same problem solved the other
// way: at dayCols it is dropped rather than widening every cell to hold it.
const readoutCols = 12

// cellCols is how wide one face's column is: the face, or its readout if that
// is wider.
func cellCols() int {
	if colsN > readoutCols {
		return colsN
	}
	return readoutCols
}

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

// tuner is what r puts up: the six knobs, one of them picked, and whatever is
// being typed into it. It holds no values of its own -- the text it shows is
// the same text the frame loop lays out from, so what is on screen is always
// what is in effect.
type tuner struct {
	open    bool
	sel     int    // which of the six is picked
	edit    string // what has been typed into it, once typing has started
	editing bool
	err     string // the last refusal, in the flag's own words
}

// tuneOptions is the words a knob takes, for the two that take words rather
// than numbers. The lists the flags themselves are checked against, so the
// tuner offers exactly what --halign and --valign accept.
func tuneOptions(i int) []string {
	switch i {
	case tuneHalign:
		return haligns
	case tuneValign:
		return valigns
	}
	return nil
}

// tuneWord is the word a numeric knob takes instead of a number, and which
// sits one step below its smallest value: even for a pad, auto for the scale.
// The cell ratio has none -- there is no "work it out for me" for a font.
func tuneWord(i int) string {
	switch i {
	case tuneHpad, tuneVpad:
		return "even"
	case tuneScale, tunePerRow:
		return "auto"
	}
	return ""
}

// tenthsText writes a count of tenths as a decimal, by hand: 21 is "2.1" and
// 20 is "2". Every number the tuner steps to is built this way rather than
// formatted from a float, which is what keeps the two ports spelling the same
// value the same -- see the note on tunables.
func tenthsText(tenths int) string {
	if tenths%10 == 0 {
		return strconv.Itoa(tenths / 10)
	}
	return strconv.Itoa(tenths/10) + "." + strconv.Itoa(tenths%10)
}

// tuneStep moves a knob one step and returns the text it lands on -- or the
// text it started from, where there is nowhere to go. A word list walks, and
// wraps if asked (which is what space does, and the arrows do not). A pad
// moves a whole percent, since that is what it counts; the scale and the cell
// ratio move a tenth, counted as integer tenths so no float is formatted back
// into text. Below the smallest number is the knob's word, where it has one,
// and above the largest is nothing at all.
//
// Anything it lands on goes through checkTune before it is handed back, so
// stepping cannot reach a value typing would be refused for -- a scale one
// step past what the face may be simply does not move.
func tuneStep(i int, val string, delta int, wrap bool) string {
	if opts := tuneOptions(i); opts != nil {
		at := 0
		for n, o := range opts {
			if o == val {
				at = n
			}
		}
		next := at + delta
		if wrap {
			next = (next + len(opts)) % len(opts)
		}
		if next < 0 || next >= len(opts) {
			return val
		}
		return opts[next]
	}

	word := tuneWord(i)
	if val == word {
		if delta < 0 {
			return val // already below the smallest number there is
		}
		// Back into numbers at the plain default, which is the size, the
		// spacing and the row an untouched clock has.
		switch i {
		case tuneScale:
			return "1"
		case tunePerRow:
			return strconv.Itoa(defaultPerRow)
		}
		return "0%"
	}

	var next string
	if i == tunePerRow {
		n, err := parseCount(val, "--per-row", maxPerRow)
		if err != nil {
			return val
		}
		if n+delta < 1 {
			return word
		}
		next = strconv.Itoa(n + delta)
	} else if i == tuneHpad || i == tuneVpad {
		n, err := parsePad(tunables[i].name, val)
		if err != nil {
			return val
		}
		if n+delta < 0 {
			return word
		}
		next = strconv.Itoa(n+delta) + "%"
	} else {
		v, ok := positiveFloat(val)
		if !ok {
			return val
		}
		tenths := int(math.Floor(v*10+0.5)) + delta
		if tenths <= 0 {
			if word != "" {
				return word
			}
			return val
		}
		next = tenthsText(tenths)
	}
	if checkTune(i, next) != nil {
		// Off the end of what this knob may be. The scale has somewhere to
		// go at the bottom -- auto -- and nowhere at the top.
		if delta < 0 && word != "" {
			return word
		}
		return val
	}
	return next
}

// tuneShow is a knob's value as the box says it: the words it takes with the
// one it holds in brackets, or -- for a number -- the steps either side of it,
// so the size of a step is on screen rather than something to find out by
// pressing a key.
func tuneShow(i int, val string) string {
	if opts := tuneOptions(i); opts != nil {
		out := ""
		for _, o := range opts {
			if out != "" {
				out += " "
			}
			if o == val {
				out += "[" + o + "]"
			} else {
				out += o
			}
		}
		return out
	}
	out := "[" + val + "]"
	if prev := tuneStep(i, val, -1, false); prev != val {
		out = prev + " " + out
	}
	if next := tuneStep(i, val, 1, false); next != val {
		out += " " + next
	}
	return out
}

// tuneRows is the tuner's modal, one row per knob and a footer. The value
// column is the text each knob holds, except the one being typed into, which
// shows the buffer and a cursor -- so an empty buffer still reads as a field
// waiting for something rather than as a value of nothing.
//
// The face height goes beside the scale, since that is the number --scale
// auto is choosing and the one a reader wanting to pin it down needs.
func tuneRows(t tuner, vals [numTunes]string) []string {
	rows := make([]string, 0, len(tunables)+3)
	for i, k := range tunables {
		mark := "  "
		if i == t.sel {
			mark = "> "
		}
		val := tuneShow(i, vals[i])
		if i == t.sel && t.editing {
			val = t.edit + "_"
		}
		if i == tuneScale {
			val += fmt.Sprintf("   (%d rows)", rowsN)
		}
		rows = append(rows, mark+ljust(k.name, tuneCol)+val)
	}
	say := tunables[t.sel].takes
	if t.err != "" {
		say = t.err
	}
	return append(rows, "", say,
		"up down pick   left right or space change",
		"or type a value and enter   esc done")
}

// tuneFlags is what the tuned layout would take on a command line: the knobs
// that differ from an untouched clock's, in the tuner's own order. Nothing is
// formatted here -- these are the strings that were typed.
func tuneFlags(vals [numTunes]string) []string {
	def := defaultTunes()
	var out []string
	for i, k := range tunables {
		if vals[i] != def[i] {
			out = append(out, "--"+k.name, vals[i])
		}
	}
	return out
}

// configTokens is the clock on screen written as an argument list: the knobs
// that differ from an untouched clock's, the -n if one was given, and the zone
// list as it was typed. What S saves, one token per line, and what the line
// the tuner leaves behind is made of.
func configTokens(vals [numTunes]string, zoneList string) []string {
	tokens := tuneFlags(vals)
	if zoneList != "" {
		tokens = append(tokens, zoneList)
	}
	return tokens
}

// tuneCommand is the same list said out loud: quoted for a shell, and empty
// unless some knob was actually moved -- a clock still at its defaults has
// nothing to tell anyone. No program name in front of it, since the two ports
// are installed under different ones and the flags are the part worth copying
// either way.
func tuneCommand(vals [numTunes]string, zoneList string) string {
	if len(tuneFlags(vals)) == 0 {
		return ""
	}
	tokens := configTokens(vals, zoneList)
	quoted := make([]string, len(tokens))
	for i, t := range tokens {
		quoted[i] = shellQuote(t)
	}
	return strings.Join(quoted, " ")
}

// shellQuote wraps a zone list a shell would read as more than one word.
// Single quotes, and a name holding one of those is quoted the long way
// round, which is the one spelling every POSIX shell agrees on.
func shellQuote(s string) string {
	safe := true
	for i := 0; i < len(s); i++ {
		c := s[i]
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' ||
			strings.IndexByte("_,./:+-%=", c) >= 0) {
			safe = false
			break
		}
	}
	if safe && s != "" {
		return s
	}
	return "'" + strings.ReplaceAll(s, "'", `'"'"'`) + "'"
}

// Keys the tuner reads that are not one character: what keyDecoder hands back
// instead of a byte.
const (
	keyUp    = "\x01up"
	keyDown  = "\x01down"
	keyLeft  = "\x01left"
	keyRight = "\x01right"
	keyEnter = "\x01enter"
	keyBack  = "\x01back"
	keyEsc   = "\x01esc"
)

// keyDecoder turns the byte stream into keys. Arrows arrive as an escape
// sequence -- ESC [ A, or ESC O A from a terminal in application cursor mode
// -- so ESC cannot be answered the moment it lands: it is either a key of its
// own or the first byte of one. It is held until the byte after it says
// which, and settle() is what decides a lone one, at the end of a batch of
// keys a frame reads. Both ports settle at that same point rather than on a
// timeout: a timer would make which frame an Esc lands in a question about
// the machine's speed, and keytest compares the frames.
//
// A batch boundary is not the end of a keystroke, though, which is what a
// held-down arrow shows: the terminal sends ESC [ C fifty times a second and
// a read can end anywhere in that, so an ESC left over at the end of one
// frame is as likely to be half an arrow as it is a whole Esc. Answering it
// there closed the tuner mid-keypress. So a pending sequence has to sit out a
// whole frame with nothing following it before it is called: waited counts
// the frames it has survived, and one is enough, since the rest of a sequence
// the terminal has already sent is never more than a read away.
type keyDecoder struct {
	state  int // 0 nothing pending, 1 an ESC, 2 inside a sequence
	waited int // frames the pending thing has sat through
}

// keysPerFrame is how many bytes of keys a frame answers before drawing
// again, which is pyclock.py's read size and has to be: a keystroke is more
// than one byte, and answering them one at a time let a tick land in the
// middle of one, so this port painted frames pyclock.py never painted. Both
// take one read of this size per frame and answer all of it.
const keysPerFrame = 64

// escGrace is how many frames a half-read keystroke is given to finish --
// 38ms, which is nothing to wait for an Esc and more than enough for bytes
// the terminal has already written.
const escGrace = 2

// feed reads one byte, returning the keys it completes -- none, one, or (an
// ESC followed by an ordinary key) two.
func (d *keyDecoder) feed(b byte) []string {
	d.waited = 0
	switch d.state {
	case 1:
		if b == '[' || b == 'O' {
			d.state = 2
			return nil
		}
		d.state = 0
		return append([]string{keyEsc}, d.feed(b)...)
	case 2:
		// Parameter bytes first, then one final byte in @ to ~: a modified
		// arrow is ESC [ 1 ; 5 A, and anything else in that shape is read to
		// its end and dropped rather than leaking its letters into a value.
		if b < 0x40 || b > 0x7e {
			return nil
		}
		d.state = 0
		switch b {
		case 'A':
			return []string{keyUp}
		case 'B':
			return []string{keyDown}
		case 'C':
			return []string{keyRight}
		case 'D':
			return []string{keyLeft}
		}
		return nil
	}
	switch {
	case b == 0x1b:
		d.state = 1
		return nil
	case b == '\r' || b == '\n':
		return []string{keyEnter}
	case b == 0x7f || b == 0x08:
		return []string{keyBack}
	case b >= 0x20 && b < 0x7f:
		return []string{string(rune(b))}
	}
	return nil
}

// settle ends a frame's batch of keys. Nothing pending, nothing to do; and
// anything pending gets one frame's grace, in case it is a keystroke the read
// cut in half. What is still there after that was all there was: a bare ESC
// is the key, and a sequence that never finished is dropped rather than left
// to swallow the next key that arrives.
func (d *keyDecoder) settle() []string {
	if d.state == 0 {
		return nil
	}
	if d.waited < escGrace {
		d.waited++
		return nil
	}
	state := d.state
	d.state, d.waited = 0, 0
	if state == 1 {
		return []string{keyEsc}
	}
	return nil
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
		// The face is colsN wide and its cell may be wider, so the face rows
		// are padded into it. Plain spaces on either side of already-coloured
		// rows, rather than centring them: centring counts characters, and a
		// coloured row is mostly escape bytes.
		cell := cellCols()
		padLeft := strings.Repeat(" ", (cell-colsN)/2)
		padRight := strings.Repeat(" ", cell-colsN-len(padLeft))
		for r := 0; r < rowsN; r++ {
			parts := make([]string, len(drawn))
			for i := range drawn {
				parts[i] = padLeft + drawn[i][r] + padRight
			}
			rows = append(rows, row(parts))
		}
		labels := make([]string, len(chunk))
		digits := make([]string, len(chunk))
		for i, d := range chunk {
			labels[i] = center(fitLabel(d.label, cell), cell, lay.extraLeft)
			digits[i] = center(digital(times[i], weekday), cell, lay.extraLeft)
		}
		rows = append(rows, row(labels))
		rows = append(rows, row(digits))
	}
	return rows
}

// isTerminal reports whether f is a terminal rather than a pipe, a file, or
// some other character device. The ioctl is what isatty(3) itself does, and
// so is what Python's sys.stdout.isatty() does on the other side of the port:
// a terminal is a thing with a termios, not a thing with a device number.
//
// Asking os.Stat for ModeCharDevice instead is the obvious version and is
// wrong in exactly one place that matters -- /dev/null is a character device.
// Under it, `clock >/dev/null` took the alternate screen and ran forever
// where pyclock.py drew one frame and exited, and nothing caught it: every
// redirection in difftest goes to a regular file.
func isTerminal(f *os.File) bool {
	var t syscall.Termios
	return ioctlTermios(f.Fd(), tcGet, &t) == nil
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
	cell := cellCols()
	maxFit := (termCols + gap) / (cell + gap)
	if maxFit < 1 {
		return 0, fmt.Errorf(
			"terminal is %d columns wide and one clock face needs %d; widen the window, or lower --cell-ratio",
			termCols, cell)
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
//
// An odd rowsN (or colsN) is preferred within symmetryWindow of the largest
// fit: an odd count centres the face's true axis exactly in the middle of a
// character cell, while an even one centres it exactly on the boundary
// between two cells, where no placement of a major tick's two dots can be
// symmetric -- see the axis-safe tick comment on spoke(), and
// ARCHITECTURE.md, for why that is otherwise unavoidable. Every smaller
// candidate already fits, by the same monotonicity argument above, so
// trading a handful of rows for one with both counts odd costs nothing but
// those few rows -- capped at symmetryWindow, so a face that never finds one
// does not shrink indefinitely looking.
func autoScale(wantPerRow, numFaces, cols, lines, hpad, vpad int) {
	best := 0
	for n := maxRowsN; n >= minRowsN; n-- {
		rowsN = n
		colsN = int(math.Floor(float64(n)*cellRatio + 0.5))
		perRow, err := fitPerRow(wantPerRow, numFaces, cols, gapFloor(cols, hpad, gap))
		if err != nil {
			continue
		}
		if fitHeight(chunkCount(numFaces, perRow), lines, gapFloor(lines, vpad, vgap)) == nil {
			best = n
			break
		}
	}
	if best == 0 {
		rowsN = minRowsN
		colsN = int(math.Floor(float64(minRowsN)*cellRatio + 0.5))
		return
	}
	rowFallback, colFallback := -1, -1
	for n := best; n > best-symmetryWindow && n >= minRowsN; n-- {
		c := int(math.Floor(float64(n)*cellRatio + 0.5))
		if n%2 == 1 && c%2 == 1 {
			rowsN, colsN = n, c
			return
		}
		if n%2 == 1 && rowFallback < 0 {
			rowFallback = n
		}
		if c%2 == 1 && colFallback < 0 {
			colFallback = n
		}
	}
	// No candidate had both odd: an odd rowsN keeps the 3/9 o'clock ticks
	// symmetric, which is the more noticeable pair, so it wins over an odd
	// colsN alone.
	switch {
	case rowFallback >= 0:
		rowsN = rowFallback
	case colFallback >= 0:
		rowsN = colFallback
	default:
		rowsN = best
	}
	colsN = int(math.Floor(float64(rowsN)*cellRatio + 0.5))
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
// under us. It clears ECHO/ECHONL/ICANON but leaves ISIG set, so Ctrl+C still
// signals, and hands back the two moves the clock can make with the terminal
// afterwards: restore puts back what the shell handed over, requiet takes it
// again. Quitting needs the first, Ctrl+Z needs both, either side of the stop.
// Restoring with the flushing variant drops whatever was typed during the run,
// so stray keys can't land in the shell afterwards. Falls back to no-ops when
// stdin isn't a terminal.
func quietTerminal() (restore, requiet func()) {
	nothing := func() {}
	fd := os.Stdin.Fd()
	var saved syscall.Termios
	if ioctlTermios(fd, tcGet, &saved) != nil {
		return nothing, nothing
	}
	quiet := saved
	quiet.Lflag &^= syscall.ECHO | syscall.ECHONL | syscall.ICANON
	if ioctlTermios(fd, tcSet, &quiet) != nil {
		return nothing, nothing
	}
	// Both copy before the call: the ioctl takes a pointer, and the saved and
	// quiet modes have to survive being applied more than once.
	return func() {
			mode := saved
			ioctlTermios(fd, tcSetFlush, &mode)
		}, func() {
			mode := quiet
			ioctlTermios(fd, tcSet, &mode)
		}
}

// suspend is Ctrl+Z: give the terminal back, stop for real, and take it again
// on the way out the other side. kill(2) delivers before it returns, so
// everything after it runs on resume -- cbreak again, the alternate screen
// again, and a repaint, since what SIGCONT comes back to is the screen the
// shell left rather than the one the clock was drawing on.
//
// The stop is SIGSTOP rather than the usual move, which is to put SIGTSTP's
// default disposition back and raise that at yourself. That move is not
// available here: signal.Notify installs the runtime's handler, and
// signal.Reset does not take it off again -- sigInstallGoHandler keeps it for
// every signal but a couple of special cases -- so the raise lands in a
// handler with no channel left to send to, which returns and swallows it. The
// clock would hand the terminal back and carry straight on, which is a Ctrl+Z
// that does nothing. Nothing catches, blocks or swallows SIGSTOP, so it stops.
//
// pyclock.py has no such trouble and stops itself the same way regardless: what
// a shell prints for a job stopped by SIGSTOP differs from what it prints for
// one stopped by SIGTSTP -- "Stopped(SIGSTOP)" against "Stopped" in bash --
// and two ports that stopped by different signals would not stop alike. The one thing lost is that SIGTSTP is discarded when the process
// group is orphaned, where SIGSTOP is not: a clock sent `kill -TSTP` from
// outside such a group stops where it would once have been left running, and
// wants a `kill -CONT` to come back. Ctrl+Z cannot reach it there in the first
// place -- the terminal driver discards job-control signals for an orphaned
// group too, before any of this is reached.
func suspend(restore, requiet func(), fullScreen bool) {
	fmt.Print(showCursor)
	if fullScreen {
		fmt.Print(leaveAlt)
	}
	restore()

	syscall.Kill(syscall.Getpid(), syscall.SIGSTOP)
	// Raising a stop is not the same as having stopped. kill(2) promises the
	// signal is delivered before it returns, and in a process of more than one
	// thread -- which this is, always -- the thread that takes it need not be
	// this one: the rest of the group stops at the next point each returns to
	// user mode, and this one can run on until then. On Linux that is far
	// enough to take the terminal back before the stop it just asked for, so
	// Ctrl+Z handed the screen over and took it again in the same breath and
	// the shell got a stopped clock's terminal after all. Sleeping a tick is
	// the barrier, and needs to be nothing cleverer: it cannot finish early,
	// and a clock that really stopped is not running for any of it.
	time.Sleep(tick)

	requiet()
	if fullScreen {
		fmt.Print(enterAlt)
	}
	fmt.Print(hideCursor)
}

// pendingKeys is whatever is waiting on stdin, or nothing at all. Never
// blocks, and assumes cbreak mode, where a key arrives without a Return
// behind it.
//
// Asked once a frame rather than read from continuously, which is not a
// detail: a reader sitting in read(2) is handed the first byte of a keystroke
// the instant the terminal has it, so an arrow can arrive split in two and a
// two-character value can arrive a character at a time -- and then this port
// paints frames pyclock.py, which reads once a frame and takes whatever has
// gathered, never paints. That difference cost a release. This is the same
// select-then-read pyclock.py does, in the same place in the loop.
func pendingKeys(buf []byte) []byte {
	ready, err := selectRead()
	if err != nil || !ready {
		return nil
	}
	n, err := syscall.Read(0, buf)
	if err != nil || n <= 0 {
		return nil
	}
	return buf[:n]
}

// leftWith is the command line the clock ended up reading as, once the tuner
// has been at it: printed by main after the screen has gone back to the
// shell, since a layout worked out inside the alternate screen is lost with
// it. Empty when nothing was changed, which is every redirected run.
var leftWith string

// run does everything that can fail up front, before the terminal is touched:
// os.Exit skips deferred restores, so nothing may return an error once the
// cursor is hidden or cbreak mode is on.
func run() error {
	frozen, pinned, err := freeze()
	if err != nil {
		return err
	}
	// The preferences file first, then the command line on top of it, both
	// through the same parser: a flag typed here beats the same flag saved
	// there because it is read second, which is the only rule either of them
	// needs. -h and --version are refused in a file, where a clock that
	// printed its usage and stopped would be a bad afternoon.
	conf := configPath()
	opt := defaultOptions()
	tokens, err := loadConfig(conf)
	if err != nil {
		return err
	}
	if opt, err = parseArgs(tokens, opt); err != nil {
		if errors.Is(err, errHelp) || errors.Is(err, errVersion) {
			return fmt.Errorf("%s: -h and --version are not settings; delete that line", conf)
		}
		return fmt.Errorf("%s: %v", conf, err)
	}
	opt, err = parseArgs(os.Args[1:], opt)
	if errors.Is(err, errHelp) {
		fmt.Print(usage)
		return nil
	}
	if errors.Is(err, errVersion) {
		fmt.Printf("clock %s\n", version)
		return nil
	}
	if err != nil {
		return err
	}
	vals := opt.vals
	zoneList, quiet := opt.zoneList, opt.quiet
	color := useColor(opt.colorWhen)

	// The six knobs, as text, are the whole of the layout state: --cell-ratio
	// has already beaten CLOCK_CELL_RATIO in defaultTunes, and a fixed
	// --scale sizes the face here and for good, where --scale auto leaves
	// rowsN to the per-frame search. r re-runs exactly this, which is why it
	// can change any of them without the loop knowing where a value came
	// from.
	set := readTunes(vals)
	set.apply()

	// A pinned clock is a still of one instant, and an undated still records
	// half of it, so the weekday goes under every face unless --day says
	// otherwise. Live, auto keeps it for the clocks that actually disagree.
	dayWhen := opt.dayWhen
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
	// SIGPIPE is in the list for what asking changes rather than for what
	// arrives: left alone, the runtime kills the process where a write to fd 1
	// or 2 hits EPIPE, which skips every defer below and hands back a terminal
	// still in cbreak. Once it is notified, the write returns the error
	// instead and the frame loop can stop like anything else. SIGTSTP is there
	// for the same kind of reason and costs more: notifying it turns off the
	// stop the runtime would have done, so the loop owes the reader one, and
	// owes it a terminal to come back to -- see suspend.
	signal.Notify(sigs, os.Interrupt, syscall.SIGTERM, syscall.SIGPIPE, syscall.SIGTSTP)

	restoreTerm, requietTerm := quietTerminal()
	defer restoreTerm()
	// Keys are read from stdin, so there are only keys to read when stdin is
	// a terminal. Redirected, reading it would eat whatever it is -- and
	// pyclock.py asks the same question, of the same descriptor, for the same
	// reason.
	interactive := isTerminal(os.Stdin)
	keyBuf := make([]byte, keysPerFrame)

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
	// A pinned clock draws one frame unless CLOCK_FRAMES asks for a sequence;
	// drawn is which frame of it this is, and so how far the instant has moved
	// from the pinned one.
	frames, step, err := sequence(pinned)
	if err != nil {
		return err
	}
	drawn := 0

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
	// The tuner, and the keys feeding it.
	tune := tuner{}
	var dec keyDecoder
	// What S does, and what it says afterwards: the clock on screen written
	// back to the preferences file, or why it could not be.
	save := func() string {
		if err := saveConfig(conf, configTokens(vals, zoneList)); err != nil {
			return err.Error()
		}
		return "saved to " + conf
	}
	// Said once the tuner closes, in the same modal it used, and dropped at
	// the next key: the flags the layout on screen would need.
	note := ""
	// Off the wall clock, not the frame's: a clock pinned with CLOCK_FREEZE
	// never advances, and the hint still has to give up after three seconds.
	flashUntil := time.Now().Add(flashFor)
	// Which faces there are, and in what order, changes only when some zone's
	// offset changes -- and a tz transition always lands on a whole second, so
	// recomputing once a second cannot miss one. See inside the loop.
	var faceSecond int64
	var faces []dial

	for {
		now := time.Now()
		if pinned {
			now = frozen
		}
		if oneShot {
			now = now.Add(time.Duration(drawn) * step)
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

		// Unlike the size, this is not re-measured every frame. Merging and
		// ordering both turn on the zones' offsets at now, which move only at
		// a tz transition, and a transition happens on a whole second -- so a
		// second is the coarsest interval that cannot skip one, and at 19ms
		// frames that is ~50x less work. Unix() floors, and pyclock.py floors
		// rather than truncating to match it, so the two hold the same number
		// here before 1970 as well as after -- see the longer note there for
		// why nothing drawn would differ either way.
		if second := now.Unix(); faces == nil || second != faceSecond {
			faceSecond = second
			faces = orderFaces(mergeZones(zones, now), now)
		}

		// -n auto's whole point is choosing whatever per-row count lets
		// --scale auto grow the face furthest; with a fixed --scale there is
		// no face size left for it to affect, so it falls back to the plain
		// default cap. Settled per frame rather than once, since the tuner
		// can move the scale between auto and fixed while the clock runs.
		wantPerRow := set.perRow
		if set.perRowAuto {
			wantPerRow = defaultPerRow
		}

		if set.scaleAuto {
			if cols > 0 && lines > 0 {
				if set.perRowAuto {
					wantPerRow = len(faces) // no real cap: see autoScale's own comment
				}
				autoScale(wantPerRow, len(faces), cols, lines, set.geo.hpad, set.geo.vpad)
			} else {
				// Nothing measurable to fill, so there is nothing to solve --
				// same as any other window that cannot be measured.
				rowsN = defaultRowsN
				colsN = int(math.Floor(defaultRowsN*cellRatio + 0.5))
			}
		}

		var rows []string
		perRow, err := fitPerRow(wantPerRow, len(faces), cols, gapFloor(cols, set.geo.hpad, gap))
		chunks := 0
		if err == nil {
			chunks = chunkCount(len(faces), perRow)
			err = fitHeight(chunks, lines, gapFloor(lines, set.geo.vpad, vgap))
		}
		// The key list and the startup hint are the same kind of thing --
		// a modal laid over the clocks -- so only one shows at a time, and
		// the key list, being asked for, wins over a hint that is already
		// redundant with the "q" line in it.
		var content []string
		switch {
		case fullScreen && tune.open:
			// The one modal that shows over a window too small for the
			// clocks: a scale typed too large is undone from here, and
			// hiding it would leave nothing to undo it with.
			content = tuneRows(tune, vals)
		case fullScreen && note != "":
			// Folded, not one line: a note names a file, and a path is
			// easily longer than a window -- where a modal that does not fit
			// is a modal not shown at all, which for "cannot write" would
			// mean a failed save that said nothing.
			content = fold(note, cols-4)
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
			rows = complaint(err.Error(), cols, lines, set.geo.halign)
		} else {
			// Any lean left over from the grid is answered by the labels
			// leaning the other way, so the frame comes out no more than a
			// column off centre -- and with a gutter to swallow the odd
			// column, dead centre.
			var lay layout
			lay.gap, lay.extra, lay.left, lay.extraLeft = spread(perRow, cellCols(), cols, gap, set.geo.hpad, set.geo.halign)
			lay.vgap, lay.vextra, lay.top, _ = spread(chunks, rowsN+2, lines, vgap, set.geo.vpad, set.geo.valign)
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
		// The one write whose error is worth reading: everything else painted
		// here is an escape sequence that a dead pipe makes moot anyway, and
		// the next frame lands on this line regardless.
		if _, err := fmt.Print(b.String()); err != nil {
			if errors.Is(err, syscall.EPIPE) {
				return errPipe
			}
			return err
		}
		height = len(rows)

		if oneShot {
			drawn++
			if drawn >= frames {
				return nil
			}
			continue
		}

		// The keys this frame answers, read once and all at once -- the same
		// place in the loop pyclock.py reads them, so a burst is spread over
		// the same frames in both. Then the tick is waited out, with nothing
		// left to answer but signals.
		wasTuning := tune.open
		prevVals := vals
		if interactive {
			for _, b := range pendingKeys(keyBuf) {
				for _, key := range dec.feed(b) {
					if pressed(key, &tune, &vals, &set, &holding, &held, &helpOn, &note, now, save) {
						return nil
					}
				}
			}
			// A keystroke left half-read waits a frame to be finished before
			// it is taken for something else. Decided here rather than on a
			// timer: see keyDecoder.
			for _, key := range dec.settle() {
				if pressed(key, &tune, &vals, &set, &holding, &held, &helpOn, &note, now, save) {
					return nil
				}
			}
		}

		for waiting := true; waiting; {
			select {
			case <-t.C:
				waiting = false
			case s := <-sigs:
				switch s {
				case syscall.SIGPIPE:
					// A SIGPIPE that beat the write error to the loop; the
					// two race, and either way the reader is gone.
					return errPipe
				case syscall.SIGTSTP:
					suspend(restoreTerm, requietTerm, fullScreen)
					// Stop waiting out a tick that was interrupted by a stop
					// of unknown length: the screen resumes blank, and the
					// reader should not have to watch it stay that way.
					waiting = false
				default:
					return nil
				}
			}
		}
		if vals != prevVals {
			leftWith = tuneCommand(vals, zoneList)
		}
		if wasTuning && !tune.open {
			// Closing the tuner says what it would take to start the clock
			// this way, since the screen it was tuned on is about to be given
			// back to the shell and take the answer with it.
			note = leftWith
		}
	}
}

// pressed answers one key, and says whether it was the one that quits.
//
// With the tuner open every key belongs to it -- the values it takes are
// words like "left", "even" and "auto", so q, space and h are letters there
// rather than the keys they are everywhere else. Ctrl+C still quits, being
// the terminal driver's rather than the clock's.
func pressed(key string, tune *tuner, vals *[numTunes]string, set *settings,
	holding *bool, held *time.Time, helpOn *bool, note *string, now time.Time,
	save func() string) bool {
	if *note != "" {
		// Any key clears the note the tuner left behind; the key still
		// counts, so a reader who went straight for q gets it.
		*note = ""
	}
	if tune.open {
		switch key {
		case keyUp, keyDown:
			step := 1
			if key == keyUp {
				step = len(tunables) - 1
			}
			tune.sel = (tune.sel + step) % len(tunables)
			tune.edit, tune.editing, tune.err = "", false, ""
		case keyLeft, keyRight, " ":
			// Straight onto the clocks: every value stepping can reach has
			// already been through checkTune, so there is nothing to confirm
			// and nothing that can be refused. Space walks the words round
			// their list, where the arrows stop at the ends.
			delta := 1
			if key == keyLeft {
				delta = -1
			}
			if next := tuneStep(tune.sel, vals[tune.sel], delta, key == " "); next != vals[tune.sel] {
				vals[tune.sel] = next
				*set = readTunes(*vals)
				set.apply()
			}
			tune.edit, tune.editing, tune.err = "", false, ""
		case keyEnter:
			if !tune.editing {
				break
			}
			if err := checkTune(tune.sel, tune.edit); err != nil {
				// The value is gone along with the refusal: what is left of a
				// value the clock will not have is not a head start on the
				// next one, and leaving it would make the next character
				// typed land on the end of it.
				tune.edit, tune.editing, tune.err = "", false, err.Error()
				break
			}
			vals[tune.sel] = canonTune(tune.sel, tune.edit)
			*set = readTunes(*vals)
			set.apply()
			tune.edit, tune.editing, tune.err = "", false, ""
		case keyBack:
			if tune.editing {
				if tune.edit = trimRune(tune.edit); tune.edit == "" {
					tune.editing = false
				}
			}
		case keyEsc:
			// Two things to back out of, innermost first: whatever is being
			// typed, and then the tuner itself.
			if tune.editing {
				tune.edit, tune.editing, tune.err = "", false, ""
				break
			}
			tune.open = false
		default:
			if len(key) == 1 {
				tune.edit, tune.editing, tune.err = tune.edit+key, true, ""
			}
		}
		return false
	}
	switch key {
	case "q", "Q":
		return true
	case " ":
		*holding, *held = !*holding, now
	case "h", "H", "?":
		*helpOn = !*helpOn
	case "r", "R":
		*tune = tuner{open: true}
		*helpOn = false
	case "S":
		// Capitalised on purpose: it overwrites a file, and a shift is enough
		// to keep an elbow from doing it. Lowercase s is not taken, so a miss
		// does nothing at all.
		*note = save()
		*helpOn = false
	}
	return false
}

// trimRune drops the last character of a string, which is the last byte here:
// everything the tuner takes is ASCII, and keyDecoder passes nothing else on.
func trimRune(s string) string {
	if s == "" {
		return s
	}
	return s[:len(s)-1]
}

func main() {
	err := run()
	// After run's defers: the alternate screen is gone, and this lands in the
	// shell's own scrollback where it can be copied.
	if err == nil && leftWith != "" {
		fmt.Println(leftWith)
	}
	if err != nil {
		// `clock | head`: the reader went away, which is not news and not an
		// error message. Exit with the status a shell reports for a filter
		// killed by SIGPIPE, now that run's defers have put the cursor and the
		// terminal back -- which is the whole reason the runtime was stopped
		// from ending the process at the moment of the write.
		//
		// The status is stated rather than inherited: a SIGPIPE the process
		// sends itself is not one raised by a write to fd 1, and the runtime
		// ignores it, so there is no dying of the signal to be had here. 141
		// is what the shell would have shown either way, and it is what
		// pyclock.py exits with for the same case.
		if errors.Is(err, errPipe) {
			os.Exit(pipeStatus)
		}
		fmt.Fprintln(os.Stderr, "clock: "+err.Error())
		os.Exit(1)
	}
}
