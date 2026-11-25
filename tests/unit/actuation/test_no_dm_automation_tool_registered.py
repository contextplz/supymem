"""A115-10 — no LinkedIn/X DM automation tools in the actuation registry."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

import app.actuation.tools  # noqa: F401 — register tools
from app.actuation.registry import all_tools


def test_no_dm_automation_tool_registered():
    ids = {t.tool_id for t in all_tools()}
    banned = {
        "linkedin.dm",
        "x.dm",
        "twitter.dm",
        "linkedin.message",
        "social.dm",
        "browser.linkedin_dm",
    }
    assert not (ids & banned)
    assert "email.sequence" in ids
    assert "email.read" in ids
