"""The assembled founder API is scoped, validated and recoverable without work."""
import json

import pytest

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import IDEA, request, venture


def _next_step(vid, wid):
    from app.tenancy import bind
    from app.ventures.next_step import founder_next_step
    with bind(workspace_id=wid, venture_id=vid, actor="usr_alice"):
        return founder_next_step(vid)


def test_review_api_enforces_scope_and_has_a_working_action_after_each_answer(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "new_user_flow", "on")
    vid, _, _ = venture(client)
    path = f"/ventures/{vid}/context"
    assert request(client, "GET", path, subject="usr_bob").status_code == 404
    response = request(client, "GET", path)
    assert response.status_code == 200, response.text
    context = response.json()
    assert "internal_classification" not in response.text and "checklist" not in response.text
    assert context["idea_href"] and context["attachments_href"]
    assert request(client, "POST", path + "/priorities", json={"revision": context["revision"], "text": "not sure", "categories": ["fundraising"]}).status_code == 422
    assert request(client, "POST", path + "/answer", json={"revision": context["revision"], "field": "making", "value": ""}).status_code == 422
    while context["question"]:
        question = context["question"]
        response = request(client, "POST", path + "/answer", json={"revision": context["revision"],
            "field": question["field"], "value": "Not launched; no users" if question["field"] == "progress" else "Order board for bakeries"})
        assert response.status_code == 200, response.text
        context = response.json()
    response = request(client, "POST", path + "/priorities", json={"revision": context["revision"], "text": "not sure"})
    assert response.status_code == 200, response.text
    done = response.json()
    assert done["complete"] and done["priorities"] == ["outreach_leads"] and done["next_href"].startswith("/gtm?")
    stale = request(client, "POST", path + "/back", json={"revision": context["revision"], "field": "priorities"})
    assert stale.status_code == 409
    back = request(client, "POST", path + "/back", json={"revision": done["revision"], "field": "priorities"})
    assert back.status_code == 200 and not back.json()["complete"]


def test_skip_api_is_scoped_current_question_only_and_reversible(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "new_user_flow", "on")
    vid, _, _ = venture(client)
    path = f"/ventures/{vid}/context"
    current = request(client, "GET", path).json()
    field = current["question"]["field"]
    body = {"revision": current["revision"], "field": field}
    assert request(client, "POST", path + "/skip", json=body, subject="usr_bob").status_code == 404
    assert request(client, "POST", path + "/skip", json={**body, "field": "unknown"}).status_code == 409
    assert request(client, "POST", path + "/skip", json={**body, "value": "fake evidence"}).status_code == 422
    response = request(client, "POST", path + "/skip", json=body)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert any(answer["field"] == field and answer.get("skipped") and answer["value"] is None for answer in saved["answers"])
    assert request(client, "POST", path + "/skip", json=body).status_code == 409
    back = request(client, "POST", path + "/back", json={"revision": saved["revision"], "field": field})
    assert back.status_code == 200 and back.json()["question"]["field"] == field


@pytest.mark.parametrize("mode,listed,expected", [
    ("off", "workspace", False), ("on", "", True),
    ("allowlist", "workspace", True), ("allowlist", "email", True),
    ("allowlist", "elsewhere", False),
])
def test_creation_pin_is_immutable_and_allowlist_matches_workspace_or_email(client, monkeypatch, mode, listed, expected):
    from app.config import settings
    from app.db.models import Person, Venture, VentureDocument
    from app.state import db
    from app.ventures import startup_review
    monkeypatch.setattr(settings, "new_user_flow", "off")
    vid, wid, _ = venture(client)
    with db()() as session:
        session.delete(session.get(VentureDocument, f"internal_new_user_flow:{vid}"))
        session.get(Person, "usr_alice").email = "founder@example.com"
        session.commit()
    monkeypatch.setattr(settings, "new_user_flow", mode)
    monkeypatch.setattr(settings, "new_user_flow_allowlist", {"workspace": f" other, {wid}, ", "email": " FOUNDER@EXAMPLE.COM ", "elsewhere": "other@example.com"}.get(listed, ""))
    with db()() as session:
        startup_review.pin_at_creation(session, session.get(Venture, vid))
        session.commit()
        assert startup_review.enabled(session, vid) is expected
    monkeypatch.setattr(settings, "new_user_flow", "off" if expected else "on")
    with db()() as session:
        startup_review.pin_at_creation(session, session.get(Venture, vid))
        session.commit()
        assert startup_review.enabled(session, vid) is expected
    assert request(client, "GET", f"/ventures/{vid}/context").status_code == (200 if expected else 404)


