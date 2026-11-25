"""End to end: manifest + loader -> chat match card -> claim -> journey send gates -> honest source line -> usage for the cap."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import shutil

from sqlalchemy import select

from tests.unit.gtm.test_journey import journey_db as journey_db
from tests.unit.lead_library.test_service import DEVTOOLS, FIXTURE


import pytest

pytestmark = pytest.mark.hosted_only


def _build(tmp_path):
    data = tmp_path / "lead_library_v1.jsonl.gz"
    with open(FIXTURE, "rb") as source, gzip.open(data, "wb") as target:
        shutil.copyfileobj(source, target)
    rows = sum(1 for line in FIXTURE.read_text().splitlines() if line.strip())
    (tmp_path / "manifest.json").write_text(json.dumps({"sha256": hashlib.sha256(data.read_bytes()).hexdigest(), "rows": rows}))
    return data


def test_library_lead_flows_from_file_to_a_compliant_draft(journey_db, tmp_path, monkeypatch, capsys):
    from app.chat import customers
    from app.db.models import GtmContact, GtmOutreachSettings
    from app.gtm import journey
    from app.gtm.email_learning import record
    from app.gtm.relationships.email_compliance import OPT_OUT, complete_body
    from app.lead_library import loader
    from app.lead_library.models import LeadLibraryEntry, LeadLibraryUsage
    from core.clock import utcnow
    data = _build(tmp_path)
    assert loader.main(["--file", str(data)]) == 0
    assert json.loads(capsys.readouterr().out)["insert"] == 20
    with journey_db["factory"]() as session:
        assert session.scalar(select(LeadLibraryEntry.id).limit(1)) is None
    assert loader.main(["--file", str(data), "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["insert"] == 20

    with journey_db["factory"]() as session:
        for entry in session.scalars(select(LeadLibraryEntry)):
            entry.provenance_json = json.dumps(json.loads(entry.provenance_json) | {"email_published": True})
        session.commit()
    monkeypatch.setattr(customers, "_view", lambda vid: {"icp": DEVTOOLS})
    monkeypatch.setattr("app.intake.contacts._prepare_drafts", lambda wid, vid: "ready")
    text, actions = customers._library_section(journey_db["vid"], DEVTOOLS)
    assert text.startswith("**Top matches from Supymem's lead library**") and "more similar people" not in text and "Avery Quillwheel" in text
    assert [a["action"] for a in actions] == ["gtm.library_claim"]
    more = asyncio.run(customers.execute(journey_db["vid"], "gtm.library_more", {"offset": 3}, journey_db["actor"]))
    assert "5. **Dmitri" in more["body_md"]
    added = asyncio.run(customers.execute(journey_db["vid"], "gtm.library_claim", actions[0]["payload"], journey_db["actor"]))
    assert added["body_md"].startswith("Added 5 people from Supymem's lead library") and "Nothing was sent." in added["body_md"]
    again = asyncio.run(customers.execute(journey_db["vid"], "gtm.library_claim", actions[0]["payload"], journey_db["actor"]))
    assert "Added 0 people" in again["body_md"] and "5 already in your leads" in again["body_md"]

    now = utcnow()
    with journey_db["factory"]() as session:
        settings = session.get(GtmOutreachSettings, journey_db["wid"]) or GtmOutreachSettings(workspace_id=journey_db["wid"], created_at=now, updated_at=now)
        settings.send_enabled, settings.sender_postal_address = True, "Synthetic fixture address"
        settings.aup_accepted_at = settings.dpa_accepted_at = now
        session.add(settings)
        session.commit()
        avery = session.scalar(select(GtmContact).where(GtmContact.source == f"venture:{journey_db['vid']}:lead_library:synthetic_001"))
        assert journey._block(session, journey_db["wid"], avery, icp=DEVTOOLS) is None
        body = complete_body(session, journey_db["wid"], avery, "Hi Avery,\n\nQuick question about your CI.", "Sai, founder of Order board")
        assert "Your email address is listed publicly on directory.example.test." in body and OPT_OUT in body
        record(session, avery, "send", "msg_e2e", observed_at=now)
        avery_id = avery.id
        session.commit()
        assert session.scalar(select(LeadLibraryUsage.workspace_id).where(LeadLibraryUsage.outcome == "sent")) == journey_db["wid"]
    icp = DEVTOOLS | {"competitors": []}
    prospects = {p["id"] for p in journey._prospects(journey_db["vid"], wid=journey_db["wid"], icp=icp, current=icp)}
    assert avery_id in prospects
