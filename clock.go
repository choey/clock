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

	// 19ms, not 20: coprime to 10, so the millisecond ones digit cycles through
	// all ten values instead of sitting still. Reads as a live clock.
	tick = 19 * time.Millisecond

	rowsN            = 11   // face height, in terminal rows
	defaultCellRatio = 2.1  // cell height / width; braille dots are square at 2
	gap              = 3    // blank columns between adjacent faces
	markerR          = 0.70 // numeral distance from the centre, as a fraction of the radius

	defaultPerRow = 3  // faces per row before wrapping
	maxPerRow     = 64 // an upper bound so a typo can't ask for a million faces
)

const usage = `clock - analog terminal clocks

usage: clock [-n N | --per-row N] [ZONES]

  ZONES              comma-separated; default is your local zone
  -n, --per-row N    clocks per row before wrapping (default 3, reduced to fit)
  -h, --help         this message

A zone is an IANA name (Europe/Berlin), a regional abbreviation (ET CT MT PT
AKT HT BST IST JST AET ...), a 2-letter country code (JP, GB), or a US ZIP
code (94110). ET/CT/MT/PT follow daylight saving, so they read EST or EDT
depending on the date; EST/EDT/PST/PDT and the rest are the fixed offsets,
which never shift.

Press q or Ctrl+C to quit.

examples:
  clock
  clock ET,PT,UTC
  clock -n 2 ET,PT,UTC
  clock Europe/Berlin,Asia/Tokyo,94110 --per-row 2
`

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

// freezeLayout is the one instant format CLOCK_FREEZE accepts. Exactly six
// fractional digits, exactly UTC: Python's datetime stops at microseconds, and
// pinning the format keeps both implementations rejecting the same strings.
const freezeLayout = "2006-01-02T15:04:05.000000Z"

// freeze pins the clock to a fixed instant and draws exactly one frame, so the
// Go and Python renders can be diffed byte for byte. Dev hook, not in --help.
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

func (c *canvas) set(fx, fy float64) {
	// Floor(v + 0.5), not Round(): Round is half-away-from-zero but Python's
	// round() is half-to-even, which would split the two renders apart
	x, y := int(math.Floor(snap(fx)+0.5)), int(math.Floor(snap(fy)+0.5))
	if x < 0 || y < 0 || x >= c.w || y >= c.h {
		return
	}
	c.cells[(y/4)*c.cols+x/2] |= dotBits[x%2][y%4]
}

