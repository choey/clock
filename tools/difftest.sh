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

# Both clocks resolve ZIP codes through the ziptz library in this tree rather
# than whichever one is installed: Go through the replace directive in go.mod,
# Python because clock.py's own directory comes first on sys.path and ziptz/
# is a package sitting in it.

out=${TMPDIR:-/tmp}/clock-difftest.$$
mkdir -p "$out"
# Every complaint the Python clock makes, kept for the error coverage check at
# the bottom: a message nothing ever prints is a message nothing tests.
: >"$out/all.err"
trap 'rm -rf "$out"' EXIT

verbose=${1:-}
pass=0
fail=0

# Build once: `go run .` recompiles per invocation, and there are hundreds.
go build -o "$out/clock" .

# One sequence: frames and step (ms), then a normal check() case. CLOCK_FRAMES
# steps the pinned instant and draws that many frames, so this compares what a
# single frame cannot show -- the second hand sweeping, the rewind repainting
# over the frame before it, and the faces regrouping mid-run.
sequence() {
	frames=$1 step=$2
	shift 2
	check "$@"
	frames= step=
}

# One golden frame: a name to store it under, then a normal check() case.
#
# Everything else in this file compares the two implementations against each
# other, which cannot see a change that alters the picture in both -- and both
# is how they are always changed, in one pass. These pin the picture itself: the
# bytes were looked at once, and stay until someone deliberately accepts new
# ones. That the two agree is still checked first; a golden only means anything
# once it does.
#
# BLESS=1 tools/difftest.sh rewrites them all. Do that on purpose, and read the
# diff in the commit -- it is the only review these get.
golden() {
	name=$1
	shift
	check "$@" || true
	if [ -n "${BLESS:-}" ]; then
		cp "$out/go.out" "tools/golden/$name"
		printf 'blessed %s\n' "$name"
		return
	fi
	if [ ! -f "tools/golden/$name" ]; then
		printf 'FAIL golden %s is missing (BLESS=1 to create it)\n' "$name"
		fail=$((fail + 1))
	elif cmp -s "$out/go.out" "tools/golden/$name"; then
		pass=$((pass + 1))
		[ -z "$verbose" ] || printf 'ok   golden %s\n' "$name"
	else
		printf 'FAIL golden %s: the rendering changed (BLESS=1 to accept)\n' "$name"
		diff -u "tools/golden/$name" "$out/go.out" | head -20 || true
		fail=$((fail + 1))
	fi
}

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
	# In an if, not `|| true`: `cmd || true` leaves $? holding true's status,
	# so both sides read 0 however the clock exited and the comparison below
	# was comparing nothing. An if condition is exempt from set -e too.
	if env CLOCK_FREEZE="$freeze" COLUMNS="$cols" LINES="$lines" \
		CLOCK_CELL_RATIO="${ratio:-}" TZDIR="${tzdir:-}" \
		CLOCK_FRAMES="${frames:-}" CLOCK_STEP="${step:-}" \
		"$out/clock" "$@" >"$out/go.out" 2>"$out/go.err" </dev/null
	then go_status=0
	else go_status=$?
	fi
	if env CLOCK_FREEZE="$freeze" COLUMNS="$cols" LINES="$lines" \
		CLOCK_CELL_RATIO="${ratio:-}" TZDIR="${tzdir:-}" \
		CLOCK_FRAMES="${frames:-}" CLOCK_STEP="${step:-}" \
		python3 clock.py "$@" >"$out/py.out" 2>"$out/py.err" </dev/null
	then py_status=0
	else py_status=$?
	fi

	cat "$out/py.err" >>"$out/all.err"
	label="[$cols x $lines${ratio:+ r=$ratio}${tzdir:+ tz=$tzdir}${frames:+ x$frames}${step:+ @${step}ms}] $*"
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
# Offsets that are not whole hours, which the ordering sorts on and the merge
# keys off: Chatham is +12:45, Eucla +8:45, Kathmandu +5:45, Kolkata +5:30.
check "$SUMMER" 200 60 Pacific/Chatham,Australia/Eucla,Asia/Kathmandu,Asia/Kolkata
check "$SUMMER" 200 60 Pacific/Chatham,UTC,Australia/Eucla
check "$WINTER" 200 60 Pacific/Chatham,Australia/Eucla,Asia/Kathmandu,Asia/Kolkata
check "$SUMMER" 200 60 --day=always Pacific/Chatham,Asia/Kolkata

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
check "$SUMMER" 200 60 -n auto ET,PT,UTC
check "$SUMMER" 200 60 -nauto ET,PT,UTC
check "$SUMMER" 200 60 --per-row auto ET,PT,UTC
check "$SUMMER" 200 60 --per-row=auto ET,PT,UTC
check "$SUMMER" 200 60 -n auto --scale 1 ET,PT,UTC
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
check "$SUMMER" 200 60 --color=off ET
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
check "$SUMMER" 200 60 -q ET
check "$SUMMER" 200 60 --quiet ET
check "$SUMMER" 200 60 --quiet=x ET
check "$SUMMER" 200 60 -q --quiet ET
check "$SUMMER" 200 60 --cell-ratio=2.6 ET
check "$SUMMER" 200 60 --cell-ratio 2.6 ET
check "$SUMMER" 200 60 --cell-ratio
check "$SUMMER" 200 60 --cell-ratio 0 ET
check "$SUMMER" 200 60 --cell-ratio -1 ET
check "$SUMMER" 200 60 --cell-ratio bogus ET
check "$SUMMER" 200 60 --cell-ratio inf ET
check "$SUMMER" 200 60 --cell-ratio nan ET
check "$SUMMER" 200 60 --scale=1.5 ET,PT
check "$SUMMER" 200 60 --scale 1.5 ET,PT
check "$SUMMER" 200 60 --scale 0.5 ET,PT
check "$SUMMER" 200 60 --scale
check "$SUMMER" 200 60 --scale 0 ET
check "$SUMMER" 200 60 --scale -1 ET
check "$SUMMER" 200 60 --scale bogus ET
check "$SUMMER" 200 60 --scale inf ET
check "$SUMMER" 200 60 --scale nan ET
check "$SUMMER" 200 60 --scale 0.1 ET
check "$SUMMER" 200 60 --scale 100 ET
check "$SUMMER" 200 60 --scale=auto ET
check "$SUMMER" 200 60 --scale auto ET
check "$SUMMER" 200 60 --help
check "$SUMMER" 200 60 -h
check "$SUMMER" 200 60 ET --help
check "$SUMMER" 200 60 --version
check "$SUMMER" 200 60 ET --version
check "$SUMMER" 200 60 --version=1
check "$SUMMER" 200 60 --version 1
check "$SUMMER" 200 60 -- --version
# --version stops where it is found, so a bad flag before it still complains
# and a bad flag after it is never reached -- the same rule --help follows,
# and the pair of cases that pins which side of it wins.
check "$SUMMER" 200 60 --bogus --version
check "$SUMMER" 200 60 --version --bogus

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
# --scale 1 pins every case in this section to the fixed default size: the
# weekday's own visibility depends on a 16-column-wide face (dayCols), which
# --scale auto -- the default -- would not otherwise hold steady.
echo "== order and days =="
for at in 00:00:00.000000 05:02:41.901000 11:00:00.000000 23:59:59.999999; do
	for zones in PT,UTC,10001,JP,GB UTC,GMT,PST,PT,PDT JST,PT,UTC NZ,HT,UTC \
		Pacific/Kiritimati,Etc/GMT+12,UTC local,JST ET; do
		check "2026-07-15T${at}Z" 200 60 --scale 1 --day=auto "$zones" -n 5
		check "2026-01-15T${at}Z" 200 60 --scale 1 --day=auto "$zones" -n 5
		check "2026-07-15T${at}Z" 200 60 --scale 1 "$zones" -n 5
	done
