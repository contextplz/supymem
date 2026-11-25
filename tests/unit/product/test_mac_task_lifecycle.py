"""Mac task lifecycle: lapsed reviews never block or get reused; related chat/status copy."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.unit.chat.test_site_actions import env  # noqa: F401 (fixture)
from tests.unit.product.test_device_orders import orders  # noqa: F401 (fixture)


def _age(svc, order_id, **changes):
    from app.db.models import VentureDocument
    with svc.db()() as session:
        row = session.get(VentureDocument, order_id)
        saved = json.loads(row.provenance_json)
        saved.update(changes)
        row.provenance_json = json.dumps(saved)
        session.commit()


def test_expired_or_cancelled_review_never_blocks_or_is_reused(orders):  # noqa: F811
    svc, caller, manifest = orders
    first = svc.prepare("v_one", caller, manifest)
    _age(svc, first["id"], expires_at="2000-01-01T00:00:00")
    second = svc.prepare("v_one", caller, manifest, recover_active=True)
    assert second["id"] != first["id"]
    args = ("v_one", caller, second["id"], second["review_digest"])
    assert svc.transition(*args, "cancel")["state"] == "cancelled"
    assert svc.prepare("v_one", caller, manifest, recover_active=True)["id"] not in {first["id"], second["id"]}


def test_claimed_then_cancelled_order_stops_blocking_after_its_run_limit(orders):  # noqa: F811
    svc, caller, manifest = orders
    order = svc.prepare("v_one", caller, manifest)
    args = ("v_one", caller, order["id"], order["review_digest"])
    svc.transition(*args, "approve")
    svc.transition(*args, "claim")
    svc.transition(*args, "cancel")
    with pytest.raises(svc.ActiveDeviceOrder):
        svc.prepare("v_one", caller, manifest)
    _age(svc, order["id"], claimed_at="2000-01-01T00:00:00+00:00")
    assert svc.prepare("v_one", caller, manifest)["id"] != order["id"]


def test_list_orders_marks_expired(orders):  # noqa: F811
    svc, caller, manifest = orders
    order = svc.prepare("v_one", caller, manifest)
    assert svc.list_orders("v_one", caller)[0]["state"] == "pending"
    _age(svc, order["id"], expires_at="2000-01-01T00:00:00")
    assert svc.list_orders("v_one", caller)[0]["state"] == "expired"


def test_run_status_ignores_an_expired_unclaimed_review():
    from app.ventures.run_status import _project_recent_mac_task
    body = {"state": "pending", "expires_at": "2000-01-01T00:00:00+00:00", "manifest": {}}
    order = SimpleNamespace(id="dwo_1", provenance_json=json.dumps(body), updated_at=datetime.now(timezone.utc))
    result = {"state": "idle", "activity": "x", "changed_at": None, "action": None}
    _project_recent_mac_task(result, "v_one", order)
    assert result["state"] == "idle"


def test_rename_strips_trailing_filler():
    from app.chat.intent import RENAME_RE
    for text in ("call yourself Charlie again", "Please call yourself Charlie now.", "call yourself Charlie instead"):
        assert RENAME_RE.match(text).group("name") == "Charlie"


@pytest.mark.parametrize("env", ["Warehouse dispatch planning", "Weekend garden planning"], indirect=True)
def test_page_stop_notice_keeps_the_known_heading_failure(env, monkeypatch):
    from app.chat.mac_stop import post_mac_stopped
    from app.chat.store import list_messages
    from app.build.device_orders import KIND
    from app.db.models import VentureDocument
    from app.state import db
    from core.clock import utcnow

    _, vid, _ = env
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with db()() as session:
        session.add(VentureDocument(id="failed_heading", venture_id=vid, kind=KIND, title="Your requested page", body_md="",
            provenance_json=json.dumps({"manifest": {"coding_loop_verification": {"provider": "codex"}},
                "task_snapshot": {"kind": "landing_page_build"}, "iterations": [{"summary": "The same checks kept failing",
                    "diagnosis": {"failures": ["sections.0.headline: String should have at most 140 characters"]}}]}),
            created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    assert post_mac_stopped(vid, "failed_heading", None, outcome_code="needs_founder")
    notice = next(item for item in list_messages(vid) if item.get("kind") == "mac_task_stopped")
    assert "heading" in notice["body_md"].lower() and "140" in notice["body_md"]
    assert "In your venture chat" in notice["body_md"]
    assert "ask Charlie to build a new version with the corrected heading" in notice["body_md"]
    assert "sections.0" not in notice["body_md"] and "could not confirm" not in notice["body_md"]


def test_revision_acceptance_posts_publish_next(env, monkeypatch):  # noqa: F811
    _, vid, _ = env
    from app.chat import site_actions
    monkeypatch.setattr(site_actions, "_waitlist_state", lambda v: {"ok": True, "enabled": True, "submission_verified": True})
    from app.chat.milestone_hooks import build_accepted
    from app.db.models import ChatMessage
    from app.state import db

    run = SimpleNamespace(id="run_rev2", venture_id=vid, app_id="app_1", spec="{}")
    with db()() as s:
        build_accepted(s, run, revision=True)
        s.commit()
    with db()() as s:
        row = s.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid, ChatMessage.kind == "build_accepted")).one()
    assert "publish the updated landing page" in row.body_md
