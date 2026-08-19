.PHONY: test test-go test-py sweep regen

test: test-go test-py sweep

test-go:
	go test ./...

test-py:
	python3 -m unittest -q test_ziptz

# Every ZIP there is, through both libraries, compared. The tests above cover
# the cases someone thought of; this covers the ones nobody did -- all 1,000
# prefixes and all 100,000 five-digit codes, answers and error text alike.
sweep:
	@out=$${TMPDIR:-/tmp}/ziptz-sweep.$$$$; mkdir -p $$out; \
	go run tools/sweep.go >$$out/go && python3 tools/sweep.py >$$out/py && \
	if cmp -s $$out/go $$out/py; then \
		echo "sweep: $$(wc -l <$$out/go | tr -d ' ') answers, identical"; \
	else \
		echo "sweep: the two libraries disagree:"; diff $$out/py $$out/go | head -20; \
		rm -rf $$out; exit 1; \
	fi; rm -rf $$out

# Rewrites the tables in ziptz.go and ziptz.py from Census data -- see
# "Regenerating" in README.md, and expect to need it almost never. Needs
# timezonefinder, which nothing else here does:
#
#     python3 -m venv .venv && .venv/bin/pip install timezonefinder
#     .venv/bin/python tools/genzips.py
regen:
	tools/genzips.py
