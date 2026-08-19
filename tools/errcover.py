#!/usr/bin/env python3
"""Every error the clock can print, printed by some test.

difftest compares what the two implementations say. It cannot tell that a
message exists which nothing ever says -- a message edited in one file and not
the other, or one whose path no case walks, looks exactly like a message
nobody has broken yet.

So difftest keeps everything the Python clock wrote to stderr, and this reads
clock.py for the messages it can raise and checks each one turned up. What is
left is either a case worth adding or an exemption worth writing down.

    tools/errcover.py <stderr-log>
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Messages no test on a working machine can produce, with why. Both need a
# system whose time zone database is broken or absent, and any machine that
# can run this suite has one -- Go will not even give its up, falling back to
# the copy built into the binary when ZONEINFO points nowhere.
EXEMPT = {
    "no zone.tab under /usr/share/zoneinfo":
        "needs a machine with no tz database at all",
    "which this system's time zone database lacks":
        "needs a database holding some zones but not one the tables name",
}


def messages(path):
    """Every ClockError(...) in the file, as (line, longest literal run).

    The longest run is what to look for: the parts around a placeholder vary
    with the input, and the longest is both the most distinctive and the least
    likely to be a fragment shared with another message.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ClockError"):
            continue
        if not node.args:
            continue
        parts, stack = [], [node.args[0]]
        while stack:
            item = stack.pop()
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                parts.append(item.value)
            elif isinstance(item, ast.JoinedStr):
                parts.extend(v.value for v in item.values if isinstance(v, ast.Constant))
            elif isinstance(item, ast.BinOp):
                stack.extend([item.left, item.right])
        longest = max((p.strip() for p in parts), key=len, default="")
        # Anything shorter is a fragment like ", got " that would match half
        # the log by accident.
        if len(longest) >= 12:
            yield node.lineno, longest


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: tools/errcover.py <stderr-log>")
    printed = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")

    missing, exempted = [], 0
    for lineno, text in messages(ROOT / "clock.py"):
        if text in printed:
            continue
        why = next((reason for mark, reason in EXEMPT.items() if mark in text), None)
        if why:
            exempted += 1
            continue
        missing.append((lineno, text))

    if missing:
        print(f"FAIL {len(missing)} error messages no case produces:")
        for lineno, text in sorted(missing):
            print(f"     clock.py:{lineno}  {text[:80]}")
        print("     (add a case that reaches it, or an exemption saying why not)")
        return 1
    print(f"ok   every error message reachable by a test was printed by one "
          f"({exempted} exempt)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
