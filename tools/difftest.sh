#!/bin/sh
# Prove the Go and Python clocks render byte-for-byte identical output.
#
# Both are pinned to a fixed instant with CLOCK_FREEZE so they draw exactly one
# frame, and to a fixed terminal size with COLUMNS/LINES, then stdout, stderr
# and exit status are compared for every argument list below.
#
# Usage: tools/difftest.sh [-v]      (-v echoes each case as it passes)
set -eu

cd "$(dirname "$0")/.."
out=${TMPDIR:-/tmp}/clock-difftest.$$
mkdir -p "$out"
trap 'rm -rf "$out"' EXIT

verbose=${1:-}
pass=0
fail=0

# Build once: `go run .` recompiles per invocation, and there are hundreds.
go build -o "$out/clock" .

# One case: CLOCK_FREEZE, COLUMNS, LINES, then the argv to pass to both.
check() {
	freeze=$1 cols=$2 lines=$3
	shift 3

	# A live clock runs until told to stop, and there is nothing here to tell
	# it; every case has to pin an instant so both sides draw one frame and go.
	if [ -z "$freeze" ]; then
		echo "BUG in difftest.sh: empty CLOCK_FREEZE would hang"
		exit 2
	fi

	# ratio and tzdir are globals the caller sets; passing them through env
	# rather than exporting keeps one section's values out of the next one's.
	env CLOCK_FREEZE="$freeze" COLUMNS="$cols" LINES="$lines" \
		CLOCK_CELL_RATIO="${ratio:-}" TZDIR="${tzdir:-}" \
		"$out/clock" "$@" >"$out/go.out" 2>"$out/go.err" </dev/null || true
	go_status=$?
	env CLOCK_FREEZE="$freeze" COLUMNS="$cols" LINES="$lines" \
		CLOCK_CELL_RATIO="${ratio:-}" TZDIR="${tzdir:-}" \
		python3 clock.py "$@" >"$out/py.out" 2>"$out/py.err" </dev/null || true
	py_status=$?

	label="[$cols x $lines${ratio:+ r=$ratio}${tzdir:+ tz=$tzdir}] $*"
	if ! cmp -s "$out/go.out" "$out/py.out"; then
		printf 'FAIL stdout %s\n' "$label"
		diff -u "$out/py.out" "$out/go.out" | head -20
		fail=$((fail + 1))
		return
	fi
	if ! cmp -s "$out/go.err" "$out/py.err"; then
		printf 'FAIL stderr %s\n' "$label"
		diff -u "$out/py.err" "$out/go.err" | head -20
		fail=$((fail + 1))
		return
	fi
	if [ "$go_status" -ne "$py_status" ]; then
		printf 'FAIL status %s (go=%s py=%s)\n' "$label" "$go_status" "$py_status"
		fail=$((fail + 1))
		return
	fi
	pass=$((pass + 1))
	[ -z "$verbose" ] || printf 'ok   %s\n' "$label"
}

# Summer and winter, to catch DST-dependent labels.
SUMMER=2026-07-15T09:53:07.123456Z
WINTER=2026-01-15T09:53:07.123456Z

echo "== zones =="
for zones in "" ET ET,PT,UTC local UTC EST MST GMT CET Asia/Kathmandu Etc/GMT+5 \
	Europe/Berlin,Asia/Tokyo,ET,PT,UTC,JP JP GB DE FR IN NZ US AU RU CA \
	94110 941 10001 99546 00501 PST PDT EDT CDT CST MDT HDT AKST AKDT ZZ QQ \
	79835 79821 79855 798 799 96799 96701 967 37301 37387 373 \
	86504 86503 860 49635 49630 498 00000 99999 \
	PST,PT,EST,ET MST,MT,HST,HT \
	PDT,PDT PDT,pdt PDT,PT PT,PDT UTC,UTC,UTC UK,BST PST,PT,PDT \
	ET,America/New_York local,local ET,ET,PT,PT,UTC \
	"ET , PT" "ET,,PT" "/etc/passwd" "../../etc/passwd" "Europe/Bogus" \
	1234 1 123456 AET,ACT,AWT,NZT,IST,JST,KST,SGT,HKT,BST,UK,AKT,HT; do
	check "$SUMMER" 200 60 "$zones"
	check "$WINTER" 200 60 "$zones"
