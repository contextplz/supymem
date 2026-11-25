"""Founder chat must change the served site, remember competitors, and draft email."""

from __future__ import annotations

import hashlib
from html import escape
from pathlib import Path

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app
from tests.company_os_harness import configure_local_test_model


@pytest.fixture(params=["Equipment handovers for factory supervisors", "Weekend outings for families"])
def env(tmp_path, monkeypatch, request):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/steer.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("FIRSTLOOK_COPY_MODE", "fallback")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    main = load_company_os_app()
    configure_local_test_model()
    from app import state as state_mod
    from app.memory import reset_memory_for_tests

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    reset_memory_for_tests(tmp_path / "mem.sqlite3")
    with TestClient(main.create_app()) as client:
        created = client.post(
            "/ventures",
            json={"raw_idea": request.param},
        )
        assert created.status_code == 201, created.text
        yield client, created.json()["id"], created.json()["workspace_id"], tmp_path


def _saved_page(workspace_id, venture_id, root, copy):
    from app.firstlook.copy import copy_hash
    from app.firstlook.versions import persist

    html = "<!doctype html><html><body><h1>" + escape(copy["headline"]) + "</h1>"
    html += "".join("<p>" + escape(item) + "</p>" for item in copy.get("assumptions", [])) + "</body></html>"
    path = root / "saved-page.html"
    path.write_text(html, encoding="utf-8")
    return persist(workspace_id=workspace_id, venture_id=venture_id, stage="first_look",
        template="legacy_fixture", archetype="landing", brandkit_hash="legacy", copy_hash=copy_hash(copy),
        copy=copy, brandkit={}, variant_index=0, path=str(path), url=None,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def _page_tool(monkeypatch):
    from app.build import device_orders
    from tests.unit.pagebuilder.test_api import connect_local_tool

    connect_local_tool(monkeypatch)
    requests = []
    def prepare(venture_id, caller, instruction):
        requests.append((venture_id, caller, instruction))
        return {"id": "page-order", "venture_id": venture_id, "state": "pending", "review_digest": "page-review", "review_path": "/builds"}
    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", prepare)
    monkeypatch.setattr(device_orders, "transition", lambda venture_id, *args:
        {"id": "page-order", "venture_id": venture_id, "state": "approved", "review_path": "/builds"})
    return requests


@pytest.mark.asyncio
async def test_compound_steer_rewrites_site_and_drafts_email(env, monkeypatch):
    client, vid, wid, tmp_path = env
    from app.core.local_mode import is_local
    from app.firstlook.versions import get_latest

    leak = "A tight niche I know, even if I don't have a name yet"
    first = _saved_page(wid, vid, tmp_path, {"headline": "Your current page", "assumptions": [leak]})
    requested = _page_tool(monkeypatch) if is_local() else []
    before = first.sha256
    html_before = Path(first.path).read_text(encoding="utf-8")
    assert "A tight niche I know" in html_before

    reply = client.post(
        f"/ventures/{vid}/chat",
        json={
            "message": (
                "rewrite the headline to Stop waiting on the lobby, "
                "competitors are DoorDash, Toast, and Resy, "
                "draft an email to Priya, do not send"
            )
        },
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()["body_md"]
    assert "Queued" not in body
    if not is_local():
        assert "Stop waiting on the lobby" in body
    assert "DoorDash" in body
    assert "Priya" in body
    assert "companyos.local" not in body.lower()
    assert "Draft only" in body or "nothing went out" in body.lower()

    latest = get_latest(wid)
    assert latest is not None
    assert (latest.sha256 == before) if is_local() else (latest.sha256 != before)
    html = Path(latest.path).read_text(encoding="utf-8")
    if is_local():
        assert len(requested) == 1 and requested[0][0] == vid
        assert "Stop waiting on the lobby" in requested[0][2]
        assert "Do not publish, deploy, install packages, purchase or contact anyone" in requested[0][2]
        assert html == html_before and "working" in body
    else:
        assert "Stop waiting on the lobby" in html
        assert "A tight niche I know" not in html
    drafts = list((tmp_path / "ventures" / vid / "repo" / "drafts").glob("email-to-priya.md"))
    assert drafts
    assert "not sent" in drafts[0].read_text(encoding="utf-8").lower()


@pytest.mark.asyncio
async def test_site_copy_task_changes_html_or_is_not_done(env, monkeypatch):
    client, vid, wid, tmp_path = env
    from app.core.local_mode import is_local
    from app.firstlook.versions import get_latest
    from app.planning.store import enqueue_task

    first = _saved_page(wid, vid, tmp_path, {"headline": "Your current page"})
    requested = _page_tool(monkeypatch) if is_local() else []
    task = enqueue_task(
        vid,
        {
            "title": "Rewrite the landing headline to Guests wait outside. You shouldn't.",
            "detail": "rewrite the headline to Guests wait outside. You shouldn't.",
            "why_now": "Founder asked in chat.",
            "tags": ["chat", "landing"],
            "runnable": True,
        },
    )
    ran = client.post(f"/ventures/{vid}/tasks/{task['id']}/run")
    assert ran.status_code == 202, ran.text
    body = ran.json()
    assert body["status"] == ("queued" if is_local() else "running") and body["queued"] is True
    if is_local():
        assert body["device_work_order_id"] == "page-order"
    else:
        from app.planning.execute import run_task_job

        await run_task_job({"venture_id": vid, "task_id": task["id"]})
    from app.planning.store import get_task
    assert (get_task(vid, task["id"])["status"] != "done") if is_local() else (get_task(vid, task["id"])["status"] == "done")
    latest = get_latest(wid)
    assert latest is not None
    html = Path(latest.path).read_text(encoding="utf-8")
    if is_local():
        assert len(requested) == 1 and "Guests wait outside" in requested[0][2]
        assert latest.sha256 == first.sha256 and "Guests wait outside" not in html
    else:
        assert "Guests wait outside" in html
        assert latest.sha256 != first.sha256

    same = enqueue_task(vid, {"title": task["title"], "detail": task["detail"],
        "why_now": "Repeat with no new copy.", "tags": ["chat", "landing"], "runnable": True})
    again = client.post(f"/ventures/{vid}/tasks/{same['id']}/run")
    assert again.status_code == 202, again.text
    if is_local():
        assert again.json()["status"] == "queued" and len(requested) == 2
        assert get_latest(wid).sha256 == first.sha256
    else:
        await run_task_job({"venture_id": vid, "task_id": same["id"]})
    assert get_task(vid, same["id"])["status"] != "done"


def test_short_draft_priya_note_writes_file_without_a_task(env):
    client, vid, _wid, tmp_path = env
    from app.planning.store import enqueue_task, get_task

    parked = enqueue_task(
        vid,
        {
            "title": "draft Priya note",
            "detail": "Do this instead: draft Priya note.",
            "why_now": "You retargeted in-flight work to: “draft Priya note”",
            "tags": ["chat", "retarget"],
            "runnable": False,
        },
    )
    reply = client.post(
        f"/ventures/{vid}/chat",
        json={"message": "draft Priya note"},
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()["body_md"]
    assert "Queued" not in body
    assert "Priya" in body
    assert "Draft only" in body or "nothing went out" in body.lower()
    drafts = list((tmp_path / "ventures" / vid / "repo" / "drafts").glob("email-to-priya.md"))
    assert drafts
    assert "not sent" in drafts[0].read_text(encoding="utf-8").lower()
    closed = get_task(vid, parked["id"])
    assert closed is not None
    assert closed["status"] == "done"


def test_retarget_cancels_running_and_enqueues_new_brief(env):
    client, vid, _wid, _tmp = env
    from app.db.models import PlannedTask
    from app.planning.store import enqueue_task, get_task, retarget_front_task
    from app.state import db
    from core.clock import utcnow

    old = enqueue_task(
        vid,
        {
            "title": "Prototype the lobby lock core workflow",
            "detail": "Stub the workflow.",
            "why_now": "Playbook.",
            "tags": ["code"],
            "runnable": True,
        },
    )
    factory = db()
    with factory() as session:
        row = session.get(PlannedTask, old["id"])
        row.status = "running"
        row.updated_at = utcnow()
        session.commit()
    nxt = retarget_front_task(
        vid,
        "Draft an email to Priya about the waitlist",
        detail="draft an email to Priya, do not send",
    )
    assert nxt["id"] != old["id"]
    assert nxt["status"] == "open"
    assert "Priya" in nxt["title"]
    abandoned = get_task(vid, old["id"])
    assert abandoned["status"] == "dismissed"


def test_cancel_parks_discovery_and_blocks_refill(env):
    client, vid, _wid, _tmp = env
    from app.planning.cycle import refill_unused_work
    from app.planning.store import cancel_matching_work, enqueue_task, list_tasks

    enqueue_task(
        vid,
        {
            "title": "Capture lobby lock objection patterns",
            "detail": "Generic discovery.",
            "why_now": "Playbook.",
            "tags": ["insight"],
            "runnable": True,
        },
    )
    parked = cancel_matching_work(vid, "cancel generic discovery and split the work")
    assert parked
    assert all(row["status"] == "dismissed" for row in parked)
    filled = refill_unused_work(vid)
    titles = [row["title"] for row in filled] + [
        row["title"]
        for row in list_tasks(vid, statuses=("open", "running"))
    ]
    assert not any("objection" in title.lower() for title in titles)
