"""V8 progress fixes: live-progress link, background cards, full titles, guarded replan, server builder step."""
import pytest
from fastapi import HTTPException

from tests.company_os_harness import load_company_os_app
from tests.unit.product.test_run_status import add_job, env  # noqa: F401 - shared fixture


def test_background_cards_are_truthful_and_link_to_now(env):  # noqa: F811
    factory, module = env
    add_job(factory, id="j_run", status="running", kind="idea.validate")
    add_job(factory, id="j_leased", status="pending", kind="idea.clarify_turn", age=1)
    cards = {c["job_id"]: c for c in module.snapshot("v1", "w1")["background_work"]}
    assert cards["j_run"]["state"] == "working" and cards["j_run"]["stage"] == "Validation"
    assert cards["j_leased"]["state"] == "queued"
    assert all(c["activity"] and c["owner_role"] and c["changed_at"] for c in cards.values())
    assert "secret" not in str(cards)


def test_task_run_card_names_task_and_owner(env):  # noqa: F811
    factory, module = env
    from app.db.models import Job, PlannedTask
    from core.clock import utcnow

    now = utcnow()
    with factory() as s:
        s.add(PlannedTask(id="t1", venture_id="v1", workspace_id="w1", title="Write the full supplier interview script",
                          why_now="x", tags_json="[]", est_minutes=5, runnable=1, source="chat", status="running", rank=1,
                          created_at=now, updated_at=now))
        s.add(Job(id="j_t", kind="task.run", venture_id="v1", workspace_id="w1", payload_json='{"task_id":"t1"}',
                  status="running", created_at=now))
        s.commit()
    card = module.snapshot("v1", "w1")["background_work"][0]
    assert card["activity"] == "Write the full supplier interview script"


@pytest.mark.parametrize("domain", ["Supplier invoice checks", "Family trail journals"])
@pytest.mark.parametrize("connected", [False, True])
def test_live_progress_goes_to_now_and_builder_step(monkeypatch, tmp_path, domain, connected):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.core.local_secrets import update_settings
    from app.core.providers_local import save_settings
    from app import coding_agents_consent
    from desktop_runtime import agent_status
    from app.ventures import next_step as ns
    from app.ventures import state_brief

    save_settings(provider="openai_compatible", base_url="http://127.0.0.1:1234", models={"chat": "test-model"})
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {"codex": connected})
    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "subscription_login": True, "available": True}])
    update_settings("capabilities", {"landing_pages": False}, {})
    base = dict(workspace_id="w1", gtm_entry=False, active_jobs=True, idea_state="validating", idea_confirmed=False,
                personal=False, plan_state=None, dossier_state=None, products=0, accepted_build=False,
                accepted_landing=False, product_attempt=False, accepted_product=False)
    assert ns._resolve("v1", "/v/v1", base)["href"] == "/v/v1/now"
    monkeypatch.setattr(state_brief, "_website_section", lambda *_a: {"live": False, "waitlist_enabled": False})
    confirmed = {**base, "active_jobs": False, "idea_state": "confirmed", "idea_confirmed": True,
        "dossier_state": "complete", "coding_tool": connected, "idea_summary": domain}
    monkeypatch.delenv("SUPYMEM_PAGEBUILDER_ENABLED", raising=False)
    assert ns._resolve("v1", "/v/v1", confirmed, page_first=True)["key"] != "site.build"
    monkeypatch.setenv("SUPYMEM_PAGEBUILDER_ENABLED", "1")
    assert ns._resolve("v1", "/v/v1", confirmed, page_first=True)["key"] != "site.build"
    update_settings("capabilities", {"landing_pages": True}, {})
    monkeypatch.setenv("SUPYMEM_PAGEBUILDER_ENABLED", "0")
    step = ns._resolve("v1", "/v/v1", confirmed, page_first=True)
    assert step["key"] == ("site.build" if connected else "site.connect_tool")
    assert not any(word in str(step).lower() for word in ("cost", "credit", "approve"))
    assert not step.get("chat_action")
    if connected:
        assert "your connected coding tool" in step["detail"].lower()
    else:
        assert step["href"] == "/settings/local"
        assert "Settings > Keys and connections > Coding tools" in step["detail"]
        assert ns._resolve("v1", "/v/v1", {**confirmed, "page_build": "ready"}, page_first=True)["key"] == "gtm.outreach"


def test_full_title_persisted_and_replan_blocked_without_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/v8.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app import state
    from app.api.tasks_api import replan_tasks
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession
    from app.planning.store import save_tasks
    from core.clock import utcnow

    state.reset_db_factory()
    state.db()
    long = "Name five supplier catalog normalization humans who would " + "really pay for this " * 8
    saved = save_tasks("v8", [{"title": long, "why_now": "x", "tags": []}], source="chat")
    assert saved[0]["title"] == long
    now = utcnow()
    with state.db()() as s:
        s.add(Venture(id="v8", workspace_id="w8", name="N", raw_idea="idea", stage="idea", created_at=now, updated_at=now))
        s.add(IdeaSession(id="ids8", case="A", workspace_id="w8", venture_id="v8", state="validating", created_at=now, updated_at=now))
        s.commit()
    with pytest.raises(HTTPException) as err:
        replan_tasks("v8")
    assert err.value.status_code == 409 and err.value.detail["error"]["code"] == "replan_needs_approved_plan"