done
check "$SUMMER" 200 60 --scale 1 UTC,JP,NZ,HT,PT,GB,IN -n 3
check "$SUMMER" 200 60 --scale 1 --color=always UTC,JP,PT

# A 16-column face is the narrowest that holds "Wed 05:02:41.901": 1.5 clears
# it at 17 columns, 1.4 falls one short at 15 and drops the weekday instead.
for ratio in 1.4 1.5; do
	check "2026-07-15T05:02:41.901000Z" 200 60 --scale 1 UTC,PT
done
ratio=

# Every case above lays out with the even fill and centred, which is the
# default; these pin the alignments, the fixed paddings and the arithmetic
# where a gap does not divide evenly.
# --scale 1 pins every case in this section (and layout grammar, width fit,
# height fit, and cell ratios below) to the fixed default size: these test
# the padding/alignment spread and the fit boundary at a known face size, and
# --scale auto -- the default since it maximises the face instead -- would
# leave little or no leftover space for any of that to exercise.
echo "== layout =="
for size in "100 30" "101 31" "75 13" "80 24" "200 60" "23 13" "49 27" \
	"300 80" "400 100" "76 14"; do
	set -- $size
	for geo in "" "--halign left" "--halign right" "--valign top" \
		"--valign bottom" "--halign left --valign top" \
		"--hpad 10%" "--hpad 0" "--hpad 100%" "--hpad even" \
		"--vpad 10%" "--vpad 0" "--vpad even" \
		"--hpad 5% --vpad 5% --halign right --valign bottom"; do
		check "$SUMMER" "$1" "$2" --scale 1 $geo ET,PT,UTC
		check "$SUMMER" "$1" "$2" --scale 1 $geo -n 2 ET,PT,UTC,JP
	done
