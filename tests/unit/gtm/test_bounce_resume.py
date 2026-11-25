"""Bounce recovery is founder-triggered, scoped, and starts a fresh window."""
import asyncio
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.unit.gtm.test_contact_work import _sent
from tests.unit.gtm.test_journey import journey_db


def _paused(data, monkeypatch):
    from app.db.models import GtmTouch
    from app.gtm.relationships.service import _settings
    from app.gtm.supymem.pipeline import apply_bounce
    from core.clock import utcnow
    cid = _sent(data, monkeypatch)
    with data["factory"]() as session:
        for index in range(9):
            session.add(GtmTouch(id=f"old_{index}", workspace_id=data["wid"], contact_id=cid, direction="out",
                channel="email", simulated=0, sent_at=utcnow(), created_at=utcnow()))
        _settings(session, data["wid"])
        session.commit()
    apply_bounce(cid)
    return cid


def _client(data, monkeypatch):
    from app.api import gtm_god_ops
    monkeypatch.setattr(gtm_god_ops, "_auth_workspace", lambda *args: data["wid"])
    app = FastAPI()
    app.include_router(gtm_god_ops.router)
    return TestClient(app)


def test_pause_resume_new_window_and_duplicate_old_bounce(journey_db, monkeypatch):
    from app.db.models import GtmContact, GtmEngineState, GtmOutreachSettings, GtmTouch
    from app.gtm.relationships.abuse import bounce_counts
    from app.gtm.supymem.pipeline import apply_bounce
    from app.needs_you import service
    from core.clock import utcnow
    data = journey_db
    cid = _paused(data, monkeypatch)
    assert service.list_open(data["vid"])
    with data["factory"]() as session:
        old_bounce = session.get(GtmContact, cid).bounced_at
        assert bounce_counts(session, data["wid"])["sent"] == 10
    client = _client(data, monkeypatch)
    path = f"/api/v1/ventures/{data['vid']}/gtm/journey/resume-sending"
    result = client.post(path, json={"reviewed_bounced_addresses": True})
    assert result.status_code == 200, result.text
    assert result.json()["sending_pause"] is None
    assert "Nothing was sent" in result.json()["receipt"]["body_md"]
    assert not service.list_open(data["vid"])
    with data["factory"]() as session:
        settings = session.get(GtmOutreachSettings, data["wid"])
        resumed = json.loads(settings.onboarding_status)["send_resumed_at"]
        assert settings.send_enabled and session.get(GtmEngineState, data["wid"]).send_enabled
        assert bounce_counts(session, data["wid"]) == {"sent": 0, "bounced": 0, "send_resumed_at": resumed}
        new = GtmContact(id="new_bounce", workspace_id=data["wid"], email="new@example.test", first_name="New",
            times_emailed=1, stage_changed_at=utcnow(), created_at=utcnow())
        session.add(new)
        for index in range(9):
            session.add(GtmTouch(id=f"new_{index}", workspace_id=data["wid"], contact_id=new.id, direction="out",
                channel="email", simulated=0, sent_at=utcnow(), created_at=utcnow()))
        session.commit()
    apply_bounce(cid)
    apply_bounce("new_bounce")
    with data["factory"]() as session:
        assert session.get(GtmContact, cid).bounced_at == old_bounce
        assert session.get(GtmOutreachSettings, data["wid"]).send_enabled
        assert bounce_counts(session, data["wid"])["sent"] == 9
        session.add(GtmTouch(id="new_10", workspace_id=data["wid"], contact_id="new_bounce", direction="out",
            channel="email", simulated=0, sent_at=utcnow(), created_at=utcnow()))
        session.commit()
    apply_bounce("new_bounce")
    with data["factory"]() as session:
        assert not session.get(GtmOutreachSettings, data["wid"]).send_enabled
        assert bounce_counts(session, data["wid"])["sent"] == 10
        assert bounce_counts(session, data["wid"])["bounced"] == 1


def test_resume_requires_review_venture_scope_and_owner_role(journey_db, monkeypatch):
    from app.db.models import GtmOutreachSettings, WorkspaceMember
    from app.gtm import journey
    from app.gtm.relationships.abuse import pause_sends
    data = journey_db
    pause_sends(data["wid"])
    client = _client(data, monkeypatch)
    path = f"/api/v1/ventures/{data['vid']}/gtm/journey/resume-sending"
    assert client.post(path, json={}).status_code == 422
    assert client.post(path, json={"reviewed_bounced_addresses": False}).status_code == 422
    assert client.post("/api/v1/ventures/foreign/gtm/journey/resume-sending", json={"reviewed_bounced_addresses": True}).status_code == 404
    with pytest.raises(ValueError, match="gtm_scope_mismatch"):
        journey.resume_sending("foreign", data["vid"], reviewed_bounced_addresses=True, actor=data["actor"])
    with data["factory"]() as session:
        member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == data["wid"], WorkspaceMember.person_id == data["actor"]))
        member.role = "maintainer"
        session.commit()
    assert client.post(path, json={"reviewed_bounced_addresses": True}).status_code == 403
    with data["factory"]() as session:
        assert not session.get(GtmOutreachSettings, data["wid"]).send_enabled


