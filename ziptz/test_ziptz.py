"""Tests for ziptz, mirroring ziptz_test.go case for case.

    python3 -m unittest discover ziptz     # or: make test-lib
"""

import unittest

import ziptz

# Cases every ZIP table has to get right: a plain lookup, both sides of a
# boundary a prefix rounds the wrong way, and the non-contiguous zones.
CASES = (
    ("94110", "America/Los_Angeles"),  # San Francisco
    ("941", "America/Los_Angeles"),
    ("10001", "America/New_York"),  # Manhattan
    ("100", "America/New_York"),
    ("79835", "America/Denver"),  # Canutillo TX, the losing side of 798
    ("798", "America/Chicago"),  # ... which the prefix rounds to Central
    ("86502", "America/Phoenix"),  # Arizona, which skips daylight saving
    ("865", "America/Denver"),  # ... unlike the Navajo Nation around it
    ("96799", "Pacific/Pago_Pago"),  # American Samoa
    ("967", "Pacific/Honolulu"),
    ("96910", "Pacific/Guam"),
    ("99546", "America/Adak"),  # Adak, an hour behind Anchorage
    ("99501", "America/Anchorage"),
    ("006", "America/Puerto_Rico"),
)


class TestZone(unittest.TestCase):
    def test_known_zips(self):
        for zip_code, name in CASES:
            with self.subTest(zip=zip_code):
                self.assertEqual(ziptz.zone(zip_code), name)

    def test_malformed(self):
        for zip_code in ("", "1", "12", "1234", "123456", "abcde", "9411o", " 9411", "94110\n", "١٢٣"):
            with self.subTest(zip=zip_code):
                with self.assertRaises(ziptz.ZipError) as caught:
                    ziptz.zone(zip_code)
                self.assertIn("not a US ZIP code", str(caught.exception))

    def test_unassigned(self):
        # Real-looking, but the Postal Service has assigned neither: 099 is a
        # gap in the table, and 00501 is a single-building ZIP whose 005 prefix
        # has no delivery area of its own.
        for zip_code in ("099", "00501", "005"):
            with self.subTest(zip=zip_code):
                with self.assertRaises(ziptz.ZipError) as caught:
                    ziptz.zone(zip_code)
                self.assertIn("no US time zone is recorded", str(caught.exception))


class TestLocation(unittest.TestCase):
    def test_loads(self):
        self.assertEqual(ziptz.location("94110").key, "America/Los_Angeles")

    def test_rejects_nonsense(self):
        with self.assertRaises(ziptz.ZipError):
            ziptz.location("nope")


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


if __name__ == "__main__":
    unittest.main()