done

echo "== layout grammar =="
check "$SUMMER" 200 60 --scale 1 --halign=center ET
check "$SUMMER" 200 60 --scale 1 --halign center ET
check "$SUMMER" 200 60 --scale 1 --halign bogus ET
check "$SUMMER" 200 60 --scale 1 --halign top ET
check "$SUMMER" 200 60 --scale 1 --valign left ET
check "$SUMMER" 200 60 --scale 1 --halign ET
check "$SUMMER" 200 60 --scale 1 --halign
check "$SUMMER" 200 60 --scale 1 --hpad
check "$SUMMER" 200 60 --scale 1 --hpad 10 ET
check "$SUMMER" 200 60 --scale 1 --hpad 10% ET
check "$SUMMER" 200 60 --scale 1 --hpad=10% ET
check "$SUMMER" 200 60 --scale 1 --hpad 010 ET
check "$SUMMER" 200 60 --scale 1 --hpad 101 ET
check "$SUMMER" 200 60 --scale 1 --hpad 100% ET
check "$SUMMER" 200 60 --scale 1 --hpad -5 ET
check "$SUMMER" 200 60 --scale 1 --hpad +5 ET
check "$SUMMER" 200 60 --scale 1 --hpad " 5" ET
check "$SUMMER" 200 60 --scale 1 --hpad 5%% ET
check "$SUMMER" 200 60 --scale 1 --hpad %5 ET
check "$SUMMER" 200 60 --scale 1 --hpad "" ET
check "$SUMMER" 200 60 --scale 1 --hpad twenty ET
check "$SUMMER" 200 60 --scale 1 --vpad 1000 ET
check "$SUMMER" 200 60 --scale 1 --vpad 50% -n 1 ET,PT,UTC
check "$SUMMER" 200 60 --scale 1 --hpad 50% ET,PT,UTC

echo "== width fit =="
for cols in 1 22 23 24 48 49 50 74 75 76 200; do
	for n in 1 2 3 4; do
		check "$SUMMER" "$cols" 60 --scale 1 ET,PT,UTC,JP,GB -n "$n"
	done
done

