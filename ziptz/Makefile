.PHONY: test test-go test-py regen

test: test-go test-py

test-go:
	go test ./...

test-py:
	python3 -m unittest -q test_ziptz

# Rewrites the tables in ziptz.go and ziptz.py from Census data -- see
# "Regenerating" in README.md, and expect to need it almost never. Needs
# timezonefinder, which nothing else here does:
#
#     python3 -m venv .venv && .venv/bin/pip install timezonefinder
#     .venv/bin/python tools/genzips.py
regen:
	tools/genzips.py