done

# A city off the end of an IANA name, where zone.tab makes it unambiguous.
echo "== city shorthand =="
for zones in Berlin Jakarta berlin JAKARTA Kathmandu Bogota New_York NEW_YORK \
	Indianapolis Indiana/Indianapolis Center North_Dakota/Center \
	York Berl Eastern US/Eastern Europe Asia Etc "" \
	Berlin,Jakarta Berlin,Europe/Berlin Berlin,PT,94110,JP Tokyo,JST; do
	check "$SUMMER" 200 60 "$zones"
	check "$WINTER" 200 60 "$zones"
done

# Every city in the tz database is unique today, so ambiguity only shows up
# against a zone.tab written for the purpose: two zones sharing a tail, and
# ten, which is past the eight the message lists before it starts counting.
mkdir -p "$out/tz2" "$out/tz10"
printf 'XX\t+0000\tEurope/Berlin\nYY\t+0000\tAmerica/Berlin\n' >"$out/tz2/zone.tab"
: >"$out/tz10/zone.tab"
for i in 1 2 3 4 5 6 7 8 9 10; do
	printf 'X%s\t+0000\tZone%s/Springfield\n' "$i" "$i" >>"$out/tz10/zone.tab"
done
for tzdir in "$out/tz2" "$out/tz10"; do
	for zones in Berlin Springfield Europe/Berlin JP ET 94110; do
		check "$SUMMER" 200 60 "$zones"
	done
done
tzdir=

echo "== cli grammar =="
check "$SUMMER" 200 60 ET,PT,UTC -n 2
check "$SUMMER" 200 60 -n 2 ET,PT,UTC
check "$SUMMER" 200 60 --per-row 2 ET,PT
check "$SUMMER" 200 60 ET,PT --per-row 2
check "$SUMMER" 200 60 -n2 ET,PT
check "$SUMMER" 200 60 --per-row=2 ET,PT
check "$SUMMER" 200 60 -n=2 ET,PT
check "$SUMMER" 200 60 -n 0 ET
check "$SUMMER" 200 60 -n abc ET
check "$SUMMER" 200 60 -n 999 ET
check "$SUMMER" 200 60 -n " 2" ET
check "$SUMMER" 200 60 -n +2 ET
check "$SUMMER" 200 60 -n 2_0 ET
check "$SUMMER" 200 60 -n
check "$SUMMER" 200 60 --per-row
check "$SUMMER" 200 60 --per
check "$SUMMER" 200 60 --bogus
check "$SUMMER" 200 60 -x
check "$SUMMER" 200 60 -
check "$SUMMER" 200 60 ET PT
check "$SUMMER" 200 60 -- ET
check "$SUMMER" 200 60 -- -n
check "$SUMMER" 200 60 --color=always ET
check "$SUMMER" 200 60 --color ET
check "$SUMMER" 200 60 --color=auto ET
check "$SUMMER" 200 60 --color=never ET
check "$SUMMER" 200 60 --no-color ET
check "$SUMMER" 200 60 --color=bogus ET
check "$SUMMER" 200 60 --color= ET
check "$SUMMER" 200 60 --no-color=x ET
check "$SUMMER" 200 60 --color always ET
check "$SUMMER" 200 60 --colour ET
check "$SUMMER" 200 60 --color --no-color ET
check "$SUMMER" 200 60 -- --color ET
check "$SUMMER" 200 60 --day=always ET
check "$SUMMER" 200 60 --day ET
check "$SUMMER" 200 60 --day=auto ET
check "$SUMMER" 200 60 --day=never ET
check "$SUMMER" 200 60 --no-day ET
check "$SUMMER" 200 60 --day=bogus ET
check "$SUMMER" 200 60 --day= ET
check "$SUMMER" 200 60 --no-day=x ET
check "$SUMMER" 200 60 --day always ET
check "$SUMMER" 200 60 --day --no-day ET
check "$SUMMER" 200 60 --help
check "$SUMMER" 200 60 -h
check "$SUMMER" 200 60 ET --help