echo "== height fit =="
for lines in 11 12 13 24 25 26 38 39 60; do
	check "$SUMMER" 200 "$lines" --scale 1 ET,PT,UTC,JP,GB -n 2
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
	check "$SUMMER" 200 60 --scale 1 ET,PT,UTC -n 2
done
ratio=
for r in 1.7 2.0 2.4 2.9 3.3; do
	check "$SUMMER" 200 60 --scale 1 --cell-ratio "$r" ET,PT,UTC -n 2
done
# The flag wins when both are set.
ratio=1.7
check "$SUMMER" 200 60 --scale 1 --cell-ratio 2.9 ET,PT,UTC -n 2
ratio=

echo "== auto scale =="
for size in "80 24" "100 30" "200 60" "40 15" "300 90"; do
	set -- $size
	check "$SUMMER" "$1" "$2" --scale auto ET
	check "$SUMMER" "$1" "$2" --scale auto ET,PT,UTC
	check "$SUMMER" "$1" "$2" --scale auto ET,PT,UTC,JP,GB,NZ
	check "$SUMMER" "$1" "$2" --scale auto -n 1 ET,PT,UTC
	check "$SUMMER" "$1" "$2" --scale auto -n 2 ET,PT,UTC,JP
	check "$SUMMER" "$1" "$2" --scale auto --hpad 10% ET,PT,UTC
	check "$SUMMER" "$1" "$2" --scale auto --vpad 5% --valign top ET,PT,UTC
	check "$SUMMER" "$1" "$2" --scale auto --cell-ratio 2.6 ET,PT,UTC
done
check "$SUMMER" 10 5 --scale auto ET,PT,UTC
check "$SUMMER" 0 0 --scale auto ET,PT,UTC

# Windows sized so the unconstrained maximum (ignoring the symmetry
# preference) would land exactly on an even rowsN -- 24 and 26, reported
# live against Ghostty as visibly misaligned -- to pin that autoScale steps
# off them rather than just happening to most of the time.
for cols in 51 56; do
	check "$SUMMER" "$cols" 40 --scale auto UTC
done

# -n auto (the default, alongside --scale auto) searches per-row counts too,
# so a lopsided window -- wide and short, or narrow and tall -- can still
# grow the face as big as a squarer one would: extreme aspect ratios are
# exactly where a fixed per-row cap would have left it smaller than it had to
# be.
echo "== per-row auto =="
for size in "300 15" "15 300" "400 20" "20 400" "500 10" "10 500"; do
	set -- $size
	check "$SUMMER" "$1" "$2" ET,PT,UTC,JP,GB,NZ
	check "$SUMMER" "$1" "$2" -n auto ET,PT,UTC,JP,GB,NZ
	check "$SUMMER" "$1" "$2" -n 3 ET,PT,UTC,JP,GB,NZ
done
check "$SUMMER" 80 24 --scale 1 -n auto ET,PT,UTC,JP,GB,NZ
check "$SUMMER" 80 24 --scale 2 -n auto ET,PT,UTC,JP,GB,NZ

# Every case below must be frozen: an unfrozen clock never exits on its own,
# and with stdin at /dev/null there is no q to stop it either.
echo "== freeze validation =="
check nonsense 200 60 ET
check 2026-07-15T09:53:07Z 200 60 ET
check 2026-07-15T09:53:07.123Z 200 60 ET
check 2026-13-99T09:53:07.123456Z 200 60 ET

# Everything above compares one frame. These compare a run of them: CLOCK_FRAMES
# steps the pinned instant and draws that many, so the diff covers what a single
# frame cannot show -- the second hand sweeping between whole seconds, the
# rewind repainting over the frame before it, and the faces regrouping when a
# zone crosses a boundary mid-run. That last is the only state in the frame loop
# nothing else here reaches: the merge is cached on the second, and one frame
# never outlives its cache.
echo "== frame sequences =="
sequence 60 19 "$SUMMER" 200 60 ET,PT,UTC                   # a full second of sweep
sequence 60 19 "$SUMMER" 80 24 UTC                          # and in a small window
sequence 12 250 "$SUMMER" 200 60 ET,PT,UTC                  # coarser steps, three seconds
sequence 8 0 "$SUMMER" 200 60 ET,UTC                        # a still frame, repainted
sequence 21 19 2026-07-15T09:59:59.900000Z 200 60 ET,UTC    # over a minute
sequence 21 19 2026-07-15T23:59:59.900000Z 200 60 UTC       # over midnight
sequence 21 19 2026-12-31T23:59:59.900000Z 200 60 UTC       # over a year