def test_hosted_resume_keeps_dpa_gate_and_retry_action(journey_db, monkeypatch):
    from app.db.models import GtmOutreachSettings, Workspace
    from app.gtm.relationships.abuse import pause_sends
    from app.gtm.relationships.hosted import accept_dpa
    data = journey_db
    with data["factory"]() as session:
        session.get(Workspace, data["wid"]).tenancy = "managed"
        session.commit()
    pause_sends(data["wid"])
    client = _client(data, monkeypatch)
    path = f"/api/v1/ventures/{data['vid']}/gtm/journey/resume-sending"
    response = client.post(path, json={"reviewed_bounced_addresses": True})
    assert response.status_code == 403 and response.json()["detail"]["code"] == "dpa_required"
    with data["factory"]() as session:
        assert not session.get(GtmOutreachSettings, data["wid"]).send_enabled
    accept_dpa(data["wid"])
    assert client.post(path, json={"reviewed_bounced_addresses": True}).status_code == 200


def test_resume_idempotent_does_not_reset_live_window(journey_db, monkeypatch):
    from app.db.models import GtmOutreachSettings
    from app.gtm import journey
    from app.gtm.relationships.abuse import pause_sends
    data = journey_db
    pause_sends(data["wid"])
    journey.resume_sending(data["wid"], data["vid"], reviewed_bounced_addresses=True)
    with data["factory"]() as session:
        resumed = json.loads(session.get(GtmOutreachSettings, data["wid"]).onboarding_status)["send_resumed_at"]
    journey.resume_sending(data["wid"], data["vid"], reviewed_bounced_addresses=True)
    with data["factory"]() as session:
        assert json.loads(session.get(GtmOutreachSettings, data["wid"]).onboarding_status)["send_resumed_at"] == resumed


def test_bounce_request_card_and_chat_route_to_recovery(journey_db, monkeypatch):
    from tests.unit.gtm.test_journey import _mail
    from app.chat import jarvis, milestones
    from app.chat.execute import execute_action
    from app.chat.intent import classify_intent
    from app.gtm.relationships.abuse import pause_sends
    from app.needs_you import service
    data = journey_db
    _mail(data)
    pause_sends(data["wid"])
    cards = []
    monkeypatch.setattr(milestones, "post_milestone", lambda *args, **kw: cards.append(kw))
    rid = service.request_human(data["vid"], reason_code="gtm_bounce_pause", title="Paused",
        what_we_need="1 contact bounced across 10 confirmed sends. Check the bounced addresses first.",
        how_to_resume="1. Check the bounced addresses first. 2. Click Resume sending.", blocking_ref="gtm-bounce-pause")
    assert rid
    button = next(action for action in cards[0]["actions"] if action["label"] == "Resume sending")
    assert button["action"] == "gtm.resume_sending"
    assert "Check the bounced addresses first" in cards[0]["body_md"]
    assert classify_intent("resume sending") == "jarvis.resume_sending"
    reply = asyncio.run(jarvis.dispatch(data["vid"], "jarvis.resume_sending", "resume sending", None))
    assert "confirm Resume sending below" in reply["body_md"]
    assert reply["actions"][0]["confirmation"]["action"] == "gtm.resume_sending"
    result = asyncio.run(execute_action(data["vid"], button["action"], payload=button["payload"], actor_subject=data["actor"]))
    assert "Nothing was sent" in result["body_md"]


def test_resume_router_obeys_layering():
    from tests.unit.test_layering import API_DIR, FORBIDDEN_MODULES, _imported_modules
    path = API_DIR / "gtm_god_ops.py"
    assert not _imported_modules(path) & FORBIDDEN_MODULES
    source = path.read_text()
    assert "session.commit()" not in source and "with db(" not in source


def test_stale_bounce_pause_rechecks_window_and_preserves_an_existing_pause(journey_db, monkeypatch):
    from app.gtm import journey
    from app.gtm.relationships.abuse import pause_sends
    data = journey_db
    _paused(data, monkeypatch)
    journey.resume_sending(data["wid"], data["vid"], reviewed_bounced_addresses=True)
    assert pause_sends(data["wid"], reason="bounce_rate") == {"send_enabled": True, "reason": None}
    pause_sends(data["wid"])
    assert pause_sends(data["wid"], reason="bounce_rate") == {"send_enabled": False, "reason": None}