@pytest.mark.parametrize("legacy_venture", [False, True])
def test_off_path_confirmation_and_next_step_are_byte_for_byte_legacy_after_global_toggle(client, monkeypatch, legacy_venture):
    from core.clock import utcnow
    from app.config import Settings, settings
    from app.db.models import Job, VentureDocument
    from app.db.supymem_models import IdeaSession
    from app.idea import service
    from app.state import db
    from app.ventures.startup_review import PRIVATE_KINDS
    from sqlalchemy import select

    monkeypatch.delenv("NEW_USER_FLOW", raising=False)
    assert Settings(_env_file=None).new_user_flow == "on"
    monkeypatch.setattr(settings, "new_user_flow", "off")
    vid, wid, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    revision = request(client, "PATCH", base + "/revision", json={"patch": IDEA}).json()["id"]
    now = utcnow()
    monkeypatch.setattr(service, "utcnow", lambda: now)
    with db()() as session:
        idea = session.get(IdeaSession, sid)
        idea.state, idea.case = "reviewing", "know"
        if legacy_venture:
            session.delete(session.get(VentureDocument, f"internal_new_user_flow:{vid}"))
        session.commit()
    before_step = json.dumps(_next_step(vid, wid), sort_keys=True)
    first = request(client, "POST", base + "/confirm", json={"revision_id": revision})
    assert first.status_code == 200, first.text
    assert first.json()["dossier_job_id"] and "next_href" not in first.json()
    assert set(first.json()) == {"venture_id", "dossier_job_id", "session"}
    first_step = json.dumps(_next_step(vid, wid), sort_keys=True)
    monkeypatch.setattr(settings, "new_user_flow", "on")
    assert json.dumps(_next_step(vid, wid), sort_keys=True) == first_step
    with db()() as session:
        session.get(IdeaSession, sid).state = "reviewing"
        session.commit()
    second = request(client, "POST", base + "/confirm", json={"revision_id": revision})
    assert second.status_code == 200, second.text
    assert second.content == first.content
    assert json.dumps(_next_step(vid, wid), sort_keys=True) == first_step
    assert "venture.context" not in before_step + first_step
    assert "venture.review" not in before_step + first_step
    with db()() as session:
        jobs = list(session.scalars(select(Job).where(Job.venture_id == vid, Job.kind == "research.deep")))
        assert len(jobs) == 1 and jobs[0].id == first.json()["dossier_job_id"]
        private = list(session.scalars(select(VentureDocument).where(VentureDocument.venture_id == vid, VentureDocument.kind.in_(PRIVATE_KINDS))))
        assert [document.kind for document in private] == ([] if legacy_venture else ["internal_new_user_flow"])


def test_new_path_confirmation_stays_pinned_when_switch_is_turned_off_and_stage_is_private(client, monkeypatch):
    from dataclasses import asdict
    from sqlalchemy import select
    from app.config import settings
    from app.db.models import AnalyticsEvent, Job, VentureDocument
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.chat.context import assemble
    from app.state import db
    from app.ventures import startup_review

    monkeypatch.setattr(settings, "new_user_flow", "on")
    vid, wid, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    revision = request(client, "PATCH", base + "/revision", json={"patch": IDEA}).json()["id"]
    with db()() as session:
        idea = session.get(IdeaSession, sid)
        idea.state, idea.case = "reviewing", "know"
        saved = session.get(IdeaRevision, revision)
        saved.structured = json.dumps({**json.loads(saved.structured), "progress": {"mrr": 20000}})
        for job in session.scalars(select(Job).where(Job.venture_id == vid)):
            job.status = "complete"
        session.commit()
    monkeypatch.setattr(settings, "new_user_flow", "off")
    confirmed = request(client, "POST", base + "/confirm", json={"revision_id": revision})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["dossier_job_id"] is None
    assert confirmed.json()["next_href"] == f"/v/{vid}/context"
    assert _next_step(vid, wid)["key"] == "venture.context"
    with db()() as session:
        internal = json.loads(session.get(VentureDocument, f"{startup_review.INTERNAL_KIND}:{vid}").body_md)
        assert internal["internal_classification"] == "Advanced"
        assert not list(session.scalars(select(Job).where(Job.venture_id == vid, Job.kind == "research.deep")))
        analytics = [{"label": event.event_name, "properties": json.loads(event.props_json)}
                     for event in session.scalars(select(AnalyticsEvent).where(AnalyticsEvent.venture_id == vid))]
        assert analytics and "idea.confirmed" in [event["label"] for event in analytics]
    payloads = [confirmed.json(), asdict(assemble(vid)), _next_step(vid, wid), analytics]
    for path in (f"/ventures/{vid}", f"/ventures/{vid}/dashboard", f"/ventures/{vid}/context"):
        response = request(client, "GET", path)
        assert response.status_code == 200, response.text
        payloads.append(response.json())
    public = json.dumps(payloads, default=str)
    assert "internal_classification" not in public and "Advanced" not in public
    assert not any(kind in public for kind in startup_review.PRIVATE_KINDS)


def test_allowlisted_founder_email_applies_on_real_api_and_mac_entry_creation(client, monkeypatch):
    from app.config import settings
    from app.db.models import Person
    from app.state import db
    monkeypatch.setattr(settings, "new_user_flow", "off")
    old_vid, _, _ = venture(client)
    with db()() as session:
        session.get(Person, "usr_alice").email = "founder@example.com"
        session.commit()
    monkeypatch.setattr(settings, "new_user_flow", "allowlist")
    monkeypatch.setattr(settings, "new_user_flow_allowlist", " FOUNDER@EXAMPLE.COM ")
    new_vid, _, _ = venture(client)
    assert request(client, "GET", f"/ventures/{old_vid}/context").status_code == 404
    assert request(client, "GET", f"/ventures/{new_vid}/context").status_code == 200
    created = request(client, "POST", "/entry", json={"explicit_mode": "idea", "text": IDEA["problem"], "name": "Entry board", "start": False})
    assert created.status_code == 200, created.text
    assert request(client, "GET", f"/ventures/{created.json()['venture_id']}/context").status_code == 200


def test_server_switch_reads_env_and_rejects_unknown_modes(client, monkeypatch):
    from app.config import Settings
    from pydantic import ValidationError
    monkeypatch.setenv("NEW_USER_FLOW", "allowlist")
    monkeypatch.setenv("NEW_USER_FLOW_ALLOWLIST", "workspace, founder@example.com")
    configured = Settings(_env_file=None)
    assert configured.new_user_flow == "allowlist"
    assert configured.new_user_flow_allowlist == "workspace, founder@example.com"
    monkeypatch.setenv("NEW_USER_FLOW", "typo")
    with pytest.raises(ValidationError, match="off, allowlist, or on"):
        Settings(_env_file=None)
