"""Frozen Windows GUI entry point."""

import multiprocessing
import sys

from componentpress.gui import main


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main(sys.argv[1:]))
