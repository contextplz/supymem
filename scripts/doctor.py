#!/usr/bin/env python3
"""Check your local installation without printing configuration or credentials."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root), str(root / "packages"), str(root / "services/company_os")]
    try:
        from scripts.dev import _load_dotenv_into

        _load_dotenv_into(os.environ)
        os.environ.setdefault("SUPYMEM_MODE", "local")
        from app.cli_local import main as local_main

        return local_main(["doctor", *sys.argv[1:]])
    except (ImportError, OSError, ValueError):
        print("Your local check could not start. Run make setup, check your private settings file permissions, then run make doctor.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
