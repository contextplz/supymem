"""Every listed lead with a published email gets a first-email draft, or the leads list says why not."""
import json

import pytest
from sqlalchemy import select

from tests.unit.gtm.test_journey import journey_db  # noqa: F401
from tests.unit.gtm.test_journey_signals import _leased_payload


def _lead(lead_id: str, name: str, domain: str, *, lead_type: str = "customer", judged: bool = True) -> dict:
    url = f"https://{domain}/contact"
    route = {"kind": "email", "email": f"hello@{domain}", "route_type": "published", "source_url": url,
             "quote": f"Write to us at hello@{domain}"}
    return {"id": lead_id, "name": name, "company": name, "kind": "business", "country": "US", "fit": 82, "source_url": url,
            "routes": [route], "judging_status": "judged" if judged else "pending", "lead_type": lead_type,
            "why": "Runs the workflow the founder is researching", "value_exchange": "Could compare notes on the workflow", "signals": []}


def _contacts(d) -> dict:
    from app.db.models import GtmContact
    with d["factory"]() as session:
        return {c.email: c.source for c in session.scalars(select(GtmContact).where(GtmContact.workspace_id == d["wid"]))}


@pytest.mark.parametrize("audience, domain, private_site", [
    ("Cold storage operators in Canada", "frostloading.example", "http://192.168.1.10/contact"),
    ("Parents planning weekend hikes", "juniperstroll.example", "https://preview.juniperstroll.example/w/private/site/"),
])
@pytest.mark.parametrize("website", ["preview", "founder", "published"])
def test_first_draft_links_only_to_a_public_website(journey_db, monkeypatch, audience, domain, private_site, website):
    from urllib.parse import parse_qs, urlsplit

    from app.autopilot import outreach
    from app.conversion import waitlist
    from app.db.models import VentureDocument
    from app.gtm import journey
    from app.gtm.relationships.unsubscribe import preview_unsubscribe
    from app.pagebuilder import local_publish
    from app.state import MEM
    from core.clock import utcnow

    data = journey_db
    wid, vid = data["wid"], data["vid"]
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("SUPYMEM_PUBLIC_URL", "https://your-instance.example")
    preview = "http://127.0.0.1:3300/preview"
    publish_url = f"https://pages.{domain}/start"
    founder_url = f"https://product.{domain}/welcome"
    expected = {"preview": None, "founder": founder_url, "published": publish_url}[website]
    monkeypatch.setitem(MEM, f"website:{vid}", {"published": False, "url": preview})
    monkeypatch.setattr(waitlist, "status", lambda *_: {"enabled": True, "url": preview,
        "total": 0, "submission_verified": False})
    with data["factory"]() as session:
        now = utcnow()
        session.add(VentureDocument(id=f"external_site:{vid}", venture_id=vid, kind="external_site",
            title="Your existing website", path=founder_url if expected else private_site, created_at=now, updated_at=now))
        session.commit()
    if website == "published":
        local_publish._save_publication(vid, wid, {"state": "live", "url": publish_url})
    journey.update_icp(wid, vid, audience, start=False)
    assert journey._ingest(wid, vid, [{"organization": domain.split(".")[0].title(),
        "website": f"https://{domain}", "source_url": f"https://{domain}/contact", "country": "CA",
        "email": f"hello@{domain}", "enriched": True, "email_confidence": "published",
        "contact_source_url": f"https://{domain}/contact", "contact_quote": f"hello@{domain}"}]) == 1
    assert journey._draft_ready_admitted(wid, vid, review_only=True) == 1
    [draft] = outreach.drafts(vid, "draft")
    body = draft["body"]
    assert all(text not in body for text in (preview, private_site, "website preview"))
    assert [line for line in body.splitlines() if line.startswith("You can see what I'm building here:")] == (
        [f"You can see what I'm building here: {expected}"] if expected else [])
    assert f"Your email address is listed publicly on {domain}." in body
    assert 'If this is not relevant, just reply "no thanks" and I will not email you again.' in body
    [opt_out] = [line.removeprefix("Stop these emails: ") for line in body.splitlines()
                 if line.startswith("Stop these emails: ")]
    unsubscribe = urlsplit(opt_out)
    assert (unsubscribe.scheme, unsubscribe.netloc, unsubscribe.path) == ("https", "your-instance.example", "/public/unsubscribe")
    [token] = parse_qs(unsubscribe.query)["t"]
    preview_unsubscribe(token)
    for private_url in (preview, f"https://pages.{domain}/ventures/{vid}/site/local/export.html"):
        local_publish._save_publication(vid, wid, {"state": "live", "url": private_url})
        assert outreach.site_url(vid) is None


