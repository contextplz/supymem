"""Saved outreach drafts from before the current email format: shown cleaned, and rebuilt from chat."""
from __future__ import annotations

import asyncio

import pytest

from tests.company_os_harness import local_chat as local_chat

from tests.unit.gtm.test_journey import _mail, journey_db as journey_db  # noqa: F401 - fixture

CASES = [
    ("Order board", "Ada Bakery", "bakery.example", ["ada", "ben"]),
    ("Chair queue", "Smile Dental", "smiledental.example", ["sam", "lia"]),
]


@pytest.fixture(params=CASES, ids=["bakery", "dental"])
def legacy(journey_db, monkeypatch, request):
    from app.autopilot import outreach
    from app.db.models import Venture
    from app.gtm import journey

    venture, company, host, people = request.param
    d = journey_db
    with d["factory"]() as session:
        session.get(Venture, d["vid"]).name = venture
        session.commit()
    monkeypatch.setattr(journey.outreach, "site_url", lambda vid: f"https://{host}/app")
    for person in people:
        journey.add_prospect(d["wid"], d["vid"], {"name": person.title(), "company": company, "email": f"{person}@{host}",
            "source_url": f"https://{host}/contact", "country": "CA", "lawful_basis": "consent",
            "lawful_basis_note": "The contact explicitly agreed to hear about this product."})
    journey._draft_ready(d["wid"], d["vid"])
    _mail(d)
    for draft in outreach.drafts(d["vid"], "draft"):  # the format saved before the human-sounding body shipped
        old = (f"Hi {draft['to'].split('@')[0].title()},\n\nI'm building {venture}. I found {company}. How do you handle this today?\n\n"
               f"Product: https://{host}/app\n\nSource: https://{host}/contact\n\nthe {venture} team\n\n{outreach.OPT_OUT}\n"
               "Business outreach · 1 Founder Street, New York, NY 10001")
        outreach._save_draft(d["vid"], draft | {"body": old}, draft["doc_id"])
    return d | {"venture": venture, "company": company, "host": host}


def test_saved_legacy_drafts_display_in_the_current_format(legacy):
    from app.chat import customers

    d = legacy
    pending = customers._pending(customers._view(d["vid"]))
    assert len(pending) == 2
    shown = customers.render(d["vid"], customers._view(d["vid"]))["body_md"]
    for text in [shown, *(p["body"] for p in pending)]:
        assert f"I found {d['company']}" not in text
        assert "Product:" not in text and "Source:" not in text and f"the {d['venture']} team" not in text
        assert f"\nThe {d['venture']} team\n" in text and f"You can see what I'm building here: https://{d['host']}/app" in text


def test_rewrite_both_drafts_from_scratch_rebuilds_them_with_the_current_writer(legacy, monkeypatch):
    from app.autopilot import outreach
    from app.chat import customers
    from app.gtm.relationships.email_compliance import OPT_OUT
    from app.gtm.relationships.email_compliance import sender_address

    d = legacy
    sent = []
    monkeypatch.setattr(outreach, "_send_one", lambda *a: sent.append(a) or {})
    old = {p["doc_id"] for p in customers._pending(customers._view(d["vid"]))}
    name, match = customers.command(d["vid"], "rewrite both drafts from scratch")
    assert name == "rewrite"
    reply = asyncio.run(customers.run_command(d["vid"], name, match, "", d["actor"]))
    assert "Rewrote 2 draft(s)" in reply["body_md"] and "Nothing was sent" in reply["body_md"] and not sent
    pending = customers._pending(customers._view(d["vid"]))
    assert len(pending) == 2 and not old & {p["doc_id"] for p in pending}
    with d["factory"]() as session:
        postal_address = sender_address(session, d["wid"])
    assert postal_address
    for draft in pending:
        body = draft["body"]
        assert f"I found {d['company']}" not in body and "Product:" not in body and "Source:" not in body
        assert f"Your email address is listed publicly on {d['host']}." in body and OPT_OUT in body
        assert postal_address in body and outreach.founder_signature(d["vid"]) in body and draft["review_state"] == "draft"
        assert f"Your email address is listed publicly on {d['host']}." in reply["body_md"]
    retired = [x for x in outreach.drafts(d["vid"]) if x["doc_id"] in old]
    assert {x["state"] for x in retired} == {"review_abandoned"}
    assert reply["actions"][0]["action"] == "gtm.approve"


@pytest.mark.parametrize("legacy", [*CASES,
    ("Trail diary", "Weekend Hikers", "hikers.example", ["dev", "mai"])],
    indirect=True, ids=["bakery", "dental", "consumer-hiking"])
def test_rewrite_keeps_approved_drafts_and_targets_one_by_number(legacy):
    from app.chat import customers

    d = legacy
    asyncio.run(customers.execute(d["vid"], "gtm.status", {"show_saved": True}, d["actor"]))
    name, match = customers.command(d["vid"], "approve draft 1")
    approved = asyncio.run(customers.run_command(d["vid"], name, match, "", d["actor"]))
    assert "Approved 1 draft(s)" in approved["body_md"], approved["body_md"]
    name, match = customers.command(d["vid"], "redo draft 1")
    kept = asyncio.run(customers.run_command(d["vid"], name, match, "", d["actor"]))
    assert "Approved drafts stay" in kept["body_md"]
    name, match = customers.command(d["vid"], "regenerate the drafts")
    reply = asyncio.run(customers.run_command(d["vid"], name, match, "", d["actor"]))
    assert "Rewrote 1 draft(s)" in reply["body_md"] and "Kept 1 draft(s)" in reply["body_md"]
    states = sorted(p["review_state"] for p in customers._pending(customers._view(d["vid"])))
    assert states == ["approved", "draft"]


@pytest.mark.parametrize("text", ["rewrite both drafts from scratch", "redo all the drafts", "regenerate draft 2"])
def test_rewrite_leaves_normal_chat_alone_without_outreach_drafts(journey_db, text):
    from app.chat import customers
    from app.chat.email_revision import target

    assert customers.command(journey_db["vid"], text) is None
    assert target("Revise the saved Ada Bakery interview email for review.") == "Ada Bakery"
