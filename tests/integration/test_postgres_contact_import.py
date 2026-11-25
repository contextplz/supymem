"""Migrated PG contact import; DNS is an explicitly offline fixture."""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


@pytest.mark.parametrize("domain,company", [("bakery.example", "Bakery"), ("garden.example", "Garden planner")], ids=["b2b", "consumer"])
def test_import_500_contacts_is_reviewed_scoped_and_replay_safe(journey_db, monkeypatch, domain, company):
    from app.db.models import GtmContact, GtmTouch, Venture, VentureDocument
    from app.gtm import journey
    from app.intake import contacts
    from app.tenancy import bind
    from core.clock import utcnow
    from core.ids import new_id
    d = journey_db
    sibling_id, sibling_contact_id = new_id("ven"), new_id("gtmc")
    async def mx(domains):
        assert domains == {domain}
        return {domain: "mx" for domain in domains}
    monkeypatch.setattr(contacts, "check_domains", mx)
    monkeypatch.setattr(journey.outreach, "site_url", lambda vid: None)
    with d["factory"]() as session:
        session.add(Venture(id=sibling_id, workspace_id=d["wid"], name="Sibling", raw_idea=company, stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.add(GtmContact(id=sibling_contact_id, workspace_id=d["wid"], email=f"sibling@{domain}", source=f"venture:{sibling_id}:founder", stage_changed_at=utcnow(), created_at=utcnow()))
        session.commit()
    text = "name,email,company,status,last_contacted,country\n" + "\n".join(
        f"Founder {i},owner{i}@{domain},{company} {i},{('contacted', 'replied', 'interested', 'new')[i % 4]},2026-01-01,CA" for i in range(500))
    text += f"\nSibling,sibling@{domain},Other,new,,CA\nDuplicate,owner0@{domain},{company},contacted,2026-01-01,CA\nBad,invalid,{company},new,,CA"
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        preview = asyncio.run(contacts.preview(d["wid"], d["vid"], text, format="csv", mapping=None, actor=d["actor"]))
        assert (preview["valid_rows"], preview["duplicates"], preview["invalid_rows"]) == (500, 2, 1)
        with d["factory"]() as session:
            assert len(list(session.scalars(select(GtmContact).where(GtmContact.workspace_id == d["wid"])))) == 1
        with pytest.raises(contacts.ContactImportError, match="lawful basis"):
            contacts.commit(d["wid"], d["vid"], preview["preview_id"], actor=d["actor"], lawful_basis_attested=False, lawful_basis="consent")
        with pytest.raises(contacts.ContactImportError, match="Preview"):
            contacts.commit(d["wid"], sibling_id, preview["preview_id"], actor=d["actor"], lawful_basis_attested=True, lawful_basis="consent")
        result = contacts.commit(d["wid"], d["vid"], preview["preview_id"], actor=d["actor"], lawful_basis_attested=True, lawful_basis="consent")
        repeated = contacts.commit(d["wid"], d["vid"], preview["preview_id"], actor=d["actor"], lawful_basis_attested=True, lawful_basis="consent")
    assert result["imported"] == 500 and result["skipped"] == 3 and repeated["contact_ids"] == result["contact_ids"]
    assert result["drafts_status"] == repeated["drafts_status"] == "not_requested"
    assert result["journey"]["drafts"] == repeated["journey"]["drafts"] == []
    assert result["founder_stage"]["stage"] == "outreach"
    assert len(result["journey"]["prospects"]) == len(repeated["journey"]["prospects"]) == 500
    assert {prospect["continuation_action"] for prospect in result["journey"]["prospects"]} >= {"follow_up", "reply", "handoff"}
    assert all(prospect["imported"] for prospect in result["journey"]["prospects"])
    assert result["journey"]["results"]["sent"] == result["journey"]["results"]["replied"] == 0
    with d["factory"]() as session:
        imported = list(session.scalars(select(GtmContact).where(GtmContact.source == f"venture:{d['vid']}:founder_import")))
        assert len(imported) == 500 and all(c.contact_provenance == "founder_import" for c in imported)
        assert all(json.loads(c.enrichment_json)["founder_import"]["attested_by"] == d["actor"] and c.lawful_basis == "consent" for c in imported)
        assert all(c.gmail_thread_id is None for c in imported)
        assert session.get(GtmContact, sibling_contact_id).source == f"venture:{sibling_id}:founder"
        assert not list(session.scalars(select(GtmTouch)))
        assert session.scalar(select(VentureDocument.id).where(VentureDocument.venture_id == d["vid"], VentureDocument.kind == "founder_stage"))
