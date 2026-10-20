"""Frozen Windows GUI entry point."""

import multiprocessing
import sys
from pathlib import Path


if __name__ == "__main__":
    multiprocessing.freeze_support()
    arguments = sys.argv[1:]
    if (not arguments or arguments[0] in {"--release-smoke", "--release-smoke-open"}
            or len(arguments) == 1 and Path(arguments[0]).suffix.casefold() in {".cpress", ".yaml"}):
        from componentpress.gui import main
    else:
        from componentpress.cli import main
    raise SystemExit(main(arguments))
