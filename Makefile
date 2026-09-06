.PHONY: run-go run-py test

run-go:
	go run .

run-py:
	python3 clock.py

# Proves the two clocks render identical output and answer the keyboard alike.
# Needs ziptz importable for the Python side -- `pip install ziptz-us` -- since
# Go links it through go.mod and would otherwise resolve ZIPs the Python clock
# could not. difftest says so rather than reporting it as hundreds of diffs.
test:
	tools/difftest.sh
	tools/keytest.py
	tools/fitfuzz.py
	tools/argfuzz.py
	tools/docnums.py
