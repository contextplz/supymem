"""Checkout entry point for the packaged founder-only release proof CLI."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "services/company_os"), str(ROOT / "packages")]

from app.email.proof import ALLOWED_RECIPIENT, check, main, queue, unsubscribe  # noqa: E402,F401


if __name__ == "__main__":
    main()
