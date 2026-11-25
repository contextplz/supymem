"""A founder asks once for a published page with a waitlist: build, waitlist and publish all follow, with no cards."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app, local_chat as local_chat, local_publishing as local_publishing

DOMAINS = [("a contract audit tool for hospital compliance officers", "Build and publish a landing page to test demand from compliance officers, with a form to join the waitlist."),
           ("satellite methane monitoring for pipeline operators", "Create a landing page for pipeline operators and publish it"),
           ("weekend trip planning for families", "Create a landing page for families and publish it")]


@pytest.fixture()
def env(tmp_path, monkeypatch, request):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/site.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    from app import state as state_mod
    from app.memory import reset_memory_for_tests

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    reset_memory_for_tests(tmp_path / "mem.sqlite3")
    with TestClient(main.create_app()) as client:
        made = client.post("/ventures", json={"raw_idea": request.param[0]})
        assert made.status_code == 201, made.text
        yield client, made.json()["id"], made.json()["workspace_id"], request.param[1]


def _builder(monkeypatch, *, current=None, page_url="https://published.example.test"):
    from app.api import pagebuilder_api
    from app.chat import site_actions
    from app.conversion import waitlist
    from app.pagebuilder import api

    calls = {"start": 0, "configure": 0, "remember": [], "publish": []}
    def start(*args, **kwargs):
        calls["start"] += 1
        return {"job_id": "job_page", "state": "queued"}
    monkeypatch.setattr(api, "enabled", lambda: True)
    monkeypatch.setattr(api, "estimate", lambda *a, **k: {"ready": True, "quote_fingerprint": "fp", "ceiling_micro": 1})
    monkeypatch.setattr(api, "_start", start)
    monkeypatch.setattr(pagebuilder_api, "profile", lambda *a, **k: {"current": current, "plan_id": None, "task_id": None})
    monkeypatch.setattr(site_actions, "_remember_publish", calls["remember"].append)
    monkeypatch.setattr(site_actions, "publish_preflight", lambda v: (None, None, [("Build it first", {"label": "Build"}, "no_hosted_app")]))
    monkeypatch.setattr(site_actions, "_waitlist_state", lambda v: {"ok": True, "enabled": False})
    monkeypatch.setattr(waitlist, "configure", lambda *a, **k: calls.__setitem__("configure", calls["configure"] + 1) or {"url": "https://g/x"})
    monkeypatch.setattr("app.pagebuilder.local_publish.publish_site", lambda vid, actor, *, job_id=None:
        calls["publish"].append({"job_id": job_id}) or {"url": page_url, "notice": "Your saved page is live."})
    return calls


@pytest.mark.parametrize("env", DOMAINS, indirect=True)
def test_one_request_uses_your_tool_or_offers_connection_without_a_card(env, monkeypatch):
    from app.core.local_mode import is_local

    client, vid, _, request = env
    if is_local():
        from desktop_runtime import agent_status

        monkeypatch.setattr(agent_status, "statuses", lambda: [])
    calls = _builder(monkeypatch)
    reply = client.post(f"/ventures/{vid}/chat", json={"message": request}).json()
    if is_local():
        assert calls["start"] == 0 and calls["configure"] == 0 and not calls["remember"]
        assert "Settings > Keys and connections > Coding tools" in reply["body_md"]
        assert any(action.get("href") for action in reply["actions"])
    else:
        assert calls["start"] == 1 and calls["configure"] == 1 and calls["remember"] == ["job_page"]
        assert "publish it and post the link" in reply["body_md"] and "public yet" in reply["body_md"]
    assert not any(action.get("confirmation") for action in reply["actions"])
    for internal in ("hosted app", "gateway.", "unverified", "One click"):
        assert internal not in reply["body_md"]
    assert not any(action.get("label") == "Run it for me" for action in reply["actions"])


@pytest.mark.parametrize("env", DOMAINS, indirect=True)
@pytest.mark.parametrize("page_url", ["https://published.example.test", "https://pages.example.test/sent/published?source=published"])
def test_a_ready_page_publishes_locally_or_queues_hosted_checks(env, monkeypatch, page_url):
    from app.pagebuilder_admission import publication
    from app.core.local_mode import is_local
    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    client, vid, _, request = env
    confirm_saved_idea(vid)
    current = {"job_id": "job_page", "state": "ready", "server_candidates": [{"variant": 1}], "publication": None}
    calls = _builder(monkeypatch, current=current, page_url=page_url)
    monkeypatch.setattr(publication, "request", lambda v, a, selection: calls["publish"].append(selection) or {"state": "checking"})
    reply = client.post(f"/ventures/{vid}/chat", json={"message": request}).json()
    assert calls["start"] == 0
    if is_local():
        assert calls["publish"] == [{"job_id": "job_page"}]
        assert f"live at {page_url}" in reply["body_md"]
        assert any(action.get("href") == page_url for action in reply["actions"])
    else:
        assert calls["publish"] == [{"job_id": "job_page", "variant": 1, "plan_id": None, "plan_task_id": None}]
        assert "final checks" in reply["body_md"] and "not public yet" in reply["body_md"]


@pytest.mark.parametrize("env", DOMAINS, indirect=True)
def test_failed_local_publish_keeps_the_page_private_and_names_the_settings_screen(env, monkeypatch):
    from app.core.local_mode import is_local
    from app.pagebuilder.local_publish import LocalPublishError

    if not is_local():
        pytest.skip("Local publishing contract")
    client, vid, _, request = env
    calls = _builder(monkeypatch, current={"job_id": "job_page", "state": "ready"})
    def failed(*args, **kwargs):
        raise LocalPublishError("In Settings > Keys and connections > Publishing, save your token, then publish again.")
    monkeypatch.setattr("app.pagebuilder.local_publish.publish_site", failed)
    reply = client.post(f"/ventures/{vid}/chat", json={"message": request}).json()
    assert calls["start"] == 0 and calls["publish"] == []
    assert "Settings > Keys and connections > Publishing" in reply["body_md"]
    assert "live at" not in reply["body_md"] and not any(action.get("confirmation") for action in reply["actions"])


@pytest.mark.parametrize("env", DOMAINS, indirect=True)
@pytest.mark.parametrize("asked", [True, False])
def test_a_finished_build_reaches_chat_and_continues_only_a_requested_publish(env, monkeypatch, asked):
    from app.chat import site_actions
    from app.chat.store import list_messages
    from app.core.jobs import enqueue
    from app.state import db
    from app.tenancy import bind

    client, vid, wid, _ = env
    continued = []
    monkeypatch.setattr(site_actions, "_publish_saved_page", lambda *a: continued.append(a) or {
        "kind": "text", "body_md": "Your landing page is going through its final checks.", "actions": []})
    monkeypatch.setattr("app.api.pagebuilder_api.profile", lambda *a, **k: {"current": {"job_id": "job"}})
    with db()() as session, bind(workspace_id=wid, venture_id=vid, actor="usr_test"):
        job = enqueue(session, "pagebuilder.build", {"_actor_subject": "usr_test", "_runtime": {"state": "ready"},
            **({"publish_requested": True} if asked else {})}, workspace_id=wid, venture_id=vid, dedupe_key="page:" + vid)
        session.commit()
    with bind(workspace_id=wid, venture_id=vid, actor="usr_test"):
        site_actions.page_built(job)
        site_actions.page_built(job)  # a replay posts nothing new
        posted = [m for m in list_messages(vid) if "landing page" in m["body_md"] and m["role"] == "assistant"]
    assert len(posted) == 1 and bool(continued) is asked
    assert ("final checks" if asked else "private until you publish") in posted[0]["body_md"]
    if not asked:
        assert [action["action"] for action in posted[0]["actions"] if action.get("action")] == ["site.publish", "site.preview"]
        prefix = f"/api/ventures/{vid}/pagebuilder/jobs/{job}"
        assert [(action["label"], action["href"]) for action in posted[0]["actions"] if action.get("href")] == [
            ("Open preview", f"{prefix}/preview"), ("Export", f"{prefix}/source")]


def test_two_parts_offering_one_card_save_it_once():
    load_company_os_app()
    from app.chat.router import _same_card

    card = {"label": "Open waitlist", "confirmation": {"action": "waitlist.enable", "resulting_state": {}}}
    assert _same_card(card, {**card, "style": "primary"})
    assert not _same_card(card, {"confirmation": {"action": "waitlist.enable", "resulting_state": {"kind": "other"}}})
    assert not _same_card(card, {"label": "Open waitlist", "action": "waitlist.enable"})


@pytest.mark.parametrize("text,expected", [
    ("Build and publish a landing page with a waitlist form", True),
    ("Make a landing page and put it live", True),
    ("Don't publish yet, just build a landing page", False),
    ("How do I build and publish a landing page?", False),
    ("Publish the blog post about our audit", False),
])
def test_only_a_request_for_the_whole_page_stays_one_action(text, expected):
    load_company_os_app()
    from app.chat.site_actions import builds_and_publishes

    assert builds_and_publishes(text) is expected


def test_requests_keep_only_the_audience_in_the_empty_search_copy():
    load_company_os_app()
    from app.chat import customers

    for who, note in (("compliance officers at US mid-size hospitals", "Find me 10 HIPAA compliance officers at mid-size US hospitals I can email."),
                      ("pipeline operators", "Find me 12 pipeline integrity managers we can contact")):
        reply = customers.empty_search_reply({"icp": {"who": who, "problem": "audits"}, "search_request": note, "discovery_note": note})
        first = reply["body_md"].split("\n\n")[0]
        assert "I can email" not in first and "we can contact" not in first and "Find me" not in first and ". " + who not in first
