"""Entry point: python -m catchrate <command>."""

import sys

from catchrate.cli import main

if __name__ == "__main__":
    sys.exit(main())
