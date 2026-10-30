"""Internal entry point: run the interactive shell *directly*.

The launcher opens the shell in a new terminal window by spawning
``python -m quackers.internal``. This module mirrors the bare-interactive
branch of :func:`quackers.cli.main` but never re-launches a new window —
that decision is made by the (single) process the user actually invoked.

This file exists as its own module (not a hidden ``cli`` flag) so the
external user-facing command stays clean.
"""
from __future__ import annotations

import sys

from quackers import shell, store, term
from services.logger import setup_logging


def main() -> int:
    term.setup_stdout()
    setup_logging()
    store.ensure_db()
    return shell.run()


if __name__ == "__main__":
    sys.exit(main())
