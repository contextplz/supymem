"""Owned fixture pages for the round-1 browser test program (docs/supymem/beta-2026-10-04/browser-tests/*.yaml).

Extends the base FixtureServer with the pages the designer specified. Every fact derives from `uid`; every POST is
recorded by the base server (fx.posts / fx.post_receipts). Nothing here talks to a real service.
"""
from __future__ import annotations

import html
import os
import tempfile
from pathlib import Path

from cu_tasks.fixture_server import FixtureServer

e = html.escape
SERIF = {"Content-Type": "application/json"}


def form(action, fields, button="Submit", extra=""):
    return f"<form method=post action='{action}'>{fields}{extra}<button>{button}</button></form>"


from core.browser_qualification_catalog import Round1FixtureState  # noqa: E402


class Fx2(Round1FixtureState, FixtureServer):
    """Existing localhost transport over the shared pure fixture state."""
    def __init__(self, uid):
        super().__init__(uid)
        self.echo_receipts = bool(os.environ.get("CU_R1_ECHO"))
        self._upload_dir = Path(tempfile.mkdtemp(prefix="cu_r1_up_"))
        self.upload_path = self._upload_dir / f"avatar-{uid[:4]}.txt"
        self.upload_path.write_bytes(self.upload_bytes)

    def close(self):
        super().close()
        for path in self._upload_dir.glob("*"):
            path.unlink(missing_ok=True)
        if self._upload_dir.exists():
            self._upload_dir.rmdir()
