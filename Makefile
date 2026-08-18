.PHONY: run-go run-py test test-lib regen

run-go:
	go run .

run-py:
	python3 clock.py

# Proves the two clocks render identical output, then the two ziptz libraries
# answer identically.
test: test-lib
	tools/difftest.sh

test-lib:
	cd ziptz && go test ./...
	cd ziptz && python3 -m unittest -q test_ziptz

# Regenerates the ZIP tables in ziptz/ziptz.go and ziptz/ziptz.py in one pass --
# see "When to regenerate" in README.md before reaching for this.
regen:
	tools/genzips.py
