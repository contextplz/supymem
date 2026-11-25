from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.gtm.planner import plan_campaign


def test_instagram_app_review_lead_time():
    plan = plan_campaign(
        channels=["instagram"],
        actions_per_channel={"instagram": 5},
        budget_usd=100,
    )
    assert plan.ok
    assert plan.earliest_start_days >= 21
    assert any(t["prereq"] == "app_review" for t in plan.human_tasks)