# --color=always colours a pipe, which is the only way to diff the escapes;
# auto leaves one plain, so every other case here renders unchanged.
echo "== colour =="
for zones in ET ET,PT,UTC 94110,JP,GB,NZ; do
	check "$SUMMER" 200 60 --color=always "$zones"
	check "$WINTER" 200 60 --color=always "$zones"
done
check "$SUMMER" 200 60 --color=always ET,PT,UTC,JP,GB -n 2
for ratio in 1.7 2.9; do
	check "$SUMMER" 200 60 --color=always ET
done
ratio=

# Overlaps are where the layering shows: at 12:00:00 all three hands stack on
# one another, and the hour and minute hands cross every 65 minutes or so.
for at in 12:00:00.000000 12:00:00.500000 01:05:27.300000 02:10:54.500000 \
	06:32:43.600000 09:49:05.900000 03:16:21.000000 11:59:59.999999; do
	check "2026-07-15T${at}Z" 200 60 --color=always UTC
done

# Faces are ordered by the time they read and the weekday appears only when
# they disagree about the date, so both want instants either side of a midnight
# and zone lists that are deliberately out of order. Every case here is pinned,
# which defaults the weekday to always, so these ask for --day=auto: it is the
# live clock's rule, and nothing else in the harness can reach it.
echo "== order and days =="
for at in 00:00:00.000000 05:02:41.901000 11:00:00.000000 23:59:59.999999; do
	for zones in PT,UTC,10001,JP,GB UTC,GMT,PST,PT,PDT JST,PT,UTC NZ,HT,UTC \
		Pacific/Kiritimati,Etc/GMT+12,UTC local,JST ET; do
		check "2026-07-15T${at}Z" 200 60 --day=auto "$zones" -n 5
		check "2026-01-15T${at}Z" 200 60 --day=auto "$zones" -n 5
		check "2026-07-15T${at}Z" 200 60 "$zones" -n 5
	done
done
check "$SUMMER" 200 60 UTC,JP,NZ,HT,PT,GB,IN -n 3
check "$SUMMER" 200 60 --color=always UTC,JP,PT

# A 16-column face is the narrowest that holds "Wed 05:02:41.901": 1.5 clears
# it at 17 columns, 1.4 falls one short at 15 and drops the weekday instead.
for ratio in 1.4 1.5; do
	check "2026-07-15T05:02:41.901000Z" 200 60 UTC,PT
done
ratio=

# Every case above lays out with the even fill and centred, which is the
# default; these pin the alignments, the fixed paddings and the arithmetic
# where a gap does not divide evenly.
echo "== layout =="
for size in "100 30" "101 31" "75 13" "80 24" "200 60" "23 13" "49 27" \
	"300 80" "400 100" "76 14"; do
	set -- $size
	for geo in "" "--halign left" "--halign right" "--valign top" \
		"--valign bottom" "--halign left --valign top" \
		"--hpad 10%" "--hpad 0" "--hpad 100%" "--hpad even" \
		"--vpad 10%" "--vpad 0" "--vpad even" \
		"--hpad 5% --vpad 5% --halign right --valign bottom"; do
		check "$SUMMER" "$1" "$2" $geo ET,PT,UTC
		check "$SUMMER" "$1" "$2" $geo -n 2 ET,PT,UTC,JP
	done
done