# Two unrelated audiences and domains: the fix must not depend on one niche's wording.
@pytest.mark.parametrize("audience, domains", [
    ("Independent pilates studios in Austin", ("corestudio.example", "stretchloft.example")),
    ("Family-owned hardware stores in Ohio", ("millerhardware.example", "oakhillsupply.example")),
])
def test_both_published_leads_get_drafts_including_insight_leads(journey_db, audience, domains):
    from app.autopilot import outreach
    from app.gtm import journey, reachable
    d = journey_db
    journey.update_icp(d["wid"], d["vid"], audience)
    payload = _leased_payload(d)
    customer = _lead("lead-a", "First Business", domains[0])
    insight = _lead("lead-b", "Second Business", domains[1], lead_type="insight")  # listed under "People you can reach" too
    reachable._publish(journey._journal(payload), payload, [customer, insight])
    contacts = _contacts(d)
    assert {f"hello@{domain}" for domain in domains} <= contacts.keys()
    assert journey._draft_ready_admitted(d["wid"], d["vid"], review_only=True) == 2
    assert {draft["to"] for draft in outreach.drafts(d["vid"], "draft")} == {f"hello@{domain}" for domain in domains}


def test_insight_lead_saved_before_the_fix_is_backfilled_when_drafting(journey_db):
    from app.autopilot import outreach
    from app.gtm import journey
    d = journey_db
    journey.update_icp(d["wid"], d["vid"], "Independent bakery owners in Leeds")
    payload = _leased_payload(d)
    old = _lead("old-insight", "Corner Bakery", "cornerbakery.example", lead_type="insight")
    journey._journal(payload).mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(industry_experts=[old]))
    assert "hello@cornerbakery.example" not in _contacts(d)
    assert journey._draft_ready_admitted(d["wid"], d["vid"], review_only=True) == 1
    assert [draft["to"] for draft in outreach.drafts(d["vid"], "draft")] == ["hello@cornerbakery.example"]


def test_excluded_published_leads_are_labelled_not_promised_a_draft(journey_db):
    from app.chat import customers
    from app.db.models import GtmCompany, GtmContact, Venture
    from app.gtm import journey, reachable
    from core.clock import utcnow
    d = journey_db
    journey.update_icp(d["wid"], d["vid"], "Independent pilates studios in Austin")
    now = utcnow()
    with d["factory"]() as session:  # another venture in the workspace already owns this address
        session.add(Venture(id="ven_other", workspace_id=d["wid"], name="Other", raw_idea="Other idea", stage="draft", created_at=now, updated_at=now))
        session.add(GtmCompany(id="co_taken", workspace_id=d["wid"], canonical_name="Taken Studio", domain="takenstudio.example", created_at=now))
        session.flush()
        session.add(GtmContact(id="gtmc_taken", workspace_id=d["wid"], company_id="co_taken", email="hello@takenstudio.example",
            email_confidence="risky", lawful_basis="none", source="venture:ven_other:public", enrichment_json="{}", stage_changed_at=now, created_at=now))
        session.commit()
    payload = _leased_payload(d)
    good = _lead("good", "Good Studio", "goodstudio.example")
    taken = _lead("taken", "Taken Studio", "takenstudio.example")
    unchecked = _lead("unchecked", "Unchecked Studio", "uncheckedstudio.example", judged=False)
    reachable._publish(journey._journal(payload), payload, [good, taken, unchecked])
    journey._draft_ready_admitted(d["wid"], d["vid"], review_only=True)
    view = journey.status(d["wid"], d["vid"], authorize=False)
    leads = {lead["id"]: lead for lead in view["reachable_leads"] + view["industry_experts"]}
    assert "no_draft_reason" not in leads["good"]
    assert customers._email(leads["good"]) == "hello@goodstudio.example (published)"
    assert leads["taken"]["no_draft_reason"] == "this address is already a contact for another of your ventures"
    assert leads["unchecked"]["no_draft_reason"] == "not yet checked for fit"
    assert customers._email(leads["unchecked"]) == "hello@uncheckedstudio.example (published; no first email: not yet checked for fit)"
    assert _contacts(d)["hello@takenstudio.example"] == "venture:ven_other:public"  # never duplicated into this venture
    assert all("—" not in lead.get("no_draft_reason", "") for lead in leads.values())
