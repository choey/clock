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

	# ratio is a global the caller sets; passing it through env rather than
	# exporting it keeps one section's value out of the next one's cases.
	env CLOCK_FREEZE="$freeze" COLUMNS="$cols" LINES="$lines" \
		CLOCK_CELL_RATIO="${ratio:-}" \
		"$out/clock" "$@" >"$out/go.out" 2>"$out/go.err" </dev/null || true
	go_status=$?
	env CLOCK_FREEZE="$freeze" COLUMNS="$cols" LINES="$lines" \
		CLOCK_CELL_RATIO="${ratio:-}" \
		python3 clock.py "$@" >"$out/py.out" 2>"$out/py.err" </dev/null || true
	py_status=$?

	label="[$cols x $lines${ratio:+ r=$ratio}] $*"
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
check "$SUMMER" 200 60 --help
check "$SUMMER" 200 60 -h
check "$SUMMER" 200 60 ET --help

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
