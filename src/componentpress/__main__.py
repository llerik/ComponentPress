import sys
from pathlib import Path


if __name__ == "__main__":
    if (len(sys.argv) == 1 or sys.argv[1] in {"--release-smoke", "--release-smoke-open"}
            or Path(sys.argv[1]).suffix.casefold() in {".cpress", ".yaml"}):
        from .gui import main
    else:
        from .cli import main
    raise SystemExit(main())
