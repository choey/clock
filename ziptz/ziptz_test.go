package ziptz

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"
)

// The cases live in testdata/cases.json, which test_ziptz.py reads too: one
// list, run by both libraries, so neither can quietly stop agreeing with the
// other. Add a case there rather than here.
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

// errorKinds maps a case's error kind to the text the message must carry.
var errorKinds = map[string]string{
	"malformed":  "not a US ZIP code",
	"unassigned": "no US time zone is recorded",
}

type testData struct {
	Cases    []testCase        `json:"cases"`
	Zones    []zoneCase        `json:"zones"`
	Instants map[string]string `json:"instants"`
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

func loadCases(t *testing.T) []testCase {
	t.Helper()
	return load(t).Cases
}

func TestZone(t *testing.T) {
	for _, c := range loadCases(t) {
		t.Run(c.Token, func(t *testing.T) {
			got, err := Zone(c.Token)
			if c.Zone != "" {
				if err != nil {
					t.Fatalf("Zone(%q): %v  [%s]", c.Token, err, c.Why)
				}
				if got != c.Zone {
					t.Errorf("Zone(%q) = %q, want %q  [%s]", c.Token, got, c.Zone, c.Why)
				}
				return
			}
			if err == nil {
				t.Fatalf("Zone(%q) = %q, want a %s error  [%s]", c.Token, got, c.Error, c.Why)
			}
			if want := errorKinds[c.Error]; !strings.Contains(err.Error(), want) {
				t.Errorf("Zone(%q): %v, want a %s error saying %q", c.Token, err, c.Error, want)
			}
		})
	}
}

// Location has to agree with Zone on every case, and fail on the same ones:
// the clock calls it, not Zone.
func TestLocation(t *testing.T) {
	for _, c := range loadCases(t) {
		t.Run(c.Token, func(t *testing.T) {
			loc, err := Location(c.Token)
			if c.Zone == "" {
				if err == nil {
					t.Fatalf("Location(%q) = %v, want a %s error", c.Token, loc, c.Error)
				}
				return
			}
			if err != nil {
				// A system without this zone installed is an environment
				// fact, not a bug in the table.
				if strings.Contains(err.Error(), "time zone database lacks") {
					t.Skipf("this system's tz database lacks %s", c.Zone)
				}
				t.Fatalf("Location(%q): %v", c.Token, err)
			}
			if loc.String() != c.Zone {
				t.Errorf("Location(%q) = %v, want %v", c.Token, loc, c.Zone)
			}
		})
	}
}

// A five-digit ZIP is exact, so every listed exception must win over its own
// prefix -- and must actually disagree with it, or it would not be one.
func TestExceptionsBeatTheirPrefix(t *testing.T) {
	for i := 0; i < len(exceptions); {
		prefix := exceptions[i : i+3]
		letter := exceptions[i+3]
		count := int(exceptions[i+4]-'0')*10 + int(exceptions[i+5]-'0')
		body := i + 6
		for k := 0; k < count; k++ {
			zip := prefix + exceptions[body+k*2:body+k*2+2]
			got, err := Zone(zip)
			if err != nil {
				t.Fatalf("Zone(%q): %v", zip, err)
			}
			if got != zones[letter] {
				t.Errorf("Zone(%q) = %q, want the exception's %q", zip, got, zones[letter])
			}
			if got == PrefixZone(prefix) {
				t.Errorf("%q is listed as an exception but agrees with prefix %q", zip, prefix)
			}
		}
		i = body + count*2
	}
}

// Both tables are generated, so the shape checks are really checks on
// tools/genzips.py: records the right width, prefixes ascending, and every
// letter one that zones knows.
func TestTablesAreWellFormed(t *testing.T) {
	if len(runs)%4 != 0 {
		t.Fatalf("runs is %d characters, not a multiple of 4", len(runs))
	}
	prev := ""
	for i := 0; i < len(runs); i += 4 {
		prefix, letter := runs[i:i+3], runs[i+3]
		if prefix <= prev {
			t.Errorf("runs prefix %q does not follow %q", prefix, prev)
		}
		prev = prefix
		if letter != '-' && zones[letter] == "" {
			t.Errorf("runs prefix %q uses unknown zone letter %q", prefix, string(letter))
		}
	}

	prev = ""
	for i := 0; i < len(exceptions); {
		if i+6 > len(exceptions) {
			t.Fatalf("exceptions truncated at %d", i)
		}
		prefix, letter := exceptions[i:i+3], exceptions[i+3]
		count := int(exceptions[i+4]-'0')*10 + int(exceptions[i+5]-'0')
		if prefix < prev {
			t.Errorf("exceptions prefix %q does not follow %q", prefix, prev)
		}
		prev = prefix
		if zones[letter] == "" {
			t.Errorf("exceptions prefix %q uses unknown zone letter %q", prefix, string(letter))
		}
		if count == 0 {
			t.Errorf("exceptions prefix %q has an empty group", prefix)
		}
		prevSuffix := ""
		for k := 0; k < count; k++ {
			suffix := exceptions[i+6+k*2 : i+8+k*2]
			if suffix <= prevSuffix {
				t.Errorf("exceptions prefix %q: suffix %q does not follow %q",
					prefix, suffix, prevSuffix)
			}
			prevSuffix = suffix
		}
		i += 6 + count*2
	}
	if prev == "" {
		t.Error("exceptions is empty")
	}
}

// Every assigned prefix has to resolve, or the clock has a hole in it.
func TestMostPrefixesResolve(t *testing.T) {
	assigned := 0
	for n := 0; n < 1000; n++ {
		p3 := string([]byte{byte('0' + n/100), byte('0' + n/10%10), byte('0' + n%10)})
		if PrefixZone(p3) != "" {
			assigned++
		}
	}
	if assigned < 800 {
		t.Errorf("only %d of 1000 prefixes resolve; the table looks truncated", assigned)
	}
}

func TestAbbrev(t *testing.T) {
	data := load(t)
	winter, summer := data.instant(t, "winter"), data.instant(t, "summer")
	for _, c := range data.Zones {
		t.Run(c.Zone, func(t *testing.T) {
			for _, probe := range []struct {
				at   time.Time
				want string
			}{{winter, c.Winter}, {summer, c.Summer}} {
				got, err := Abbrev(c.Token, probe.at)
				if err != nil {
					if strings.Contains(err.Error(), "time zone database lacks") {
						t.Skipf("this system's tz database lacks %s", c.Zone)
					}
					t.Fatalf("Abbrev(%q): %v", c.Token, err)
				}
				if got != probe.want {
					t.Errorf("Abbrev(%q, %s) = %q, want %q  [%s]",
						c.Token, probe.at.Format("January"), got, probe.want, c.Why)
				}
			}
		})
	}
}

func TestGeneric(t *testing.T) {
	for _, c := range load(t).Zones {
		t.Run(c.Zone, func(t *testing.T) {
			got, err := Generic(c.Token)
			if err != nil {
				t.Fatalf("Generic(%q): %v", c.Token, err)
			}
			if got != c.Generic {
				t.Errorf("Generic(%q) = %q, want %q  [%s]", c.Token, got, c.Generic, c.Why)
			}
			// A zone that never shifts has no pair to generalise over, so its
			// generic name has to be the one abbreviation it ever uses.
			if c.Winter == c.Summer && got != c.Winter {
				t.Errorf("%s never shifts, so Generic should be %q, not %q",
					c.Zone, c.Winter, got)
			}
		})
	}
}

// Every zone the tables can return needs a generic name, or Generic answers
// with the empty string for it.
func TestGenericCoversEveryZone(t *testing.T) {
	for letter, name := range zones {
		if generic[name] == "" {
			t.Errorf("zone letter %q -> %s has no entry in generic", string(letter), name)
		}
	}
	for name := range generic {
		found := false
		for _, z := range zones {
			found = found || z == name
		}
		if !found {
			t.Errorf("generic has %s, which no zone letter names", name)
		}
	}
}

// Abbrev and Generic have to refuse exactly what Zone refuses.
func TestAbbrevAndGenericRejectWhatZoneDoes(t *testing.T) {
	at := load(t).instant(t, "summer")
	for _, c := range loadCases(t) {
		if c.Error == "" {
			continue
		}
		t.Run(c.Token, func(t *testing.T) {
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
		})
	}
}
