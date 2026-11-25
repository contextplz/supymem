#!/usr/bin/env python3
"""Checkout entry point for the packaged founder-only GTM proof CLI."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services/company_os"))

from app.gtm.proof import main, reviewed_request  # noqa: E402,F401


if __name__ == "__main__":
    main()