# Daylight saving, in both directions: ET falls back onto the fixed EST and the
# two faces become one, then springs forward and they part again. Nothing else
# in this file can see that happen -- it needs two frames on either side of the
# instant it happens at.
sequence 21 19 2026-11-01T05:59:59.900000Z 200 60 ET,PT,EST,UTC
sequence 21 19 2026-03-08T09:59:59.900000Z 200 60 ET,PT,EST,UTC
sequence 21 19 2026-11-01T05:59:59.900000Z 200 60 --day auto ET,PT,EST,UTC

# Before 1970, where a negative instant runs through every date and offset
# calculation in both ports. Not a test of the merge cache's key: that is
# floored in both, but truncating instead would still draw the same frames,
# since the only second the two keys would disagree about is the one
# straddling the epoch and no zone changes offset inside it.
sequence 60 19 1969-12-31T23:59:59.500000Z 200 60 ET,UTC
sequence 21 19 1969-07-20T20:17:39.900000Z 200 60 ET,PT,UTC

# Odd values, not all of them wrong: "09" is nine frames in both, "٣" is a
# digit only to Python's isdigit(), and ten digits is one too many for Atoi to
# be trusted with. Whether each is taken or refused matters less than the two
# implementations agreeing on which.
echo "== sequence validation =="
check "$SUMMER" 200 60 ET  # baseline: the vars empty, as every case above has them
for odd in 0 abc -1 1.5 +1 " 1" 1234567890 09 ٣; do
	frames=$odd
	step=
	check "$SUMMER" 200 60 ET
	frames=
	step=$odd
	check "$SUMMER" 200 60 ET
done
frames=
step=

# CLOCK_FRAMES without CLOCK_FREEZE is refused before the loop starts, so this
# exits on its own -- but a regression would leave a clock running forever, so
# the output is bounded rather than trusted. head closes the pipe, and both
# implementations die on the write.
echo "== unfrozen sequence validation =="
for unfrozen in "CLOCK_FRAMES=3" "CLOCK_STEP=19" "CLOCK_FRAMES=1 CLOCK_STEP=19"; do
	# set +e inside the subshell: the clock is meant to fail here, and set -e
	# would take the subshell down with it before the status was written.
	( set +e; env -u CLOCK_FREEZE COLUMNS=200 LINES=60 $unfrozen \
		"$out/clock" ET 2>"$out/go.err"; echo $? >"$out/go.st" ) |
		head -c 4096 >"$out/go.out"
	( set +e; env -u CLOCK_FREEZE COLUMNS=200 LINES=60 $unfrozen \
		python3 clock.py ET 2>"$out/py.err"; echo $? >"$out/py.st" ) |
		head -c 4096 >"$out/py.out"
	cat "$out/py.err" >>"$out/all.err"
	if cmp -s "$out/go.out" "$out/py.out" && cmp -s "$out/go.err" "$out/py.err" &&
		cmp -s "$out/go.st" "$out/py.st"; then
		pass=$((pass + 1))
		[ -z "$verbose" ] || printf 'ok   [unfrozen] %s\n' "$unfrozen"
	else
		printf 'FAIL [unfrozen] %s\n' "$unfrozen"
		diff -u "$out/py.err" "$out/go.err" | head -10 || true
		fail=$((fail + 1))
	fi
done

