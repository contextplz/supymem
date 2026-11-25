"""OT-10: work orders cannot drive linkedin.com."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app


def test_linkedin_host_blocked():
    load_company_os_app()
    from app.gtm.relationships.denylist import linkedin_host_blocked

    assert linkedin_host_blocked("https://www.linkedin.com/in/ada")
    assert linkedin_host_blocked("https://linkedin.com/feed")
    assert linkedin_host_blocked("linkedin.com/in/ada")
    assert linkedin_host_blocked("https://api.linkedin.com/v2/me")
    assert not linkedin_host_blocked("https://example.com")
    assert not linkedin_host_blocked("")


def test_workorder_strips_browser_tools_for_linkedin_url():
    load_company_os_app()
    from app.marketing.workorder import workorder_from_job

    order = workorder_from_job(
        {
            "id": "job_1",
            "kind": "operate_browser",
            "platform": "web",
            "payload": {"url": "https://www.linkedin.com/in/ada"},
        }
    )
    assert "browser_operator" not in order["tools_allowed"]
    assert "hermes_browser" not in order["tools_allowed"]
