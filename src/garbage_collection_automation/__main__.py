"""Allows the package to be run with ``python -m garbage_collection_automation``."""

import sys

from . import main

if __name__ == "__main__":
    sys.exit(main())
