"""ziptz as a package: the library itself is one file, ziptz.py beside this.

Two ways in, because the library is copied as often as it is installed. A
clone imports the directory -- `python3 clock.py` from the repository root
finds `ziptz/` before anything installed -- and this hands straight through to
the module. Copy `ziptz.py` somewhere on its own and it imports as itself,
with no package around it. Both give the same names.
"""

from .ziptz import *  # noqa: F401,F403
from .ziptz import EXCEPTIONS, GENERIC, RUNS, ZONES, __version__  # noqa: F401