func (c *canvas) line(x0, y0, x1, y1 float64) {
	// snap the endpoints too: steps comes off a rounded difference, and a
	// one-ulp wobble there changes the whole dot sequence, not just one dot
	x0, y0, x1, y1 = snap(x0), snap(y0), snap(x1), snap(y1)
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

// face renders one analog face for t, returning its cell rows. Every face is
// colsN cells wide and rowsN tall.
func face(t time.Time) []string {
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

// parseArgs reads the command line: one optional zone list, and -n/--per-row
// in any position. Hand-rolled rather than package flag, which insists every
// flag precede the first positional -- "clock ET,PT -n 2" would silently
// ignore the -n. clock.py runs the same algorithm for the same reason.
func parseArgs(argv []string) (int, string, error) {
	perRow := defaultPerRow
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
			return 0, "", errHelp
		case strings.HasPrefix(a, "--"):
			name, val, haveVal := strings.Cut(a[2:], "=")
			if name != "per-row" {
				return 0, "", fmt.Errorf("unknown option: --%s", name)
			}
			if !haveVal {
				i++
				if i >= len(argv) {
					return 0, "", errors.New("--per-row needs a number, e.g. --per-row 2")
				}
				val = argv[i]
			}
			n, err := parseCount(val, "--per-row", maxPerRow)
			if err != nil {
				return 0, "", err
			}
			perRow = n
		case len(a) > 1 && strings.HasPrefix(a, "-"):
			if a[1] != 'n' {
				return 0, "", fmt.Errorf("unknown option: %s", a)
			}
			rest := a[2:]
			switch {
			case rest == "":
				i++
				if i >= len(argv) {
					return 0, "", errors.New("-n needs a number, e.g. -n 2")
				}
				rest = argv[i]
			case rest[0] == '=':
				return 0, "", errors.New("-n takes its value as \"-n N\" or \"-nN\", not \"-n=N\"")
			}
			n, err := parseCount(rest, "-n", maxPerRow)
			if err != nil {
				return 0, "", err
			}
			perRow = n
		default:
			positional = append(positional, a)
		}
	}

	if len(positional) > 1 {
		return 0, "", fmt.Errorf("expected one comma-separated zone list, got %d: %s",
			len(positional), strings.Join(positional, " "))
	}
	if len(positional) == 0 {
		return perRow, "", nil
	}
	return perRow, positional[0], nil
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

func zipZone(token string) (*time.Location, error) {
	prefix := token
	if len(token) == 5 {
		prefix = token[:3]
	} else if len(token) != 3 {
		return nil, fmt.Errorf(
			"\"%s\" is not a US ZIP code; give all five digits, or the first three", token)
	}
	name := zipLookup(prefix)
	if name == "" {
		return nil, fmt.Errorf("no US time zone is recorded for ZIP codes starting %s", prefix)
	}
	loc, err := time.LoadLocation(name)
	if err != nil {
		return nil, fmt.Errorf("ZIP %s means %s, which this system's time zone database lacks", prefix, name)
	}
	return loc, nil
}

func unknownZone(token string) error {
	return fmt.Errorf("unknown zone \"%s\"; use an IANA name (Europe/Berlin), "+
		"an abbreviation (%s), a 2-letter country code (JP), or a US ZIP code",
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

// center pads s to w columns, the extra space going on the right. Counts runes,
// not bytes, to match Python's "{:^w}" — zone labels are ASCII today, but the
// alias table is hand-maintained and the two must not drift.
func center(s string, w int) string {
	n := utf8.RuneCountInString(s)
	if n >= w {
		return s
	}
	left := (w - n) / 2
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

// digital renders the readout under one face, e.g. "PDT: 09:53:07.123". The
// clock part is always 12 characters, so the label gets whatever is left; in a
// grid an over-long line would shove every column to its right out of true.
func digital(t time.Time) string {
	return t.Format("15:04:05.000")
}

// chunkCount is how many rows of faces perRow produces.
func chunkCount(n, perRow int) int {
	return (n + perRow - 1) / perRow
}

// frameHeight counts the rows a grid occupies: each chunk is a face, its zone
// name and its digital line, and chunks are separated by one blank row.
func frameHeight(chunks int) int {
	return chunks*(rowsN+2) + (chunks - 1)
}

// frame draws the whole grid: faces left to right, wrapping every perRow. A
// short last row is left-aligned so the column gutters stay lined up.
func frame(faces []dial, now time.Time, perRow int) []string {
	sep := strings.Repeat(" ", gap)
	chunks := chunkCount(len(faces), perRow)
	rows := make([]string, 0, frameHeight(chunks))

	for ci := 0; ci < chunks; ci++ {
		hi := (ci + 1) * perRow
		if hi > len(faces) {
			hi = len(faces)
		}
		chunk := faces[ci*perRow : hi]

		if ci > 0 {
			rows = append(rows, "")
		}

		times := make([]time.Time, len(chunk))
		drawn := make([][]string, len(chunk))
		for i, d := range chunk {
			times[i] = now.In(d.loc)
			drawn[i] = face(times[i])
		}
		for r := 0; r < rowsN; r++ {
			parts := make([]string, len(drawn))
			for i := range drawn {
				parts[i] = drawn[i][r]
			}
			rows = append(rows, strings.Join(parts, sep))
		}
		labels := make([]string, len(chunk))
		digits := make([]string, len(chunk))
		for i, d := range chunk {
			labels[i] = center(truncate(d.label, colsN), colsN)
			digits[i] = center(digital(times[i]), colsN)
		}
		rows = append(rows, strings.Join(labels, sep))
		rows = append(rows, strings.Join(digits, sep))
	}
	return rows
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
func fitPerRow(want, n, termCols int) (int, error) {
	if want > n {
		want = n
	}
	if termCols <= 0 {
		return want, nil // not a terminal: honour what was asked for
	}
	maxFit := (termCols + gap) / (colsN + gap)
	if maxFit < 1 {
		return 0, fmt.Errorf(
			"terminal is %d columns wide and one clock face needs %d; widen the window, or lower CLOCK_CELL_RATIO",
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
func fitHeight(chunks, termRows int) error {
	h := frameHeight(chunks)
	if termRows > 0 && h > termRows {
		return fmt.Errorf(
			"%d rows of clocks need %d lines and this terminal has %d; raise --per-row, or name fewer zones",
			chunks, h, termRows)
	}
	return nil
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
	frozen, oneShot, err := freeze()
	if err != nil {
		return err
	}
	wantPerRow, zoneList, err := parseArgs(os.Args[1:])
	if errors.Is(err, errHelp) {
		fmt.Print(usage)
		return nil
	}
	if err != nil {
		return err
	}
	startedAt := time.Now()
	if oneShot {
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

	fmt.Print(hideCursor)
	defer fmt.Print(showCursor)

	t := time.NewTicker(tick)
	defer t.Stop()

	height := 0
	for {
		now := time.Now()
		if oneShot {
			now = frozen
		}

		// re-measure every frame rather than trapping SIGWINCH: one ioctl per
		// 19ms is nothing beside redrawing the faces, it also picks up a
		// changed COLUMNS, and it keeps this loop the same shape as the
		// Python one, where a signal handler would interact with sleep()
		// under PEP 475 and drift out of step.
		cols, lines := termSize()
		faces := mergeZones(zones, now)
		perRow, err := fitPerRow(wantPerRow, len(faces), cols)
		if err != nil {
			return err
		}
		if err := fitHeight(chunkCount(len(faces), perRow), lines); err != nil {
			return err
		}
		rows := frame(faces, now, perRow)

		// rewind over the rows drawn last time, then repaint in one write.
		// clearEOL wipes a longer previous line, clearBelow a taller previous
		// frame -- the grid reshapes itself when the window is resized.
		var b strings.Builder
		if height > 0 {
			fmt.Fprintf(&b, "\x1b[%dA", height)
		}
		for _, r := range rows {
			b.WriteString(r + clearEOL + "\n")
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
				if k == 'q' || k == 'Q' {
					return nil
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
