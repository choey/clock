.PHONY: run-go run-py setup test clean

# A .venv here, if there is one, wins for everything below -- `make setup`
# builds it. This is a PATH entry rather than a $(PYTHON) variable threaded
# through every tool because the harnesses run `python3 pyclock.py` and
# `#!/usr/bin/env python3` in a dozen places, and those should keep working
# unchanged whether the venv exists or not. Absent, the entry is inert and
# everything falls back to the system python3.
export PATH := $(CURDIR)/.venv/bin:$(PATH)

run-go:
	go run .

run-py:
	python3 pyclock.py

# The Python clock resolves ZIP codes through ziptz, which is a module of its
# own; the Go clock links it through go.mod and so always has it. That
# asymmetry is why this exists: without ziptz installed the two disagree on
# every ZIP, and a modern system python refuses `pip install` outright (PEP
# 668), so "just install it" is not advice that works everywhere.
#
# A local .venv is the answer that works on every machine and touches nothing
# outside this directory.
setup:
	python3 -m venv .venv
	.venv/bin/python -m pip install --quiet --upgrade pip
	.venv/bin/python -m pip install --quiet ziptz-us
	@printf 'setup: .venv ready -- ziptz %s\n' \
		"$$(.venv/bin/python -c 'import ziptz; print(ziptz.__version__)')"

# Proves the two clocks render identical output and answer the keyboard alike.
# Needs ziptz for the Python side; run `make setup` once if it is missing, and
# difftest says so rather than reporting it as hundreds of diffs.
test:
	tools/difftest.sh
	tools/keytest.py
	tools/fitfuzz.py
	tools/argfuzz.py
	tools/docnums.py

clean:
	rm -rf .venv dist build
