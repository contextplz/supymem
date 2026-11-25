from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.concierge.chat import respond


def test_still_stuck_live_diagnosis():
    step = {"id": "s1", "title": "Click Reveal", "if_stuck": "Use the eye icon"}
    first = respond("what next", last_step=step)
    stuck = respond("still stuck", last_step=step)
    assert stuck["kind"] == "diagnosis"
    assert stuck["text"] != first["text"]
    assert "Live diagnosis" in stuck["text"]
