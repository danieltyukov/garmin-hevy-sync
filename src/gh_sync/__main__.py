"""``python -m gh_sync``: what the background schedulers run.

Scheduler entries invoke the interpreter rather than the console script so the
path they store survives reinstalls, and so Windows can use ``pythonw.exe`` to
run without flashing a console window.
"""

from .cli import main

raise SystemExit(main())
