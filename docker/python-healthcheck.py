"""Check local readiness or hosted liveness. Exit 1 on failure."""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

port = os.environ.get("PORT", "8000")
local = os.environ.get("SUPYMEM_MODE", "").strip().lower() == "local"
path = "/local-ready" if local else "/health"
url = f"http://127.0.0.1:{port}{path}"
try:
    with urllib.request.urlopen(url, timeout=2) as resp:
        healthy = resp.status == 204 if local else 200 <= resp.status < 300
        if healthy:
            sys.exit(0)
except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
    pass
sys.exit(1)
