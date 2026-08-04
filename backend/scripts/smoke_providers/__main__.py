"""`python -m scripts.smoke_providers` entry point (the package's module
docstring carries the operator instructions and judgment calls)."""

from __future__ import annotations

import sys

from scripts.smoke_providers.cli import main

if __name__ == "__main__":
    sys.exit(main())
