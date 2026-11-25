"""Founder-facing control: export, honest dashboard, no silent live posts."""

from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app
from tests.security.test_core_signed_boundary import client as signed_client, headers, make_workspace


@pytest.mark.parametrize("idea", ["Parts procurement for machine shops", "Art workshops for children"])
def test_download_includes_saved_work_without_private_or_foreign_content(signed_client, monkeypatch, tmp_path, idea):
    import asyncio
    from app.db.models import ChatConversation, ChatMessage, PlannedTask, VentureDocument
    from app.memory import MemoryKind, MemoryWrite
    from app.memory.adapters.local import LocalMemoryAdapter
    from app.state import db
    from app.ventures import founder_memory
    from app.ventures.startup_review import PRIVATE_KINDS
    from core.clock import utcnow

    make_workspace(signed_client, "usr_alice")
    created = signed_client.post("/ventures", json={"name": idea, "raw_idea": idea, "start": False},
                                headers=headers("/ventures", "POST"))
    assert created.status_code == 201, created.text
    venture = created.json()
    now = utcnow()
    memory = LocalMemoryAdapter(tmp_path / "export-memory.db")
    monkeypatch.setattr(founder_memory, "get_memory", lambda: memory)
    asyncio.run(memory.remember(MemoryWrite(venture_id=venture["id"], kind=MemoryKind.FOUNDER_PREFERENCE,
        subject="budget", statement=f"Keep the launch budget small for {idea}.", confidence=1,
        author="usr_alice", dedupe_key="budget", evidence=["founder:settings"])))
    with db()() as session:
        session.add(PlannedTask(id="task_export", venture_id=venture["id"], workspace_id=venture["workspace_id"],
            title=idea, detail=f"Saved task details for {idea}", why_now="Ready to review", created_at=now, updated_at=now))
        for conversation_id, owner, workspace in [("cnv_own", "usr_alice", venture["workspace_id"]),
                ("cnv_other", "usr_bob", venture["workspace_id"]), ("cnv_wrong", "usr_alice", "wsp_foreign")]:
            session.add(ChatConversation(id=conversation_id, venture_id=venture["id"], workspace_id=workspace,
                creator_person_id=owner, visibility="private", title=idea, creation_key=conversation_id,
                created_at=now, updated_at=now))
        session.flush()
        for message_id, conversation_id, workspace, text in [
                ("msg_own", "cnv_own", venture["workspace_id"], f"Saved launch decisions for {idea}"),
                ("msg_other", "cnv_other", venture["workspace_id"], "FOREIGN_PRIVATE_TEXT"),
                ("msg_wrong", "cnv_wrong", "wsp_foreign", "FOREIGN_WORKSPACE_TEXT")]:
            session.add(ChatMessage(id=message_id, venture_id=venture["id"], workspace_id=workspace,
                conversation_id=conversation_id, role="user", body_md=text, created_at=now))
        for document_id, kind, body in [("doc_saved", "plan", f"Saved plan for {idea}. sk-test0123456789abcdefghijkl"),
                ("doc_private", next(iter(PRIVATE_KINDS)), "PRIVATE_DOCUMENT_TEXT")]:
            session.add(VentureDocument(id=document_id, venture_id=venture["id"], kind=kind, title=idea,
                body_md=body, created_at=now, updated_at=now))
        session.commit()
    path = f"/ventures/{venture['id']}/export"
    response = signed_client.post(path, headers=headers(path, "POST"))
    assert response.status_code == 200, response.text
    pack = response.json()
    assert pack["venture"]["raw_idea"] == idea
    assert any(f"Saved plan for {idea}" in doc["body_md"] for doc in pack["documents"])
    assert f"Saved launch decisions for {idea}" in response.text
    assert f"Saved task details for {idea}" in response.text
    assert f"Keep the launch budget small for {idea}." in response.text
    for hidden in ("FOREIGN_PRIVATE_TEXT", "FOREIGN_WORKSPACE_TEXT", "PRIVATE_DOCUMENT_TEXT", "sk-test0123456789abcdefghijkl"):
        assert hidden not in response.text
    assert signed_client.post(path, headers=headers(path, "POST", "usr_bob")).status_code == 404

    def unavailable():
        raise RuntimeError("memory unavailable")

    monkeypatch.setattr(founder_memory, "get_memory", unavailable)
    retry = signed_client.post(path, headers=headers(path, "POST"))
    assert retry.status_code == 200, retry.text
    assert retry.json()["memories"]["availability"] == "unavailable"
    assert "Try downloading again" in retry.json()["memories"]["note"]
    assert f"Saved plan for {idea}" in retry.text


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/control.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    from app.core.local_mode import is_local
    from tests.unit.test_local_mode import _owner_client
    with (_owner_client(main.create_app()) if is_local() else TestClient(main.create_app())) as c:
        yield c


def _venture(client: TestClient) -> str:
    created = client.post(
        "/ventures",
        json={"raw_idea": "waitlist for neighborhood tool libraries"},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_dashboard_exposes_control_and_honest_counts(client: TestClient):
    vid = _venture(client)
    dash = client.get(f"/ventures/{vid}/dashboard")
    assert dash.status_code == 200, dash.text
    body = dash.json()
    control = body["control"]
    assert control["you_own_this"] is True
    assert control["posts_need_you"] is True
    assert "domain" in control["domain_policy"].lower()
    assert control["landing"]["href"] == f"/v/{vid}/site"
    assert isinstance(body["governance"]["pending_approvals"], int)
    tasks = body["tiles"]["tasks"]
    assert isinstance(tasks["running"], int)
    assert tasks["running"] >= 0


def test_export_and_support_bundle_have_no_secrets(client: TestClient):
    vid = _venture(client)
    exported = client.get(f"/ventures/{vid}/export")
    assert exported.status_code == 200, exported.text
    pack = exported.json()
    assert pack["you_own_this"] is True
    assert pack["venture"]["id"] == vid
    blob = exported.text.lower()
    assert "api_key" not in blob
    assert "sk-" not in blob

    stuck = client.get(f"/ventures/{vid}/support-bundle")
    assert stuck.status_code == 200, stuck.text
    bundle = stuck.json()
    assert bundle["venture_id"] == vid
    assert "api keys" in bundle["note"].lower()
    assert "api_key" not in stuck.text.lower()


def test_missing_venture_export_is_404(client: TestClient):
    assert client.get("/ventures/ven_missing/export").status_code == 404
    assert client.get("/ventures/ven_missing/support-bundle").status_code == 404


def test_autopilot_social_never_sets_allow_live():
    load_company_os_app()
    from app.planning import execute as execute_mod

    src = inspect.getsource(execute_mod._run_social_post)
    assert "allow_live=False" in src
    assert "publishNow" in src and "False" in src
