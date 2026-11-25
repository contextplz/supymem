from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


def _seed_notice_venture():
    from app.db.models import Venture
    from app.state import db
    from app.workspaces.resolve import ensure_desk
    from core.clock import utcnow

    wid, now = ensure_desk().id, utcnow()
    with db()() as session:
        if session.get(Venture, "ven_1") is None:
            session.add(Venture(id="ven_1", workspace_id=wid, name="Notice QA", raw_idea="Notice proof", stage="draft",
                                autonomy_level="L1", created_at=now, updated_at=now))
            session.commit()


def test_enqueue_pending_ack(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/n.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DESKTOP_NOTICES", str(tmp_path / "n.jsonl"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    _seed_notice_venture()
    from app import desktop_notices as notices

    nid = notices.enqueue(title="Needs you", body="Approve a post", venture_id="ven_1")
    pending = notices.pending()
    assert len(pending) == 1
    assert pending[0]["id"] == nid
    assert pending[0]["target_path"] == "/v/ven_1/now"
    assert notices.ack([nid]) == 1
    assert notices.pending() == []


def test_notice_targets_reject_external_or_untrusted_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/n.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    _seed_notice_venture()
    from app import desktop_notices as notices

    with pytest.raises(ValueError, match="desktop_notice_target_invalid"):
        notices.enqueue(title="Bad", body="External", venture_id="ven_1", target_path="https://bad.test/")
    with pytest.raises(ValueError, match="desktop_notice_target_invalid"):
        notices.enqueue(title="Bad", body="Traversal", venture_id="ven_1", target_path="/v/ven_1/../settings")
    task_id = notices.enqueue(title="Task", body="Review", venture_id="ven_1",
                              target_path="/v/ven_1#attention-ht_exact")
    assert notices.pending()[0]["id"] == task_id
    assert notices.pending()[0]["target_path"] == "/v/ven_1#attention-ht_exact"
    notices.ack([task_id])
    nid = notices.enqueue(title="Analytics", body="Review", venture_id="ven_1", target_path="/v/ven_1/analytics")
    assert notices.pending()[0]["id"] == nid
    assert notices.pending()[0]["target_path"] == "/v/ven_1/analytics"
    notices.ack([nid])
    review = notices.enqueue(
        title="Review Mac task",
        body="Review its exact scope",
        venture_id="ven_1",
        target_path="/v/ven_1/device-work-orders/dwo_exact-2",
    )
    assert notices.pending()[0]["id"] == review
    assert notices.pending()[0]["target_path"] == "/v/ven_1/device-work-orders/dwo_exact-2"
    with pytest.raises(ValueError, match="desktop_notice_target_invalid"):
        notices.enqueue(
            title="Bad order",
            body="Review",
            venture_id="ven_1",
            target_path="/v/ven_1/device-work-orders/not-an-order",
        )


def test_desktop_notices_api(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/n.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DESKTOP_NOTICES", str(tmp_path / "n.jsonl"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    main = load_company_os_app()
    from app import desktop_notices as notices
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as client:
        notices.enqueue(title="Night digest", body="Two watch hits", kind="digest")
        got = client.get("/desktop/notices")
        assert got.status_code == 200
        items = got.json()["notices"]
        assert len(items) == 1
        ack = client.post("/desktop/notices/ack", json={"ids": [items[0]["id"]]})
        assert ack.status_code == 200
        assert ack.json()["acked"] == 1
        assert client.get("/desktop/notices").json()["notices"] == []
