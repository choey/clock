package ziptz

import (
	"encoding/json"
	"os"
	"sort"
	"strings"
	"testing"
	"time"
)

// Everything both suites check lives in testdata/cases.json: the ZIP cases,
// the zones, the figures the generated tables should hold, and -- in "checks"
// -- the names of the properties each suite has to implement. A name in that
// list with no test behind it here fails, so this suite cannot quietly cover
// less than the Python one, which reads the same file and answers to the same
// names.
//
// Add a case or a check there rather than here.

type testCase struct {
	Token string `json:"token"`
	Zone  string `json:"zone"`  // the name it must resolve to
	Error string `json:"error"` // or the kind of failure it must produce
	Why   string `json:"why"`
}

// zoneCase is one representative ZIP per zone: what Generic must answer, and
// what Abbrev must answer at each of the two instants.
type zoneCase struct {
	Token   string `json:"token"`
	Zone    string `json:"zone"`
	Generic string `json:"generic"`
	Winter  string `json:"winter"`
	Summer  string `json:"summer"`
	Why     string `json:"why"`
}

type testData struct {
	Cases    []testCase          `json:"cases"`
	Zones    []zoneCase          `json:"zones"`
	Instants map[string]string   `json:"instants"`
	Tables   map[string]int      `json:"tables"`
	Checks   []string            `json:"checks"`
	Only     map[string][]string `json:"language_only"`
}

// errorKinds maps a case's error kind to the text the message must carry.
var errorKinds = map[string]string{
	"malformed":  "not a US ZIP code",
	"unassigned": "no US time zone is recorded",
}

// instant is one of the named instants in the data file, parsed.
func (d testData) instant(t *testing.T, name string) time.Time {
	t.Helper()
	at, err := time.Parse(time.RFC3339, d.Instants[name])
	if err != nil {
		t.Fatalf("instant %q in testdata/cases.json: %v", name, err)
	}
	return at
}

func load(t *testing.T) testData {
	t.Helper()
	blob, err := os.ReadFile("testdata/cases.json")
	if err != nil {
		t.Fatalf("reading the shared cases: %v", err)
	}
	var file testData
	if err := json.Unmarshal(blob, &file); err != nil {
		t.Fatalf("parsing the shared cases: %v", err)
	}
	if len(file.Zones) != len(zones) {
		t.Fatalf("%d zone cases for %d zones: every zone the tables can return needs one",
			len(file.Zones), len(zones))
	}
	// A truncated or renamed file would otherwise pass as zero cases run.
	if len(file.Cases) < 50 {
		t.Fatalf("only %d cases in testdata/cases.json; the file looks truncated", len(file.Cases))
	}
	if len(file.Checks) == 0 {
		t.Fatal("testdata/cases.json declares no checks; the file looks wrong")
	}
	for _, c := range file.Cases {
		if (c.Zone == "") == (c.Error == "") {
			t.Fatalf("case %q must have exactly one of zone and error", c.Token)
		}
		if c.Error != "" && errorKinds[c.Error] == "" {
			t.Fatalf("case %q has unknown error kind %q", c.Token, c.Error)
		}
	}
	return file
}

// runRecord is one (prefix, letter) of the run table.
type runRecord struct {
	prefix string
	letter byte
}

func runRecords() []runRecord {
	out := make([]runRecord, 0, len(runs)/4)
	for i := 0; i < len(runs); i += 4 {
		out = append(out, runRecord{runs[i : i+3], runs[i+3]})
	}
	return out
}

// exceptionGroup is one prefix's worth of the exception table.
type exceptionGroup struct {
	prefix   string
	letter   byte
	suffixes []string
}

func exceptionGroups() []exceptionGroup {
	var out []exceptionGroup
	for i := 0; i < len(exceptions); {
		count := int(exceptions[i+4]-'0')*10 + int(exceptions[i+5]-'0')
		body := i + 6
		group := exceptionGroup{prefix: exceptions[i : i+3], letter: exceptions[i+3]}
		for k := 0; k < count; k++ {
			group.suffixes = append(group.suffixes, exceptions[body+k*2:body+k*2+2])
		}
		out = append(out, group)
		i = body + count*2
	}
	return out
}

