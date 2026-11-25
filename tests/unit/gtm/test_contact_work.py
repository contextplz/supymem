# ruff: noqa: F811
"""Reply logging, booking link, suggested reply, call brief and call learnings (fake LLM; real durable state)."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.unit.gtm.test_journey import (  # noqa: F401  (fixture re-export)
    _add,
    journey_db,
)


def _sent(d, monkeypatch, *, thread="thr_1"):
    """A prospect that already received a real email, so a reply can be attributed to its thread."""
    from app.db.models import GtmContact, GtmTouch
    from core.clock import utcnow
    view = _add(d, monkeypatch)
    cid = view["prospects"][0]["id"]
    with d["factory"]() as s:
        s.get(GtmContact, cid).times_emailed = 1
        s.add(GtmTouch(id="tch_out", workspace_id=d["wid"], venture_id=d["vid"], contact_id=cid, direction="out", channel="email", provider_thread_id=thread,
                       subject="A question for Ada Bakery", body_text="hello", sent_at=utcnow(), simulated=0, created_at=utcnow()))
        s.commit()
    return cid


def _fake_llm(monkeypatch, reply):
    from app.gtm.god import llm
    calls = []
    monkeypatch.setattr(llm, "json_chat", lambda **kw: calls.append(kw) or reply)
    return calls


def test_logged_reply_uses_poller_path_and_opt_out_suppresses(journey_db, monkeypatch):
    from app.db.models import GtmContact, GtmLearningEvent, GtmSuppression
    from app.gtm import journey
    d = journey_db
    cid = _sent(d, monkeypatch)
    view = journey.contact_action(d["wid"], d["vid"], cid, "reply", {"text": "Sounds interesting, tell me more", "outcome": "interested"})
    assert (view["results"]["replied"], view["results"]["interested"]) == (1, 1)
    assert view["results"]["replies"][0]["manual"] is True and view["results"]["replies"][0]["contact_id"] == cid
    with d["factory"]() as s:
        assert s.get(GtmContact, cid).replied_at is not None
        assert "reply_positive" in {e.kind for e in s.scalars(select(GtmLearningEvent))}
        assert not s.scalar(select(GtmSuppression.id))
    view = journey.contact_action(d["wid"], d["vid"], cid, "reply", {"text": "Please stop emailing", "outcome": "opted_out"})
    assert view["results"]["negative"] == 1
    with d["factory"]() as s:
        assert s.get(GtmContact, cid).do_not_contact == 1
        assert s.scalar(select(GtmSuppression.reason)) == "replied_opt_out"
    journey.update_settings(d["wid"], d["vid"], {"booking_link": "https://cal.com/ada"})
    with pytest.raises(ValueError, match="gtm_prospect_blocked"):
        journey.contact_action(d["wid"], d["vid"], cid, "reply-draft", {})


def test_opt_out_wording_suppresses_even_when_marked_not_now(journey_db, monkeypatch):
    from app.db.models import GtmSuppression
    from app.gtm import journey
    d = journey_db
    cid = _sent(d, monkeypatch)
    journey.contact_action(d["wid"], d["vid"], cid, "reply", {"text": "unsubscribe me", "outcome": "not_now"})
    with d["factory"]() as s:
        assert s.scalar(select(GtmSuppression.id))


def test_reply_needs_a_sent_message_and_valid_outcome(journey_db, monkeypatch):
    from app.gtm import journey
    d = journey_db
    cid = _add(d, monkeypatch)["prospects"][0]["id"]
    with pytest.raises(ValueError, match="gtm_reply_needs_sent_message"):
        journey.contact_action(d["wid"], d["vid"], cid, "reply", {"text": "hi", "outcome": "interested"})
    with pytest.raises(ValueError, match="gtm_reply_invalid"):
        journey.contact_action(d["wid"], d["vid"], cid, "reply", {"text": "hi", "outcome": "bogus"})


def test_u46_manual_outcomes_are_durable_scoped_and_not_delivery(journey_db, monkeypatch):
    from app.db.models import GtmTouch
    from app.gtm import journey
    d = journey_db
    _add(d, monkeypatch)
    with d["factory"]() as session:
        row = journey._row(session, d["vid"], create=True)
        data = journey._json(row.provenance_json)
        data["channels"] = [{"name": "Puppy community", "url": "https://www.reddit.com/r/puppy101/", "draft": "Hi, open to a call?", "why_fits": "Puppy owners gather here."}]
        journey._store(session, row, data)
        session.commit()
    import hashlib
    cid = hashlib.sha256(journey.status(d["wid"], d["vid"])["channels"][0]["url"].encode()).hexdigest()[:24]
    from tests.unit.gtm.test_bounce_resume import _client
    client = _client(d, monkeypatch)
    response = client.post(f"/api/v1/ventures/{d['vid']}/gtm/journey/prospects/{cid}/sent", json={})
    assert response.status_code == 200
    first = response.json()
    assert first["phase"] == "results" and first["manual_outreach"][0]["sent_at"]
    booked = journey.contact_action(d["wid"], d["vid"], cid, "call-booked", {})
    repeat = journey.contact_action(d["wid"], d["vid"], cid, "sent", {})
    assert repeat["manual_outreach"] == booked["manual_outreach"]
    assert len(booked["manual_outreach"]) == 1 and booked["manual_outreach"][0]["booked_at"]
    assert booked["results"]["sent"] == 1
    from app.ventures.next_step import _facts, founder_next_step
    assert _facts(d["vid"])["first_call_booked"]
    assert founder_next_step(d["vid"])["key"] == "gtm.results"
    with d["factory"]() as session:
        assert not session.scalar(select(GtmTouch.id))
    with pytest.raises(ValueError, match="gtm_not_found"):
        journey.contact_action(d["wid"], d["vid"], "another-venture-lead", "sent", {})
    with d["factory"]() as session:
        row = journey._row(session, d["vid"])
        data = journey._json(row.provenance_json)
        data["need_signals"] = [{"source_url": "https://forum.example/old", "quote": "I need help training my puppy", "recency": "older"}]
        journey._store(session, row, data)
        session.commit()
    with pytest.raises(ValueError, match="gtm_not_found"):
        journey.contact_action(d["wid"], d["vid"], hashlib.sha256(b"https://forum.example/old").hexdigest()[:24], "sent", {})


@pytest.mark.parametrize("url", ["http://cal.com/ada", "https://localhost/x", "https://127.0.0.1/x", "https://u:p@calendar.example/x", "javascript:alert(1)", "https://nodot/x", "https://cal.com/a b"])
def test_booking_link_rejects_unsafe_urls(journey_db, url):
    from app.core.local_mode import is_local
    from app.gtm import journey
    d = journey_db
    with pytest.raises(ValueError, match="valid HTTPS booking link" if is_local() else "gtm_booking_link_invalid"):
        journey.update_settings(d["wid"], d["vid"], {"booking_link": url})


def test_booking_link_saved_and_cleared(journey_db):
    from app.gtm import journey
    d = journey_db
    assert journey.update_settings(d["wid"], d["vid"], {"booking_link": " https://cal.com/ada/15min "})["booking_link"] == "https://cal.com/ada/15min"
    assert journey.update_settings(d["wid"], d["vid"], {"booking_link": ""})["booking_link"] is None


def test_suggested_reply_includes_link_and_passes_existing_review_checks(journey_db, monkeypatch):
    from app.core.local_mode import is_local
    from app.gtm import journey
    d = journey_db
    cid = _sent(d, monkeypatch)
    with pytest.raises(ValueError, match="gtm_reply_not_interested"):
        journey.contact_action(d["wid"], d["vid"], cid, "reply-draft", {})
    journey.contact_action(d["wid"], d["vid"], cid, "reply", {"text": "Yes, interested in a demo", "outcome": "interested"})
    if is_local():
        view = journey.contact_action(d["wid"], d["vid"], cid, "reply-draft", {})
        draft = next(row for row in view["drafts"] if row.get("kind") == "reply")
        assert draft["review_state"] == "draft" and view["booking_link"] is None
        assert "You can pick a time here:" not in draft["body"] and "unsubscribe" in draft["body"].lower()
        journey.patch_draft(d["wid"], d["vid"], draft["doc_id"], {"decision": "skipped", "expected_revision": draft["revision"]})
    else:
        with pytest.raises(ValueError, match="gtm_booking_link_required"):
            journey.contact_action(d["wid"], d["vid"], cid, "reply-draft", {})
    journey.update_settings(d["wid"], d["vid"], {"booking_link": "https://cal.com/ada/15min"})
    calls = _fake_llm(monkeypatch, {"reply": "Thanks for the reply. Happy to talk through how you take orders today. See https://evil.example for more."})
    view = journey.contact_action(d["wid"], d["vid"], cid, "reply-draft", {})
    assert calls[0]["purpose"] == "gtm.reply_draft" and calls[0]["workspace_id"] == d["wid"]
    draft = next(x for x in view["drafts"] if x.get("kind") == "reply" and x["review_state"] != "skipped")
    assert "https://cal.com/ada/15min" in draft["body"] and "evil.example" not in draft["body"] and draft["review_state"] == "draft"
    assert draft["subject"].startswith("Re: ") and draft["day"] == 7
    again = journey.contact_action(d["wid"], d["vid"], cid, "reply-draft", {})
    assert len([x for x in again["drafts"] if x.get("kind") == "reply" and x["review_state"] != "skipped"]) == 1
    approved = journey.patch_draft(d["wid"], d["vid"], draft["doc_id"], {"decision": "approved", "expected_revision": draft["revision"]})
    assert next(x for x in approved["drafts"] if x["doc_id"] == draft["doc_id"])["review_state"] == "approved"
    from app.db.models import GtmContact
    with d["factory"]() as session:  # the reply may be sent to someone who replied; ordinary outreach still may not
        contact = session.get(GtmContact, cid)
        assert journey._block(session, d["wid"], contact) == "suppressed_or_replied" and journey._block(session, d["wid"], contact, reply=True) is None
    assert next(b for b in approved["batches"] if b["batch_id"] == draft["batch_id"])["draft_ids"] == [draft["doc_id"]]


def test_country_confirmation_and_skip_preserve_reply_work(journey_db, monkeypatch):
    from app.gtm import journey
    d = journey_db
    cid = _sent(d, monkeypatch)
    journey.contact_action(d["wid"], d["vid"], cid, "reply", {"text": "Yes, interested in a demo", "outcome": "interested"})
    journey.update_settings(d["wid"], d["vid"], {"booking_link": "https://cal.com/ada"})
    journey.patch_prospect(d["wid"], d["vid"], cid, {"skip": True})
    with pytest.raises(ValueError, match="gtm_prospect_blocked"):
        journey.contact_action(d["wid"], d["vid"], cid, "reply-draft", {})
    journey.confirm_prospects(d["wid"], d["vid"], [{"contact_id": cid, "skip": False, "country": "CA", "lawful_basis": "consent", "lawful_basis_note": "Agreed to discuss the bakery order board."}])
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fixture")
    _fake_llm(monkeypatch, {"reply": "Thanks for getting back to me. Could we find a time to talk?"})
    view = journey.contact_action(d["wid"], d["vid"], cid, "reply-draft", {})
    assert view["geo_options"] and view["prospects"][0]["country"] == "CA" and not view["prospects"][0]["skipped"]
    assert "credit" not in view["estimate"]["basis"].lower()
    assert "Nothing is posted or sent." in view["estimate"]["basis"]
    assert view["booking_link"] == "https://cal.com/ada" and view["results"]["interested"] == 1
    assert "contact_work" in view and "learned" in view and any(draft.get("kind") == "reply" for draft in view["drafts"])


@pytest.mark.parametrize("model", [None, {"reply": "We guarantee 10x results for every bakery. Trusted by 500 customers."}, {"reply": "Why? When? How?"}])
def test_suggested_reply_falls_back_to_plain_template(journey_db, monkeypatch, model):
    from app.gtm import journey
    d = journey_db
    cid = _sent(d, monkeypatch)
    journey.contact_action(d["wid"], d["vid"], cid, "reply", {"text": "Yes, interested in a demo", "outcome": "interested"})
    journey.update_settings(d["wid"], d["vid"], {"booking_link": "https://calendly.com/ada"})
    _fake_llm(monkeypatch, model)
    view = journey.contact_action(d["wid"], d["vid"], cid, "reply-draft", {})
    draft = next(x for x in view["drafts"] if x.get("kind") == "reply")
    assert draft["source"] == "template" and "10x" not in draft["body"] and "calendly.com/ada" in draft["body"]


def test_call_brief_from_stored_evidence(journey_db, monkeypatch):
    from app.gtm import journey
    d = journey_db
    cid = _sent(d, monkeypatch)
    with pytest.raises(ValueError, match="gtm_reply_not_interested"):
        journey.contact_action(d["wid"], d["vid"], cid, "brief", {})
    journey.contact_action(d["wid"], d["vid"], cid, "reply", {"text": "Yes, interested in a demo", "outcome": "interested"})
    calls = _fake_llm(monkeypatch, {"who": "Ada runs a bakery.", "pains": ["Orders may be scattered across chats"], "questions": [f"Question {i}?" for i in range(1, 8)]})
    brief = journey.contact_action(d["wid"], d["vid"], cid, "brief", {})["contact_work"][cid]["brief"]
    assert calls[0]["purpose"] == "gtm.call_brief"
    assert "Yes, interested in a demo" in calls[0]["user"] and "https://bakery.example/contact" in calls[0]["user"] and "Order board" in calls[0]["user"]
    assert "Ada runs a bakery." in brief and "5. Question 5?" in brief and "Question 6" not in brief
    _fake_llm(monkeypatch, None)
    with pytest.raises(ValueError, match="gtm_llm_unavailable"):
        journey.contact_action(d["wid"], d["vid"], cid, "brief", {})


def test_call_notes_extract_learnings_and_drive_results(journey_db, monkeypatch):
    from app.db.models import GtmLearningEvent
    from app.db.supymem_models import VentureWaitlist, VentureWaitlistEntry
    from app.gtm import journey
    from core.clock import utcnow
    d = journey_db
    cid = _sent(d, monkeypatch)
    view = journey.status(d["wid"], d["vid"])
    assert view["learned"]["calls"] == 0 and view["next_step"] == {"kind": "talk", "text": "Talk to 5 more people before choosing what to build next."}
    journey.contact_action(d["wid"], d["vid"], cid, "reply", {"text": "Yes, interested in a demo", "outcome": "interested"})
    _fake_llm(monkeypatch, None)
    with pytest.raises(ValueError, match="gtm_llm_unavailable"):
        journey.contact_action(d["wid"], d["vid"], cid, "call-notes", {"text": "She tracks orders in WhatsApp and would pay 20 a month."})
    assert journey.status(d["wid"], d["vid"])["contact_work"][cid]["call_notes"].startswith("She tracks")  # notes survive a model failure
    calls = _fake_llm(monkeypatch, {"pains_confirmed": ["Orders get lost in WhatsApp"], "pains_refuted": ["Pricing is not a worry"], "objections": ["No time to switch"], "willingness_to_pay": "YES"})
    view = journey.contact_action(d["wid"], d["vid"], cid, "call-notes", {"text": "She tracks orders in WhatsApp and would pay 20 a month."})
    assert calls[0]["purpose"] == "gtm.call_learnings"
    assert view["learned"] == {"calls": 1, "pains_confirmed": ["Orders get lost in WhatsApp"], "pains_refuted": ["Pricing is not a worry"], "objections": ["No time to switch"], "would_pay": 1}
    assert view["next_step"]["kind"] == "experiment"
    with d["factory"]() as s:
        assert "call_learning" in {e.kind for e in s.scalars(select(GtmLearningEvent))}
        s.add(VentureWaitlist(venture_id=d["vid"], workspace_id=d["wid"], enabled=True, consent_version="v", enabled_by="usr", updated_at=utcnow()))
        s.flush()
        for i, test in enumerate([False, False, True]):
            s.add(VentureWaitlistEntry(id=f"wle_{i}", venture_id=d["vid"], workspace_id=d["wid"], email=f"p{i}@x.example", consent_version="v", consent_text="ok", is_test=test, created_at=utcnow()))
        s.commit()
    assert journey.status(d["wid"], d["vid"])["waitlist_signups"] == 2


@pytest.mark.parametrize("audience", ["Freight dispatch coordinators", "Parents choosing swimming lessons"])
@pytest.mark.parametrize("configured", [True, False])
def test_ask_sending_creates_one_needs_you_request(journey_db, monkeypatch, audience, configured):
    from app.core.local_mode import is_local
    from app.gtm import journey
    from app.needs_you.service import list_open
    d = journey_db
    journey.update_icp(d["wid"], d["vid"], audience)
    if is_local():
        from app.core.local_capabilities import enabled
        from app.core.local_secrets import read_settings, update_settings
        from tests.unit.gtm.test_journey import _mail
        if configured:
            _mail(d)
        update_settings("capabilities", {"send_email": False}, {})
    settings = journey.guard.get_settings(d["vid"])
    view = journey.ask_sending(d["wid"], d["vid"])
    journey.ask_sending(d["wid"], d["vid"])
    assert view["receipt"]["kind"] == ("text" if is_local() else "needs_you")
    assert [r["reason_code"] for r in list_open(d["vid"])] == ([] if is_local() else ["gtm_sending_off"])
    assert journey.guard.get_settings(d["vid"]) == settings
    if is_local():
        assert enabled("send_email") is configured
        next_step = "Nothing was sent" if configured else "Settings > Keys and connections > Your mail account"
        assert next_step in view["receipt"]["body_md"]
        update_settings("capabilities", {"send_email": False}, {})
        monkeypatch.setattr(journey, "authority", lambda *args, **kwargs: "another-authorized-writer")
        with pytest.raises(ValueError, match="Open your desktop app"):
            journey.ask_sending(d["wid"], d["vid"])
        assert read_settings("capabilities")["values"]["send_email"] is False
