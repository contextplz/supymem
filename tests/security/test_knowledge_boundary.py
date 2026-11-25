"""Assembled signed API must enforce venture access before reading context."""
from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import request, venture


def test_knowledge_and_record_detail_are_tenant_scoped(client):
    vid, _, _ = venture(client)
    path = f"/ventures/{vid}/knowledge"
    response = request(client, "GET", path, params={"q": "poor photos listings conversion", "kind": "historical_claim"})
    assert response.status_code == 200, response.text
    doc = response.json()
    assert doc["venture_id"] == vid and doc["items"][0]["company"] == "Airbnb"
    record_path = path + "/records/" + doc["items"][0]["id"]
    assert request(client, "GET", record_path).status_code == 200
    assert request(client, "GET", path, subject="usr_bob").status_code == 404
    assert request(client, "GET", record_path, subject="usr_bob").status_code == 404
    assert client.get(path).status_code in {401, 403}
    assert request(client, "GET", path, params={"limit": 500}).status_code == 422
    assert request(client, "GET", path + "/records/missing").status_code == 404


def test_reference_browsing_does_not_enqueue_or_charge(client):
    from sqlalchemy import func, select
    from app.db.models import Job
    from app.state import db
    vid, _, _ = venture(client)
    with db()() as session:
        before = session.scalar(select(func.count()).select_from(Job))
    result = request(client, "GET", f"/ventures/{vid}/knowledge")
    assert result.status_code == 200 and result.json()["inferred_from_idea"] is True
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == before


def test_proactive_coaching_is_scoped_fresh_and_read_only(client, monkeypatch):
    from sqlalchemy import func, select
    from app.db.models import Job
    from app.state import db
    from app.ventures import next_step
    vid, _, _ = venture(client)
    path = f"/ventures/{vid}/knowledge/coaching"
    monkeypatch.setattr(next_step, "founder_next_step", lambda _: {"key": "idea.clarify", "title": "Clarify your idea"})
    with db()() as session:
        before = session.scalar(select(func.count()).select_from(Job))
    first = request(client, "GET", path)
    assert first.status_code == 200 and first.json()["stage"] == "Problem discovery"
    assert "no-store" in first.headers["cache-control"]
    monkeypatch.setattr(next_step, "founder_next_step", lambda _: {"key": "site.publish", "title": "Publish your landing page"})
    assert request(client, "GET", path).json()["stage"] == "MVP and delivery"
    assert request(client, "GET", path, subject="usr_bob").status_code == 404
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == before


def test_relevance_review_does_not_bypass_context_filter(client):
    vid, _, _ = venture(client)
    result = request(client, "POST", f"/ventures/{vid}/knowledge/review", json={
        "query": "AI job application assistant for job seekers: tailor resumes and track applications"
    })
    assert result.status_code == 200, result.text
    items = result.json()["items"]
    assert all("developer tooling" not in row.get("fit_labels", []) for row in items)
    assert all("security" not in row.get("fit_labels", []) for row in items)


def test_coaching_uses_current_unconfirmed_revision_not_stale_idea(client, monkeypatch):
    import json
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession, IdeaRevision
    from app.state import db
    from app.ventures import next_step
    from core.clock import utcnow
    vid, wid, sid = venture(client)
    with db()() as session:
        session.get(Venture, vid).raw_idea = "B2B SaaS developer tools customer feedback manual beta workflow"
        idea = session.get(IdeaSession, sid)
        revision = IdeaRevision(id="rev_coaching", session_id=sid, workspace_id=wid, venture_id=vid,
            rev=2, source="founder", structured=json.dumps({"customer": "API platform teams"}), created_at=utcnow())
        session.add(revision)
        idea.current_revision_id = revision.id
        session.commit()
    monkeypatch.setattr(next_step, "founder_next_step", lambda _: {"key": "idea.clarify", "title": "Clarify this workflow"})
    path = f"/ventures/{vid}/knowledge/coaching"
    first = request(client, "GET", path)
    assert first.status_code == 200 and first.json()["items"]
    assert "API platform teams" in first.json()["items"][0]["do"]
    with db()() as session:
        session.get(IdeaRevision, "rev_coaching").structured = json.dumps({"customer": "integration maintainers"})
        session.commit()
    assert "integration maintainers" in request(client, "GET", path).json()["items"][0]["do"]
