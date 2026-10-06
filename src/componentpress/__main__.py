import sys


if __name__ == "__main__":
    if len(sys.argv) == 1:
        from .gui import main
    else:
        from .cli import main
    raise SystemExit(main())
