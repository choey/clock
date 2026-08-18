"""Tests for ziptz, run against the same cases as ziptz_test.go.

    python3 -m unittest -q test_ziptz     # or: make test-py

The cases live in testdata/cases.json, which the Go suite reads too: one list,
run by both libraries, so neither can quietly stop agreeing with the other.
Add a case there rather than here.
"""

import datetime
import json
import pathlib
import unittest

import ziptz

# The text a case's error kind must carry.
ERROR_KINDS = {
    "malformed": "not a US ZIP code",
    "unassigned": "no US time zone is recorded",
}


def load():
    path = pathlib.Path(__file__).resolve().parent / "testdata" / "cases.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    # Every zone the tables can return needs a case, or one could go untested.
    assert len(data["zones"]) == len(ziptz.ZONES), (
        f"{len(data['zones'])} zone cases for {len(ziptz.ZONES)} zones: "
        "every zone the tables can return needs one"
    )
    data["instants"] = {
        name: datetime.datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
        for name, when in data["instants"].items()
    }
    return data


def load_cases():
    cases = load()["cases"]
    # A truncated or renamed file would otherwise pass as zero cases run.
    assert len(cases) >= 50, f"only {len(cases)} cases in {path}; it looks truncated"
    for case in cases:
        assert ("zone" in case) != ("error" in case), (
            f"case {case['token']!r} must have exactly one of zone and error"
        )
        assert case.get("error", "malformed") in ERROR_KINDS, (
            f"case {case['token']!r} has unknown error kind {case['error']!r}"
        )
    return cases


DATA = load()
CASES = load_cases()
ZONE_CASES = DATA["zones"]
WINTER, SUMMER = DATA["instants"]["winter"], DATA["instants"]["summer"]


class TestZone(unittest.TestCase):
    def test_cases(self):
        for case in CASES:
            token, why = case["token"], case["why"]
            with self.subTest(token=token, why=why):
                if "zone" in case:
                    self.assertEqual(ziptz.zone(token), case["zone"], why)
                    continue
                with self.assertRaises(ziptz.ZipError) as caught:
                    ziptz.zone(token)
                self.assertIn(ERROR_KINDS[case["error"]], str(caught.exception), why)


class TestLocation(unittest.TestCase):
    """location() has to agree with zone() on every case, and fail on the same
    ones: the clock calls it, not zone()."""

    def test_cases(self):
        for case in CASES:
            token = case["token"]
            with self.subTest(token=token, why=case["why"]):
                if "zone" not in case:
                    with self.assertRaises(ziptz.ZipError):
                        ziptz.location(token)
                    continue
                try:
                    self.assertEqual(ziptz.location(token).key, case["zone"])
                except ziptz.ZipError as exc:
                    # A system without this zone installed is an environment
                    # fact, not a bug in the table.
                    if "time zone database lacks" not in str(exc):
                        raise
                    self.skipTest(f"this system's tz database lacks {case['zone']}")


def exception_groups():
    """Every (prefix, letter, suffix) the exception table records."""
    i = 0
    while i < len(ziptz.EXCEPTIONS):
        prefix = ziptz.EXCEPTIONS[i : i + 3]
        letter = ziptz.EXCEPTIONS[i + 3]
        count = int(ziptz.EXCEPTIONS[i + 4 : i + 6])
        body = i + 6
        yield prefix, letter, count, [
            ziptz.EXCEPTIONS[body + k * 2 : body + k * 2 + 2] for k in range(count)
        ]
        i = body + count * 2


class TestTables(unittest.TestCase):
    def test_exceptions_beat_their_prefix(self):
        """A five-digit ZIP is exact, so every listed exception must win over
        its own prefix -- and must actually disagree with it, or it would not
        be one."""
        for prefix, letter, _, suffixes in exception_groups():
            for suffix in suffixes:
                zip_code = prefix + suffix
                with self.subTest(zip=zip_code):
                    self.assertEqual(ziptz.zone(zip_code), ziptz.ZONES[letter])
                    self.assertNotEqual(
                        ziptz.zone(zip_code), ziptz.prefix_zone(prefix)
                    )

    def test_runs_well_formed(self):
        """Both tables are generated, so the shape checks are really checks on
        tools/genzips.py."""
        self.assertEqual(len(ziptz.RUNS) % 4, 0)
        prev = ""
        for i in range(0, len(ziptz.RUNS), 4):
            prefix, letter = ziptz.RUNS[i : i + 3], ziptz.RUNS[i + 3]
            self.assertGreater(prefix, prev)
            prev = prefix
            if letter != "-":
                self.assertIn(letter, ziptz.ZONES)

    def test_exceptions_well_formed(self):
        prev = ""
        seen = 0
        for prefix, letter, count, suffixes in exception_groups():
            self.assertGreaterEqual(prefix, prev)
            prev = prefix
            self.assertIn(letter, ziptz.ZONES)
            self.assertGreater(count, 0)
            self.assertEqual(suffixes, sorted(suffixes))
            seen += 1
        self.assertGreater(seen, 0)

    def test_most_prefixes_resolve(self):
        """Every assigned prefix has to resolve, or the clock has a hole in it."""
        assigned = sum(1 for n in range(1000) if ziptz.prefix_zone(f"{n:03d}"))
        self.assertGreater(assigned, 800)


class TestAbbrev(unittest.TestCase):
    def test_cases(self):
        for case in ZONE_CASES:
            for season, at in (("winter", WINTER), ("summer", SUMMER)):
                with self.subTest(zone=case["zone"], season=season, why=case["why"]):
                    try:
                        got = ziptz.abbrev(case["token"], at)
                    except ziptz.ZipError as exc:
                        if "time zone database lacks" not in str(exc):
                            raise
                        self.skipTest(f"this system's tz database lacks {case['zone']}")
                    self.assertEqual(got, case[season], case["why"])

    def test_defaults_to_now(self):
        self.assertEqual(
            ziptz.abbrev("94110"), ziptz.abbrev("94110", datetime.datetime.now(datetime.timezone.utc))
        )


class TestGeneric(unittest.TestCase):
    def test_cases(self):
        for case in ZONE_CASES:
            with self.subTest(zone=case["zone"], why=case["why"]):
                got = ziptz.generic(case["token"])
                self.assertEqual(got, case["generic"], case["why"])
                # A zone that never shifts has no pair to generalise over, so
                # its generic name has to be the one abbreviation it ever uses.
                if case["winter"] == case["summer"]:
                    self.assertEqual(got, case["winter"])

    def test_covers_every_zone(self):
        """Every zone the tables can return needs a generic name, or generic()
        raises KeyError for it."""
        self.assertEqual(set(ziptz.GENERIC), set(ziptz.ZONES.values()))


class TestAbbrevAndGenericRejectWhatZoneDoes(unittest.TestCase):
    def test_cases(self):
        for case in CASES:
            if "error" not in case:
                continue
            with self.subTest(token=case["token"], why=case["why"]):
                want = ERROR_KINDS[case["error"]]
                with self.assertRaises(ziptz.ZipError) as caught:
                    ziptz.abbrev(case["token"], SUMMER)
                self.assertIn(want, str(caught.exception))
                with self.assertRaises(ziptz.ZipError) as caught:
                    ziptz.generic(case["token"])
                self.assertIn(want, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
