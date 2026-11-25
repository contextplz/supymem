"""Unit tests for marketing plan → jobs → verify → visuals."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app
from tests.content_fixtures import filled_brief


def test_visual_spec_tier0_derives_headline():
    load_company_os_app()
    from app.content.visuals.spec import build_spec

    spec = build_spec(
        body="We got the architecture wrong.\n\nTwo months, thirty agents, one mistake.",
        brand={"wordmark": "Supymem", "accent": "#1B4D3E"},
    )
    assert spec["template"] in {
        "insight_card",
        "stat_card",
        "quote_card",
        "before_after",
        "screenshot_frame",
    }
    assert "architecture" in spec["headline"].lower() or "wrong" in spec["headline"].lower()
    assert spec["kicker"] == "SUPYMEM"


def test_publish_proof_requires_live_id(monkeypatch):
    load_company_os_app()
    from app.marketing import verify

    monkeypatch.setenv("CONTENT_DRY_RUN", "false")
    ok, reason = verify.publish_proof_ok(
        {"mode": "live", "external_id": None, "external_url": ""},
        require_live_id=True,
    )
    assert not ok
    assert "missing" in reason

    ok2, _ = verify.publish_proof_ok(
        {
            "mode": "live",
            "external_id": "urn:li:share:123",
            "external_url": "https://www.linkedin.com/feed/update/urn:li:share:123/",
        },
        require_live_id=True,
    )
    assert ok2


def test_block_hints_detect_captcha():
    load_company_os_app()
    from app.marketing.verify import is_block_error

    assert is_block_error("CAPTCHA required")
    assert is_block_error("login expired — reconnect")
    assert not is_block_error("lint:too_long")


def test_strategist_and_compiler_create_job_chains(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/mkt.db")
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path}/mkt.db")
    monkeypatch.setenv("CONTENT_DRY_RUN", "true")
    load_company_os_app()

    from app.content import brief as brief_store
    from app.marketing import store
    from app.marketing.strategist import create_plan_from_brief
    from app.state import reset_db_factory
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    reset_db_factory()
    desk = ensure_desk(LOCAL_PERSON_ID)
    brief_store.save(workspace_id=desk.id, data=filled_brief(), source_name="t.json")
    out = create_plan_from_brief(workspace_id=desk.id, horizon_days=14, compile_days=7)
    assert out["ok"]
    assert out["jobs_created"] > 0

    jobs = store.list_jobs(desk.id, plan_id=out["plan"]["id"], limit=300)
    assert any(j["kind"] == "create_content" for j in jobs)
    assert any(j["kind"] == "publish_content" for j in jobs)
    assert any(j["kind"] == "create_visual" and j["platform"] == "linkedin" for j in jobs)
    # Dependency chain: publish depends on visual or create.
    publish = next(j for j in jobs if j["kind"] == "publish_content" and j["platform"] == "linkedin")
    assert publish["depends_on"]


def test_router_prefers_api_for_publish():
    load_company_os_app()
    from app.marketing.router import route

    decision = route(
        {
            "id": "mjb_x",
            "kind": "publish_content",
            "platform": "linkedin",
            "title": "Publish",
            "payload": {"platform": "linkedin"},
            "depends_on": [],
            "status": "pending",
        },
        plan={"strategy": {"content": {"visuals": {"enabled": True}}}, "constraints": {}},
    )
    assert decision["lane"] == "api"
    assert decision["adapter"] == "content_engine"
    assert "image_attached_when_visual_path_exists" in decision["workorder"]["success_criteria"]


def test_router_check_result_uses_hermes_browser():
    load_company_os_app()
    from app.marketing.router import route
    from app.marketing.schema import default_constraints

    decision = route(
        {
            "id": "mjb_z",
            "kind": "check_result",
            "platform": "linkedin",
            "title": "Verify",
            "payload": {"verify_browser": True, "external_url": "https://example.com"},
            "depends_on": [],
            "status": "pending",
        },
        plan={"constraints": default_constraints()},
    )
    assert decision["lane"] == "hermes"
    assert decision["adapter"] == "hermes_browser"


def test_workorder_lists_hermes_research_for_find():
    load_company_os_app()
    from app.marketing.schema import default_constraints
    from app.marketing.workorder import workorder_from_job

    wo = workorder_from_job(
        {
            "id": "mjb_y",
            "kind": "find_threads",
            "platform": "reddit",
            "title": "Find",
            "payload": {},
            "depends_on": [],
            "status": "pending",
        },
        plan={"constraints": default_constraints()},
    )
    assert "hermes_research" in wo["tools_allowed"]


def test_compiler_emits_check_result_after_linkedin_publish(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/mkt2.db")
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path}/mkt2.db")
    monkeypatch.setenv("CONTENT_DRY_RUN", "true")
    load_company_os_app()

    from app.content import brief as brief_store
    from app.marketing import store
    from app.marketing.strategist import create_plan_from_brief
    from app.state import reset_db_factory
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    reset_db_factory()
    desk = ensure_desk(LOCAL_PERSON_ID)
    brief_store.save(workspace_id=desk.id, data=filled_brief(), source_name="t.json")
    out = create_plan_from_brief(workspace_id=desk.id, horizon_days=14, compile_days=7)
    jobs = store.list_jobs(desk.id, plan_id=out["plan"]["id"], limit=300)
    checks = [j for j in jobs if j["kind"] == "check_result" and j["platform"] == "linkedin"]
    assert checks
    assert any(j["depends_on"] for j in checks)


def test_render_png_smoke(tmp_path):
    pytest.importorskip("PIL")
    load_company_os_app()
    from app.content.visuals.render import render_png
    from app.content.visuals.spec import build_spec

    spec = build_spec(
        body="Dwell time is the ranking function.\n\nLinkedIn is not ranking AI vs human.",
        brand={"wordmark": "Company OS"},
    )
    dest = tmp_path / "card.png"
    result = render_png(spec, dest)
    assert result["ok"], result
    assert dest.exists()
    assert dest.stat().st_size > 1000


def test_handoff_maps_research_to_brief_and_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/mkt_handoff.db")
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path}/mkt_handoff.db")
    monkeypatch.setenv("CONTENT_DRY_RUN", "true")
    load_company_os_app()

    from app.content import brief as brief_store
    from app.marketing.handoff import brief_dict_from_research, create_plan_from_research
    from app.state import reset_db_factory
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    reset_db_factory()
    desk = ensure_desk(LOCAL_PERSON_ID)
    data = brief_dict_from_research(
        research={
            "summary": "AI GTM for seed founders",
            "competitors": [{"name": "Clay"}],
            "claims": [{"text": "Found 3 leads in under a minute"}],
            "strongest_for": ["Runs the whole loop"],
            "demand_signals": ["Founders hate empty calendars"],
        },
        venture={"name": "Supymem", "raw_idea": "AI GTM operator"},
    )
    assert data["product"]["name"] == "Supymem"
    assert data["claims"]

    out = create_plan_from_research(
        workspace_id=desk.id,
        research={
            "summary": "AI GTM for seed founders",
            "competitors": [{"name": "Clay"}],
            "claims": [{"text": "Found 3 leads in under a minute"}],
            "strongest_for": ["Runs the whole loop"],
        },
        horizon_days=14,
        compile_days=7,
    )
    assert out["ok"]
    assert out["handoff"] == "research→brief→plan"
    assert brief_store.active(desk.id) is not None
    assert out["jobs_created"] > 0


def test_compiler_emits_outbound_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/mkt_out.db")
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path}/mkt_out.db")
    monkeypatch.setenv("CONTENT_DRY_RUN", "true")
    load_company_os_app()

    from app.content import brief as brief_store
    from app.marketing import compiler, store
    from app.marketing.schema import default_constraints, empty_strategy
    from app.state import reset_db_factory
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    reset_db_factory()
    desk = ensure_desk(LOCAL_PERSON_ID)
    brief_store.save(workspace_id=desk.id, data=filled_brief(), source_name="t.json")
    strategy = empty_strategy()
    strategy["outbound"]["enabled"] = True
    strategy["content"]["platforms"] = {}
    plan = store.save_plan(
        workspace_id=desk.id,
        strategy=strategy,
        constraints=default_constraints(),
        brief_id=None,
        horizon_days=14,
    )
    jobs = compiler.compile_plan(workspace_id=desk.id, plan=plan, days=7)
    kinds = {j["kind"] for j in jobs}
    assert "enqueue_outreach" in kinds
    assert "send_email_touch" in kinds


def test_router_outbound_and_ads_lanes():
    load_company_os_app()
    from app.marketing.router import route
    from app.marketing.schema import default_constraints

    plan = {"constraints": default_constraints()}
    send = route(
        {
            "id": "mjb_mail",
            "kind": "send_email_touch",
            "platform": "email",
            "title": "Send",
            "payload": {},
            "depends_on": [],
            "status": "pending",
        },
        plan=plan,
    )
    assert send["lane"] == "code"
    assert send["adapter"] == "gtm_outreach"

    google = route(
        {
            "id": "mjb_ad",
            "kind": "create_ad",
            "platform": "google",
            "title": "Ad",
            "payload": {"platform": "google"},
            "depends_on": [],
            "status": "pending",
        },
        plan=plan,
    )
    assert google["adapter"] == "ads_google"

    enable = route(
        {
            "id": "mjb_en",
            "kind": "enable_ad",
            "platform": "meta",
            "title": "Enable",
            "payload": {"platform": "meta", "campaign_id": "123"},
            "depends_on": [],
            "status": "pending",
        },
        plan=plan,
    )
    assert enable["adapter"] == "ads_meta"


def test_ads_fail_closed_without_credentials(monkeypatch):
    load_company_os_app()
    monkeypatch.delenv("GOOGLE_ADS_DEVELOPER_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_ADS_CUSTOMER_ID", raising=False)
    monkeypatch.delenv("META_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("META_AD_ACCOUNT_ID", raising=False)
    from app.marketing.media import probes

    g = probes.create_paused_ad(platform="google", name="Test")
    assert g["status"] == "blocked"
    assert "GOOGLE_ADS" in (g.get("block_reason") or "")

    m = probes.create_paused_ad(platform="meta", name="Test")
    assert m["status"] == "blocked"
    assert "META_" in (m.get("block_reason") or "")

    en = probes.enable_ad(platform="meta", resource_id="camp_1")
    assert en["status"] == "blocked"
    assert "spend_money" in (en.get("block_reason") or "")


def test_learning_summarize_and_apply(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/mkt_learn.db")
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path}/mkt_learn.db")
    load_company_os_app()

    from app.marketing import learning
    from app.marketing.schema import empty_strategy
    from app.state import reset_db_factory
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    reset_db_factory()
    desk = ensure_desk(LOCAL_PERSON_ID)
    learning.record(desk.id, "done_publish_content", platform="linkedin")
    summary = learning.summarize(desk.id)
    assert summary["events"] >= 1
    assert summary["recommendations"]
    strategy = learning.apply_to_strategy(empty_strategy(), summary)
    assert "learning" in strategy
