import sys

from app.cli import main

# `python -m app` must propagate the status `main` returns, or a cancelled
# `init` would report success to whatever invoked it.
sys.exit(main())
