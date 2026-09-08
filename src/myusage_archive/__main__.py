"""Allow ``python -m myusage_archive``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