echo "== layout grammar =="
check "$SUMMER" 200 60 --halign=center ET
check "$SUMMER" 200 60 --halign center ET
check "$SUMMER" 200 60 --halign bogus ET
check "$SUMMER" 200 60 --halign top ET
check "$SUMMER" 200 60 --valign left ET
check "$SUMMER" 200 60 --halign ET
check "$SUMMER" 200 60 --halign
check "$SUMMER" 200 60 --hpad
check "$SUMMER" 200 60 --hpad 10 ET
check "$SUMMER" 200 60 --hpad 10% ET
check "$SUMMER" 200 60 --hpad=10% ET
check "$SUMMER" 200 60 --hpad 010 ET
check "$SUMMER" 200 60 --hpad 101 ET
check "$SUMMER" 200 60 --hpad 100% ET
check "$SUMMER" 200 60 --hpad -5 ET
check "$SUMMER" 200 60 --hpad +5 ET
check "$SUMMER" 200 60 --hpad " 5" ET
check "$SUMMER" 200 60 --hpad 5%% ET
check "$SUMMER" 200 60 --hpad %5 ET
check "$SUMMER" 200 60 --hpad "" ET
check "$SUMMER" 200 60 --hpad twenty ET
check "$SUMMER" 200 60 --vpad 1000 ET
check "$SUMMER" 200 60 --vpad 50% -n 1 ET,PT,UTC
check "$SUMMER" 200 60 --hpad 50% ET,PT,UTC

echo "== width fit =="
for cols in 1 22 23 24 48 49 50 74 75 76 200; do
	for n in 1 2 3 4; do
		check "$SUMMER" "$cols" 60 ET,PT,UTC,JP,GB -n "$n"
	done
done

echo "== height fit =="
for lines in 11 12 13 24 25 26 38 39 60; do
	check "$SUMMER" 200 "$lines" ET,PT,UTC,JP,GB -n 2
done

echo "== unknown terminal size =="
env -u COLUMNS -u LINES CLOCK_FREEZE="$SUMMER" "$out/clock" ET,PT,UTC \
	>"$out/go.out" 2>"$out/go.err" </dev/null || true
env -u COLUMNS -u LINES CLOCK_FREEZE="$SUMMER" python3 clock.py ET,PT,UTC \
	>"$out/py.out" 2>"$out/py.err" </dev/null || true
if cmp -s "$out/go.out" "$out/py.out" && cmp -s "$out/go.err" "$out/py.err"; then
	pass=$((pass + 1))
	[ -z "$verbose" ] || echo 'ok   no COLUMNS/LINES, stdout piped'
else
	echo 'FAIL no COLUMNS/LINES, stdout piped'
	fail=$((fail + 1))
fi

echo "== cell ratios =="
for ratio in 1.7 2.0 2.1 2.4 2.9 3.3; do
	check "$SUMMER" 200 60 ET,PT,UTC -n 2
done
ratio=

# Every case below must be frozen: an unfrozen clock never exits on its own,
# and with stdin at /dev/null there is no q to stop it either.
echo "== freeze validation =="
check nonsense 200 60 ET
check 2026-07-15T09:53:07Z 200 60 ET
check 2026-07-15T09:53:07.123Z 200 60 ET
check 2026-13-99T09:53:07.123456Z 200 60 ET

echo "== embedded table parity =="
go_runs=$(sed -n 's/^const zipRuns = "\(.*\)".*/\1/p' clock.go)
py_runs=$(sed -n 's/^ZIP_RUNS = "\(.*\)".*/\1/p' clock.py)
if [ "$go_runs" = "$py_runs" ]; then
	pass=$((pass + 1))
	[ -z "$verbose" ] || printf 'ok   zip runs match (%s records)\n' "$((${#go_runs} / 4))"
else
	echo 'FAIL zip runs differ between clock.go and clock.py'
	fail=$((fail + 1))
fi

go_exc=$(sed -n 's/^const zipExceptions = "\(.*\)".*/\1/p' clock.go)
py_exc=$(sed -n 's/^ZIP_EXCEPTIONS = "\(.*\)".*/\1/p' clock.py)
if [ "$go_exc" = "$py_exc" ]; then
	pass=$((pass + 1))
	[ -z "$verbose" ] || printf 'ok   zip exceptions match (%s records)\n' "$((${#go_exc} / 6))"
else
	echo 'FAIL zip exceptions differ between clock.go and clock.py'
	fail=$((fail + 1))
