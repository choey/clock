.PHONY: run-go run-py regen

run-go:
	go run .

run-py:
	python3 clock.py

# Regenerates the ZIP tables in clock.go and clock.py in one pass -- see
# "When to regenerate" in README.md before reaching for this.
regen:
	tools/genzips.py
