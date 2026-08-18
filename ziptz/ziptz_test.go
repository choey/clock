package ziptz

import (
	"strings"
	"testing"
)

// Cases every ZIP table has to get right: a plain lookup, both sides of a
// boundary a prefix rounds the wrong way, and the non-contiguous zones.
var cases = []struct {
	zip  string
	zone string
}{
	{"94110", "America/Los_Angeles"}, // San Francisco
	{"941", "America/Los_Angeles"},
	{"10001", "America/New_York"}, // Manhattan
	{"100", "America/New_York"},
	{"79835", "America/Denver"},    // Canutillo TX, the losing side of 798
	{"798", "America/Chicago"},     // ... which the prefix rounds to Central
	{"86502", "America/Phoenix"},   // Arizona, which skips daylight saving
	{"865", "America/Denver"},      // ... unlike the Navajo Nation around it
	{"96799", "Pacific/Pago_Pago"}, // American Samoa
	{"967", "Pacific/Honolulu"},
	{"96910", "Pacific/Guam"},
	{"99546", "America/Adak"}, // Adak, an hour behind Anchorage
	{"99501", "America/Anchorage"},
	{"006", "America/Puerto_Rico"},
}

func TestZone(t *testing.T) {
	for _, c := range cases {
		got, err := Zone(c.zip)
		if err != nil {
			t.Errorf("Zone(%q): %v", c.zip, err)
			continue
		}
		if got != c.zone {
			t.Errorf("Zone(%q) = %q, want %q", c.zip, got, c.zone)
		}
	}
}

func TestZoneErrors(t *testing.T) {
	for _, zip := range []string{"", "1", "12", "1234", "123456", "abcde", "9411o", " 9411", "94110\n"} {
		if got, err := Zone(zip); err == nil {
			t.Errorf("Zone(%q) = %q, want an error", zip, got)
		} else if !strings.Contains(err.Error(), "not a US ZIP code") {
			t.Errorf("Zone(%q): %v, want a shape complaint", zip, err)
		}
	}
	// Real-looking, but the Postal Service has assigned neither: 099 is a gap
	// in the table, and 00501 is a single-building ZIP whose 005 prefix has no
	// delivery area of its own.
	for _, zip := range []string{"099", "00501", "005"} {
		if got, err := Zone(zip); err == nil {
			t.Errorf("Zone(%q) = %q, want an error", zip, got)
		} else if !strings.Contains(err.Error(), "no US time zone is recorded") {
			t.Errorf("Zone(%q): %v, want an unassigned complaint", zip, err)
		}
	}
}

func TestLocation(t *testing.T) {
	loc, err := Location("94110")
	if err != nil {
		t.Fatalf("Location: %v", err)
	}
	if loc.String() != "America/Los_Angeles" {
		t.Errorf("Location(\"94110\") = %v, want America/Los_Angeles", loc)
	}
	if _, err := Location("nope"); err == nil {
		t.Error("Location(\"nope\") succeeded, want an error")
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