# One of each kind of picture the clock can draw, kept as bytes. Few enough to
# read in a diff, spread wide enough that most rendering changes touch one.
echo "== golden frames =="
golden one-face          "$SUMMER" 80 24 UTC
golden grid-2x2          "$SUMMER" 80 24 -n 2 ET,PT,UTC,JP
golden stacked           "$SUMMER" 60 40 -n 1 ET,PT
golden wide-row          "$SUMMER" 200 30 ET,PT,UTC,JP,GB,NZ
golden colour            "$SUMMER" 80 24 --color=always UTC
golden weekday           "$SUMMER" 120 24 --day=always ET,JP
golden zip               "$SUMMER" 80 24 94110
golden scale-2           "$SUMMER" 120 40 --scale 2 UTC
golden auto-scale        "$SUMMER" 200 60 -n auto ET,PT,UTC
golden aligned           "$SUMMER" 120 40 --halign right --valign bottom UTC
# 02:00Z is the point: ET is still on Tuesday where JP is on Wednesday, so
# --day=auto turns the weekday on. At $SUMMER it stays off.
golden date-split        2026-07-15T02:00:00.000000Z 120 24 --day=auto ET,JP
golden date-together     "$SUMMER" 120 24 --day=auto ET,JP
golden too-narrow        "$SUMMER" 20 24 ET,PT,UTC
golden fixed-offset      "$SUMMER" 80 24 PST,PDT
golden winter            "$WINTER" 120 24 ET,PT,UTC
frames=3
step=19
golden sequence-3        "$SUMMER" 80 24 UTC
frames=
step=

# The one thing the Python clock does that the Go one cannot: run without
# ziptz. Go links the library in at build time; Python imports it if it is
# there and names it if it is not, so every zone name still works and only ZIP
# tokens are refused. Nothing else can reach that message -- a clone always has
# ziptz/ sitting beside clock.py.
echo "== without ziptz =="
mkdir -p "$out/alone"
cp clock.py "$out/alone/clock.py"
alone_err="$out/alone.err"
if (cd "$out/alone" && env -u PYTHONPATH COLUMNS=80 LINES=24 \
	CLOCK_FREEZE="$SUMMER" python3 clock.py 94110 >/dev/null 2>"$alone_err"); then
	echo 'FAIL a ZIP without ziptz should have failed'
	fail=$((fail + 1))
elif grep -q 'is a ZIP code, and resolving one needs the ziptz' "$alone_err"; then
	pass=$((pass + 1))
	[ -z "$verbose" ] || printf 'ok   a ZIP without ziptz says what to install\n'
else
	echo 'FAIL a ZIP without ziptz said something unexpected:'
	cat "$alone_err"
	fail=$((fail + 1))
fi
cat "$alone_err" >>"$out/all.err"

# ... and everything else still works there, since only ZIP tokens need it.
if (cd "$out/alone" && env -u PYTHONPATH COLUMNS=80 LINES=24 \
	CLOCK_FREEZE="$SUMMER" python3 clock.py ET,PT,Berlin >/dev/null 2>"$alone_err"); then
	pass=$((pass + 1))
	[ -z "$verbose" ] || printf 'ok   zone names still work without ziptz\n'
else
	echo 'FAIL without ziptz, a clock with no ZIP in it should still run:'
	cat "$alone_err"
	fail=$((fail + 1))
fi

echo "== embedded table parity =="
go_runs=$(sed -n 's/^const runs = "\(.*\)".*/\1/p' ziptz/ziptz.go)
py_runs=$(sed -n 's/^RUNS = "\(.*\)".*/\1/p' ziptz/ziptz.py)
if [ "$go_runs" = "$py_runs" ]; then
	pass=$((pass + 1))
	[ -z "$verbose" ] || printf 'ok   zip runs match (%s records)\n' "$((${#go_runs} / 4))"
else
	echo 'FAIL zip runs differ between ziptz.go and ziptz.py'
	fail=$((fail + 1))
fi

