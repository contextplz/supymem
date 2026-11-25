"""GTM God diagnosis, review, and the start→tick loop."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture(autouse=True)
def isolated_app():
    load_company_os_app()


def test_brief_tags_vague_idea_as_assumption():
    from app.gtm.god.brief import assumption_count, extract_brief

    brief = extract_brief(idea="A tool that remembers why we made a decision")
    assert brief["stage"] == "pre_users"
    assert brief["assumption_heavy"] is True
    assert brief["who"]["tag"] == "assumption"
    assert assumption_count(brief) >= 3


def test_diagnose_pre_users_talks_and_still_automates_research():
    from app.gtm.god.diagnose import Evidence, diagnose

    brief = {
        "assumption_heavy": True,
        "users_count": 0,
        "who": {"tag": "assumption", "text": "someone"},
    }
    d = diagnose(brief, Evidence())
    kinds = [s.kind for s in d.now]
    assert d.skill == "talk-to-users"
    assert "talk_in_person" in kinds
    assert "research_icp" in kinds
    assert "harvest_leads" not in kinds
    assert "linkedin_send" not in kinds


def test_diagnose_site_and_domain_in_parallel():
    from app.gtm.god.diagnose import Evidence, diagnose

    brief = {"assumption_heavy": False, "users_count": 3}
    d = diagnose(
        brief,
        Evidence(icp_ready=True, positioning_ready=True, users_count=3),
    )
    now = {s.kind for s in d.now}
    assert "site" in now
    assert "domain" in now


def test_review_blocks_puffery_and_linkedin_send():
    from app.gtm.god.review import review_artifact

    bad = review_artifact("A seamless revolutionary platform. We will send this on LinkedIn.")
    assert bad["ok"] is False
    assert any("puffery" in f for f in bad["flags"])
    assert any("linkedin" in f for f in bad["flags"])
    good = review_artifact("A first look for engineering leads who cannot find the last decision.")
    assert good["ok"] is True


def test_sandbox_skips_live_llm(monkeypatch):
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("LLM_OFFLINE", "false")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    from app.gtm.god.llm import json_chat, llm_enabled

    assert llm_enabled() is False
    assert json_chat(purpose="gtm.god_diagnose", system="x", user="y") is None
    from app.gtm.god.diagnose import assert_spawn_legal

    with pytest.raises(ValueError):
        assert_spawn_legal("harvest_leads")


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/god.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path / "firstlook"))
    load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    state_mod.reset_db_factory()
    return ensure_desk(LOCAL_PERSON_ID).id


@pytest.mark.parametrize("action", ["start", "tick", "complete"])
def test_legacy_mutations_require_approved_durable_intent(workspace, action):
    from app.gtm.god import service
    from app.db.models import GtmGodPlan
    from app.state import db
    from sqlalchemy import select
    with pytest.raises(service.GodError, match="gtm_quote_required"):
        if action == "start":
            service.start_plan(workspace, idea="Unquoted founder idea")
        elif action == "tick":
            service.tick(workspace)
        else:
            service.complete_human(workspace, "old-task", note="A note is not permission to launch")
    with db()() as session:
        assert not list(session.scalars(select(GtmGodPlan).where(GtmGodPlan.workspace_id == workspace)))
