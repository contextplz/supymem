"""OT-35: unsubscribe token for A does not suppress B; G3 honors opt-out."""

from __future__ import annotations

import os
from secrets import token_urlsafe
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.company_os_harness import load_company_os_app
from tests.integration.test_postgres_auth_security import pg_database as pg_database


@pytest.fixture()
def setup(tmp_path, monkeypatch, request):
    database_url = f"sqlite:///{tmp_path}/ot35.db"
    if getattr(request, "param", None) == "postgres":
        database_url, _ = request.getfixturevalue("pg_database")
    monkeypatch.setenv("COMPANY_OS_DB_URL", database_url)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("OUTREACH_UNSUB_SECRET", token_urlsafe(32))
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    main = load_company_os_app(reset=not database_url.startswith("postgresql"))
    from app import state as state_mod
    from app.gtm.relationships.ingest import ingest_paste
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk
    from app.workspaces.service import create_workspace

    state_mod.reset_db_factory()
    desk_a = ensure_desk(LOCAL_PERSON_ID)
    ws_b = create_workspace(
        kind="company",
        name="TenantBUnsub",
        entry_mode="existing_company",
        owner_id=LOCAL_PERSON_ID,
    )
    a = ingest_paste(
        desk_a.id,
        source="profile_paste",
        raw_text="Ada A\nada@shared-mail.test\nFounder\n",
        enrich=False,
    )
    b = ingest_paste(
        ws_b.id,
        source="profile_paste",
        raw_text="Bea B\nada@shared-mail.test\nFounder\n",
        enrich=False,
    )
    return {
        "main": main,
        "a": desk_a.id,
        "b": ws_b.id,
        "contact_a": a["contact_id"],
        "contact_b": b["contact_id"],
    }


@pytest.fixture(params=[("kestrel-audit.example", "business"), ("willowcycles.example", "consumer")])
def recipient(setup, monkeypatch, request):
    import json
    from app.authz.workspace import ensure_subject_person
    from app.config import settings
    from app.db.models import GtmContact
    from app.gtm.relationships.ingest import ingest_paste
    from app.state import db
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    domain, subscriber = request.param
    email = f"{setup['contact_a']}@{domain}"
    monkeypatch.setenv("EMAIL_PUBLIC_URL", "https://email.example.test")
    monkeypatch.setenv("EMAIL_PHYSICAL_ADDRESS", "100 Market Street, San Francisco, CA 94105")
    from app.core.local_mode import is_local
    if is_local():
        monkeypatch.setenv("SUPYMEM_PUBLIC_URL", os.environ["EMAIL_PUBLIC_URL"])
        from app.core.local_secrets import update_settings
        update_settings("smtp", {"host": "127.0.0.1", "user": "you@owned.example", "sender_name": "Your sender",
            "postal_address": os.environ["EMAIL_PHYSICAL_ADDRESS"]}, {"password": token_urlsafe(24)})
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "company_os_vault_secret", "company-os-local-vault")
    other = create_workspace(kind="company", name=domain, entry_mode="existing_company",
        owner_id=ensure_subject_person("usr_unsubscribe_other_owner"))
    outsider = ingest_paste(other.id, source="profile_paste", raw_text=f"Reader\n{email}\nFounder\n", enrich=False)
    with db()() as session:
        for contact_id in (setup["contact_a"], setup["contact_b"], outsider["contact_id"]):
            contact = session.get(GtmContact, contact_id)
            contact.email = email
            contact.recipient_geo = "US"
            contact.lawful_basis = "consent"
            contact.lawful_basis_at = utcnow()
            contact.lawful_basis_note = "Asked to receive this research email."
            contact.enrichment_json = json.dumps({"contact_source_url": f"https://{domain}/contact",
                "outreach_hint": {"subscriber": subscriber, "country": "US"}})
        session.commit()
    return {**setup, "email": email, "domain": domain, "other": other.id, "outsider": outsider["contact_id"]}