go_exc=$(sed -n 's/^const exceptions = "\(.*\)".*/\1/p' ziptz/ziptz.go)
py_exc=$(sed -n 's/^EXCEPTIONS = "\(.*\)".*/\1/p' ziptz/ziptz.py)
if [ "$go_exc" = "$py_exc" ]; then
	pass=$((pass + 1))
	[ -z "$verbose" ] || printf 'ok   zip exceptions match (%s records)\n' "$((${#go_exc} / 6))"
else
	echo 'FAIL zip exceptions differ between ziptz.go and ziptz.py'
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

# Each of the two programs says its version in three places, and a release
# that disagrees with itself is a bug report waiting to happen: Go's const,
# Python's, and the one pip stamps into the wheel. The --version cases above
# already prove the two clocks print the same string as each other; this is
# what proves it is the string the package was built under.
version_agrees() {
	what=$1 go_ver=$2 py_ver=$3 toml_ver=$4
	if [ -n "$go_ver" ] && [ "$go_ver" = "$py_ver" ] && [ "$go_ver" = "$toml_ver" ]; then
		pass=$((pass + 1))
		[ -z "$verbose" ] || printf 'ok   %s version agrees everywhere (%s)\n' "$what" "$go_ver"
	else
		printf 'FAIL %s version differs: go=%s py=%s pyproject=%s\n' \
			"$what" "$go_ver" "$py_ver" "$toml_ver"
		fail=$((fail + 1))
	fi
}

version_agrees ziptz \
	"$(sed -n 's/^const Version = "\(.*\)"$/\1/p' ziptz/ziptz.go)" \
	"$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' ziptz/ziptz.py)" \
	"$(sed -n 's/^version = "\(.*\)"$/\1/p' ziptz/pyproject.toml)"

version_agrees clock \
	"$(sed -n 's/^const version = "\(.*\)"$/\1/p' clock.go)" \
	"$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' clock.py)" \
	"$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml)"

sed -n "s/^	'\(.\)': \"\([A-Za-z_/]*\)\",\$/\1=\2/p" ziptz/ziptz.go >"$out/go.zip"
sed -n 's/^    "\(.\)": "\([A-Za-z_/]*\)",$/\1=\2/p' ziptz/ziptz.py >"$out/py.zip"
if [ -s "$out/go.zip" ] && cmp -s "$out/go.zip" "$out/py.zip"; then
	pass=$((pass + 1))
	[ -z "$verbose" ] || printf 'ok   zip letter tables match (%s entries)\n' \
		"$(wc -l <"$out/go.zip" | tr -d ' ')"
else
	echo 'FAIL zip letter tables differ between ziptz.go and ziptz.py'
	diff -u "$out/py.zip" "$out/go.zip" || true
	fail=$((fail + 1))
fi

sed -n 's|^	"\([A-Za-z_/]*\)": *"\([A-Za-z]*\)",$|\1=\2|p' ziptz/ziptz.go >"$out/go.gen"
sed -n 's|^    "\([A-Za-z_/]*\)": "\([A-Za-z]*\)",$|\1=\2|p' ziptz/ziptz.py >"$out/py.gen"
if [ -s "$out/go.gen" ] && cmp -s "$out/go.gen" "$out/py.gen"; then
	pass=$((pass + 1))
	[ -z "$verbose" ] || printf 'ok   generic name tables match (%s entries)\n' \
		"$(wc -l <"$out/go.gen" | tr -d ' ')"
else
	echo 'FAIL generic name tables differ between ziptz.go and ziptz.py'
	diff -u "$out/py.gen" "$out/go.gen" || true
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

echo "== error coverage =="
# Not `out=$(...)`: out is the scratch directory the EXIT trap removes, and
# shadowing it here would leave the directory behind and try to remove a
# message instead.
if coverage=$(tools/errcover.py "$out/all.err"); then
	pass=$((pass + 1))
	[ -z "$verbose" ] || echo "$coverage"
else
	echo "$coverage"
	fail=$((fail + 1))
fi

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