// implemented maps every name in the shared "checks" list to the test that
// answers to it. TestEveryDeclaredCheckRuns is what holds this to the file.
var implemented = map[string]func(*testing.T, testData){
	"zone-cases": func(t *testing.T, d testData) {
		for _, c := range d.Cases {
			got, err := Zone(c.Token)
			if c.Zone != "" {
				if err != nil {
					t.Errorf("Zone(%q): %v  [%s]", c.Token, err, c.Why)
				} else if got != c.Zone {
					t.Errorf("Zone(%q) = %q, want %q  [%s]", c.Token, got, c.Zone, c.Why)
				}
				continue
			}
			if err == nil {
				t.Errorf("Zone(%q) = %q, want a %s error  [%s]", c.Token, got, c.Error, c.Why)
			} else if want := errorKinds[c.Error]; !strings.Contains(err.Error(), want) {
				t.Errorf("Zone(%q): %v, want a %s error saying %q", c.Token, err, c.Error, want)
			}
		}
	},

	// Location has to agree with Zone on every case, and fail on the same
	// ones: the clock calls it, not Zone.
	"location-cases": func(t *testing.T, d testData) {
		for _, c := range d.Cases {
			loc, err := Location(c.Token)
			if c.Zone == "" {
				if err == nil {
					t.Errorf("Location(%q) = %v, want a %s error", c.Token, loc, c.Error)
				}
				continue
			}
			if err != nil {
				// A system without this zone installed is an environment
				// fact, not a bug in the table.
				if strings.Contains(err.Error(), "time zone database lacks") {
					t.Skipf("this system's tz database lacks %s", c.Zone)
				}
				t.Errorf("Location(%q): %v", c.Token, err)
			} else if loc.String() != c.Zone {
				t.Errorf("Location(%q) = %v, want %v", c.Token, loc, c.Zone)
			}
		}
	},

	"abbrev-cases": func(t *testing.T, d testData) {
		winter, summer := d.instant(t, "winter"), d.instant(t, "summer")
		for _, c := range d.Zones {
			for _, probe := range []struct {
				at   time.Time
				want string
			}{{winter, c.Winter}, {summer, c.Summer}} {
				got, err := Abbrev(c.Token, probe.at)
				if err != nil {
					if strings.Contains(err.Error(), "time zone database lacks") {
						t.Skipf("this system's tz database lacks %s", c.Zone)
					}
					t.Errorf("Abbrev(%q): %v", c.Token, err)
					continue
				}
				if got != probe.want {
					t.Errorf("Abbrev(%q, %s) = %q, want %q  [%s]",
						c.Token, probe.at.Format("January"), got, probe.want, c.Why)
				}
			}
		}
	},

	"generic-cases": func(t *testing.T, d testData) {
		for _, c := range d.Zones {
			got, err := Generic(c.Token)
			if err != nil {
				t.Errorf("Generic(%q): %v", c.Token, err)
				continue
			}
			if got != c.Generic {
				t.Errorf("Generic(%q) = %q, want %q  [%s]", c.Token, got, c.Generic, c.Why)
			}
		}
	},

	"abbrev-and-generic-refuse-what-zone-refuses": func(t *testing.T, d testData) {
		at := d.instant(t, "summer")
		for _, c := range d.Cases {
			if c.Error == "" {
				continue
			}
			want := errorKinds[c.Error]
			if got, err := Abbrev(c.Token, at); err == nil {
				t.Errorf("Abbrev(%q) = %q, want a %s error", c.Token, got, c.Error)
			} else if !strings.Contains(err.Error(), want) {
				t.Errorf("Abbrev(%q): %v, want %q", c.Token, err, want)
			}
			if got, err := Generic(c.Token); err == nil {
				t.Errorf("Generic(%q) = %q, want a %s error", c.Token, got, c.Error)
			} else if !strings.Contains(err.Error(), want) {
				t.Errorf("Generic(%q): %v, want %q", c.Token, err, want)
			}
		}
	},

	// What the generated tables hold, against what the data says they should.
	// Regenerating changes these, which is the point: the new figures have to
	// be written down before the suite goes green again.
	"table-figures": func(t *testing.T, d testData) {
		records, groups := runRecords(), exceptionGroups()
		named, listed := 0, 0
		for _, r := range records {
			if r.letter != '-' {
				named++
			}
		}
		for _, g := range groups {
			listed += len(g.suffixes)
		}
		assigned := 0
		for n := 0; n < 1000; n++ {
			p3 := string([]byte{byte('0' + n/100), byte('0' + n/10%10), byte('0' + n%10)})
			if PrefixZone(p3) != "" {
				assigned++
			}
		}
		for _, want := range []struct {
			name string
			got  int
		}{
			{"runs", len(records)},
			{"runs_named", named},
			{"exceptions", listed},
			{"exception_prefixes", len(groups)},
			{"letters", len(zones)},
			{"generics", len(generic)},
			{"assigned_prefixes", assigned},
		} {
			if want.got != d.Tables[want.name] {
				t.Errorf("%s: the tables hold %d, testdata/cases.json says %d",
					want.name, want.got, d.Tables[want.name])
			}
		}
	},

	"runs-ascending": func(t *testing.T, d testData) {
		if len(runs)%4 != 0 {
			t.Fatalf("runs is %d characters, not a multiple of 4", len(runs))
		}
		prev := ""
		for _, r := range runRecords() {
			if r.prefix <= prev {
				t.Errorf("runs prefix %q does not follow %q", r.prefix, prev)
			}
			prev = r.prefix
		}
	},

	"runs-letters-known": func(t *testing.T, d testData) {
		for _, r := range runRecords() {
			if r.letter != '-' && zones[r.letter] == "" {
				t.Errorf("runs prefix %q uses unknown zone letter %q", r.prefix, string(r.letter))
			}
		}
		for _, g := range exceptionGroups() {
			if zones[g.letter] == "" {
				t.Errorf("exceptions prefix %q uses unknown zone letter %q", g.prefix, string(g.letter))
			}
		}
	},

	"exceptions-ascending": func(t *testing.T, d testData) {
		prev := ""
		for _, g := range exceptionGroups() {
			if g.prefix < prev {
				t.Errorf("exceptions prefix %q does not follow %q", g.prefix, prev)
			}
			prev = g.prefix
			if len(g.suffixes) == 0 {
				t.Errorf("exceptions prefix %q has an empty group", g.prefix)
			}
			if !sort.StringsAreSorted(g.suffixes) {
				t.Errorf("exceptions prefix %q: suffixes %v are not in order", g.prefix, g.suffixes)
			}
			seen := map[string]bool{}
			for _, suffix := range g.suffixes {
				if seen[suffix] {
					t.Errorf("exceptions prefix %q lists suffix %q twice", g.prefix, suffix)
				}
				seen[suffix] = true
			}
		}
	},

	// A five-digit ZIP is exact, so every listed exception must win over its
	// own prefix -- and must actually disagree with it, or it would not be one.
	"exceptions-beat-their-prefix": func(t *testing.T, d testData) {
		for _, g := range exceptionGroups() {
			for _, suffix := range g.suffixes {
				zip := g.prefix + suffix
				got, err := Zone(zip)
				if err != nil {
					t.Errorf("Zone(%q): %v", zip, err)
					continue
				}
				if got != zones[g.letter] {
					t.Errorf("Zone(%q) = %q, want the exception's %q", zip, got, zones[g.letter])
				}
				if got == PrefixZone(g.prefix) {
					t.Errorf("%q is listed as an exception but agrees with prefix %q", zip, g.prefix)
				}
			}
		}
	},

	"every-zone-has-a-case": func(t *testing.T, d testData) {
		covered := map[string]bool{}
		for _, c := range d.Zones {
			covered[c.Zone] = true
		}
		for letter, name := range zones {
			if !covered[name] {
				t.Errorf("zone letter %q -> %s has no case in testdata/cases.json",
					string(letter), name)
			}
		}
	},

	// Every zone the tables can return needs a generic name, or Generic
	// answers with the empty string for it.
	"generic-covers-every-zone": func(t *testing.T, d testData) {
		for letter, name := range zones {
			if generic[name] == "" {
				t.Errorf("zone letter %q -> %s has no entry in generic", string(letter), name)
			}
		}
		for name := range generic {
			found := false
			for _, zone := range zones {
				found = found || zone == name
			}
			if !found {
				t.Errorf("generic has %s, which no zone letter names", name)
			}
		}
	},

	// A zone that never shifts has no pair to generalise over, so its generic
	// name has to be the one abbreviation it ever uses.
	"non-shifting-zones-are-their-own-generic": func(t *testing.T, d testData) {
		for _, c := range d.Zones {
			if c.Winter == c.Summer && c.Generic != c.Winter {
				t.Errorf("%s never shifts, so its generic should be %q, not %q",
					c.Zone, c.Winter, c.Generic)
			}
		}
	},

	// A prefix is three digits or it is nothing. The binary search underneath
	// finds the last record sorting at or below its argument, which for a
	// shorter or longer string still finds one -- "99" sorts below "990",
	// "9999" above "995" -- so without a length check each answers a zone for a
	// question nobody asked. Python had no such check for a while and the two
	// ports disagreed here, in the one place the sweep cannot look: every token
	// it asks about is well formed by construction.
	"prefix-zone-wants-three-digits": func(t *testing.T, d testData) {
		for _, p3 := range []string{"", "9", "99", "9999", "94110", " 94", "94 "} {
			if got := PrefixZone(p3); got != "" {
				t.Errorf("PrefixZone(%q) = %q, want \"\": only three digits is a prefix", p3, got)
			}
		}
		if got := PrefixZone("941"); got == "" {
			t.Error(`PrefixZone("941") = "", want a zone: the length check went too far`)
		}
	},
}

func TestEveryDeclaredCheckRuns(t *testing.T) {
	data := load(t)
	wanted := append(append([]string{}, data.Checks...), data.Only["go"]...)
	for _, name := range wanted {
		check, ok := implemented[name]
		if !ok {
			t.Errorf("testdata/cases.json asks for %q and this suite has no test for it", name)
			continue
		}
		t.Run(name, func(t *testing.T) { check(t, data) })
	}
}

// The other direction: a check here and not in the file is a check the Python
// suite was never asked for.
func TestNothingIsTestedThatIsNotDeclared(t *testing.T) {
	data := load(t)
	declared := map[string]bool{}
	for _, name := range data.Checks {
		declared[name] = true
	}
	for _, names := range data.Only {
		for _, name := range names {
			declared[name] = true
		}
	}
	for name := range implemented {
		if !declared[name] {
			t.Errorf("this suite tests %q, which testdata/cases.json does not declare", name)
		}
	}
}