@pytest.mark.parametrize("setup", ["sqlite", "postgres"], indirect=True)
def test_signed_footer_link_stops_emails_without_scanner_or_token_leaks(recipient):
    import re
    from urllib.parse import parse_qs, urlsplit
    from app.db.models import GtmContact, GtmSuppression
    from app.gtm.relationships.email_compliance import OPT_OUT, complete_body, source_line, validate_send
    from app.gtm.relationships.unsubscribe import issue_unsubscribe_token
    from app.state import db

    data = recipient
    with db()() as session:
        contact = session.get(GtmContact, data["contact_a"])
        body = complete_body(session, data["a"], contact, "Would you share your experience?", "Your sender")
        links = re.findall(r"https://email\.example\.test/public/unsubscribe\?t=\S+", body)
        assert len(links) == 1
        link = links[0]
        assert source_line(contact) in body and OPT_OUT in body
        assert "100 Market Street, San Francisco, CA 94105" in body and "Your sender" in body
        assert "\u2014" not in body
        validate_send(session, data["a"], contact, body, "Your sender")
        assert complete_body(session, data["a"], contact, body, "Your sender") == body
        other_contact = session.get(GtmContact, data["contact_b"])
        rewritten = complete_body(session, data["b"], other_contact, body, "Your sender")
        assert link not in rewritten and rewritten.count("Stop these emails:") == 1
        token = parse_qs(urlsplit(link).query)["t"][0]
        with pytest.raises(ValueError, match="gtm_draft_one_ask_and_optout_required"):
            validate_send(session, data["a"], contact, body.replace(link, ""), "Your sender")

    path = urlsplit(link).path + "?" + urlsplit(link).query
    with TestClient(data["main"].create_app(), base_url="http://localhost", client=("127.0.0.1", 12345),
                    headers={"Origin": "http://localhost"}) as client:
        missing = client.get("/public/unsubscribe")
        assert missing.status_code == 404 and "no thanks" in missing.text
        assert client.post("/public/unsubscribe").status_code == 422
        preview = client.get(path)
        assert preview.status_code == 200
        assert '<form method="post"' in preview.text and 'type="submit"' in preview.text
        assert preview.headers["cache-control"] == "no-store"
        assert preview.headers["referrer-policy"] == "no-referrer"
        with db()() as session:
            assert session.get(GtmContact, data["contact_a"]).unsubscribed_at is None
        assert client.post("/public/unsubscribe", params={"t": token + "x"}).status_code == 404
        forged = issue_unsubscribe_token(data["other"], data["contact_a"], data["email"])
        assert client.post("/public/unsubscribe", params={"t": forged}).status_code == 404
        for _ in range(2):
            response = client.post(path, headers={"content-type": "application/x-www-form-urlencoded"})
            assert response.status_code == 200 and "You can close this page." in response.text
            assert data["a"] not in response.text and data["contact_a"] not in response.text
            assert response.headers["cache-control"] == "no-store"
        assert client.post("/public/unsubscribe", json={"token": token}).json()["ok"]
    with db()() as session:
        assert session.get(GtmContact, data["contact_a"]).unsubscribed_at is not None
        assert session.get(GtmContact, data["contact_a"]).do_not_contact
        assert session.get(GtmContact, data["contact_b"]).unsubscribed_at is None
        assert session.get(GtmContact, data["outsider"]).unsubscribed_at is None
        rows = list(session.scalars(select(GtmSuppression).where(GtmSuppression.email == data["email"])))
        assert len(rows) == 1 and rows[0].workspace_id == data["a"]


@pytest.mark.parametrize("setup", ["sqlite", "postgres"], indirect=True)
def test_suppression_stops_later_sends_across_owner_even_without_shared_learning(recipient, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from unittest.mock import Mock
    from app.db.models import GtmContact, GtmEmailAccount
    from app.gtm.relationships.email_compliance import complete_body
    from app.gtm.relationships.suppression import email_suppressed
    from app.gtm.relationships.unsubscribe import honor_unsubscribe_token, issue_unsubscribe_token
    from app.gtm.supymem import composio_gmail
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    data = recipient
    honor_unsubscribe_token(issue_unsubscribe_token(data["a"], data["contact_a"], data["email"]))
    with db()() as session:
        later_contact = session.get(GtmContact, data["contact_b"])
        assert email_suppressed(session, data["b"], later_contact)
        assert email_suppressed(session, data["b"], later_contact, replied_ok=True, suppressed_emails=set())
        assert not email_suppressed(session, data["other"], session.get(GtmContact, data["outsider"]))
        assert email_suppressed(session, data["other"], later_contact)
        sender = "sender@example.test"
        account = GtmEmailAccount(id=new_id("gtmba"), workspace_id=data["b"], email=sender,
            composio_user_id="owned-user", connected_account_id=new_id("ca"), status="active",
            created_at=utcnow(), updated_at=utcnow())
        session.add(account)
        body = complete_body(session, data["b"], later_contact, "Would you share your experience?", sender)
        session.commit()
        account_id = account.id
        connected_account_id = account.connected_account_id
    delivery = Mock(side_effect=AssertionError("Suppression must stop delivery"))
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", delivery)
    with pytest.raises(composio_gmail.DefiniteFailure, match="contact_suppressed"):
        composio_gmail._send_gmail_admitted(composio_user_id="owned-user", connected_account_id=connected_account_id,
            to=data["email"], subject="Research", body=body, thread_id=None, draft_id=None,
            workspace_id=data["b"], account_id=account_id, sender=sender)
    delivery.assert_not_called()


def test_token_for_a_does_not_suppress_b(setup):
    from app.db.models import GtmContact, GtmSuppression
    from app.gtm.relationships.gates import GateInput, evaluate_gates
    from app.gtm.relationships.unsubscribe import issue_unsubscribe_token
    from app.state import db

    token = issue_unsubscribe_token(setup["a"], setup["contact_a"], "ada@shared-mail.test")
    with TestClient(setup["main"].create_app(), base_url="http://localhost", client=("127.0.0.1", 12345),
                    headers={"Origin": "http://localhost"}) as client:
        r = client.post("/public/unsubscribe", json={"token": token})
        assert r.status_code == 200
        assert r.json()["contact_id"] == setup["contact_a"]

    with db()() as session:
        a = session.get(GtmContact, setup["contact_a"])
        b = session.get(GtmContact, setup["contact_b"])
        assert a is not None and a.unsubscribed_at is not None
        assert b is not None and b.unsubscribed_at is None
        sup_b = session.scalars(
            select(GtmSuppression).where(
                GtmSuppression.workspace_id == setup["b"],
                GtmSuppression.email == "ada@shared-mail.test",
            )
        ).first()
        assert sup_b is None

    g3 = evaluate_gates(
        GateInput(
            role="founder",
            icp_roles=("founder",),
            recipient_geo="US",
            channel="email",
            send_mode="human_clipboard",
            sender_postal_address="1 Main St",
            unsubscribed=True,
            why_this_week="They posted a hiring complaint this week",
            why_this_week_confirmed=True,
            source="paste",
        )
    )
    assert g3.void is True
    assert g3.passed["G3"] is False
