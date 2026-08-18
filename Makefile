.PHONY: run-go run-py test test-lib regen

run-go:
	go run .

run-py:
	python3 clock.py

# Proves the two clocks render identical output, then that the two ziptz
# libraries answer identically.
test: test-lib
	tools/difftest.sh

test-lib:
	$(MAKE) -C ziptz test

# The ZIP tables belong to ziptz and are regenerated there.
regen:
	$(MAKE) -C ziptz regen