fi

sed -n 's/^	{"\([A-Z]*\)", "\([A-Za-z_/]*\)"},$/\1=\2/p' clock.go >"$out/go.tab"
sed -n 's/^    ("\([A-Z]*\)", "\([A-Za-z_/]*\)"),$/\1=\2/p' clock.py >"$out/py.tab"
if [ -s "$out/go.tab" ] && cmp -s "$out/go.tab" "$out/py.tab"; then
	pass=$((pass + 1))
	[ -z "$verbose" ] || printf 'ok   alias and hint tables match (%s entries)\n' \
		"$(wc -l <"$out/go.tab" | tr -d ' ')"
else
	echo 'FAIL alias/hint tables differ between clock.go and clock.py'
	diff -u "$out/py.tab" "$out/go.tab" || true
	fail=$((fail + 1))
fi

sed -n "s/^	'\(.\)': \"\([A-Za-z_/]*\)\",\$/\1=\2/p" clock.go >"$out/go.zip"
sed -n 's/^    "\(.\)": "\([A-Za-z_/]*\)",$/\1=\2/p' clock.py >"$out/py.zip"
if [ -s "$out/go.zip" ] && cmp -s "$out/go.zip" "$out/py.zip"; then
	pass=$((pass + 1))
	[ -z "$verbose" ] || printf 'ok   zip letter tables match (%s entries)\n' \
		"$(wc -l <"$out/go.zip" | tr -d ' ')"
else
	echo 'FAIL zip letter tables differ between clock.go and clock.py'
	diff -u "$out/py.zip" "$out/go.zip" || true
	fail=$((fail + 1))
fi

# The key list is only reachable from a keypress, so no rendered case can
# cover it; the table it is built from can still be held to the same order.
sed -n 's/^	{"\([a-z]*\)", "\([A-Za-z, +]*\)"},$/\1=\2/p' clock.go >"$out/go.keys"
sed -n 's/^    ("\([a-z]*\)", "\([A-Za-z, +]*\)"),$/\1=\2/p' clock.py >"$out/py.keys"
if [ -s "$out/go.keys" ] && cmp -s "$out/go.keys" "$out/py.keys"; then
	pass=$((pass + 1))
	[ -z "$verbose" ] || printf 'ok   hotkey tables match (%s entries)\n' \
		"$(wc -l <"$out/go.keys" | tr -d ' ')"
else
	echo 'FAIL hotkey tables differ between clock.go and clock.py'
	diff -u "$out/py.keys" "$out/go.keys" || true
	fail=$((fail + 1))
fi

echo "== signal cleanup =="
for impl in "$out/clock" "python3 clock.py"; do
	# SIGTERM must still restore the cursor; without a handler Python dies
	# outright and hands back a terminal with the cursor still hidden.
	# shellcheck disable=SC2086
	env COLUMNS=200 LINES=60 $impl ET >"$out/term.out" 2>&1 </dev/null &
	term_pid=$!
	sleep 1
	kill "$term_pid" 2>/dev/null || true
	wait "$term_pid" 2>/dev/null || true
	if [ "$(tail -c 6 "$out/term.out")" = "$(printf '\033[?25h')" ]; then
		pass=$((pass + 1))
		[ -z "$verbose" ] || printf 'ok   %s restores the cursor on SIGTERM\n' "$impl"
	else
		printf 'FAIL %s left the cursor hidden after SIGTERM\n' "$impl"
		fail=$((fail + 1))
	fi
done

echo "== cross-compile gate =="
for target in linux/amd64 linux/arm64 linux/mips darwin/arm64 windows/amd64; do
	if GOOS=${target%/*} GOARCH=${target#*/} go build -o /dev/null . 2>"$out/build.err"; then
		pass=$((pass + 1))
		[ -z "$verbose" ] || printf 'ok   builds %s\n' "$target"
	else
		printf 'FAIL builds %s\n' "$target"
		cat "$out/build.err"
		fail=$((fail + 1))
	fi
done

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
