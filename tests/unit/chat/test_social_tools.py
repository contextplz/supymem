"""Chat tools that walk a non-technical founder through Zernio + X."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app, local_chat as local_chat
from tests.company_os_harness import configure_local_test_model

_FAKE_KEY = "sk_" + ("ab" * 32)


@pytest.fixture(params=["Warehouse maintenance scheduling", "Family hiking itineraries"])
def env(tmp_path, monkeypatch, request):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/social_chat.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("COMPANY_OS_BRAIN_GRAPH_PATH", str(tmp_path / "graph.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    monkeypatch.delenv("ZERNIO_API_KEY", raising=False)
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
        try:
            yield client, created.json()["id"], monkeypatch
        finally:
            import os

            os.environ.pop("ZERNIO_API_KEY", None)


def test_walkthrough_names_real_zernio_screens():
    load_company_os_app()
    from app.concierge.social_guide import walkthrough_markdown

    md = walkthrough_markdown()
    assert "Continue with Google" in md
    assert "Create API Key" in md
    assert "developer.x.com" in md
    assert "x.com" in md
    assert walkthrough_markdown(step_id="connect_x").count("### ") == 1


def test_social_setup_and_status_keep_posting_restricted(env):
    from app.chat.actions import disabled_reason
    from app.core.local_capabilities import blocked_message
    from app.core.local_mode import is_local

    if not is_local():
        return
    assert disabled_reason("social_guide") == blocked_message("social_posting")
    assert disabled_reason("social_status") == blocked_message("social_posting")
    assert disabled_reason("social_post") == blocked_message("social_posting")
    assert disabled_reason("social_connect") == blocked_message("social_posting")


def test_social_guide_via_chat(env, monkeypatch):
    from app.chat import connect
    from app.connections import hub
    from app.core.local_capabilities import blocked_message
    from app.core.local_mode import is_local

    client, vid, _monkey = env
    monkeypatch.setattr(hub, "connect_provider", lambda *args, **kwargs: pytest.fail("Connection help must not authorize an account"))
    monkeypatch.setattr(connect, "_save_watch", lambda *args, **kwargs: pytest.fail("Connection help must not start a watch"))
    assert not connect.wants_connect("Explain how to connect X")
    reply = client.post(f"/ventures/{vid}/chat", json={"message": "Explain how to connect X"})
    assert reply.status_code == 200, reply.text
    assert "zernio" not in reply.text.lower()
    if is_local():
        assert blocked_message("social_posting") in reply.json()["body_md"]
    else:
        assert "Connect an account" in reply.json()["body_md"]
        assert "X is not available yet" in reply.json()["body_md"]


def test_social_status_without_key(env):
    from app.core.local_capabilities import blocked_message
    from app.core.local_mode import is_local

    client, vid, _monkey = env
    reply = client.post(f"/ventures/{vid}/chat", json={"message": "posting isn't working"})
    assert reply.status_code == 200, reply.text
    body = reply.json()["body_md"].lower()
    if is_local():
        assert blocked_message("social_posting").lower() in body and "draft your post" in body
    else:
        assert "posting from supymem is not switched on" in body and "write your post" in body
    assert "zernio" not in body and "dry_run" not in body and "dry-run" not in body


def test_pasted_posting_key_is_never_echoed_or_named(env):
    client, vid, _monkey = env
    preview = client.post(f"/ventures/{vid}/chat", json={"message": f"here is my zernio key {_FAKE_KEY}"})
    assert preview.status_code == 200, preview.text
    assert _FAKE_KEY not in preview.text and "ZERNIO" not in preview.json()["body_md"]

