"""The Connections proof calls Gmail once and returns only bounded fields."""

from datetime import timedelta

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def mailbox_db(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/gmail_verify.db")
    load_company_os_app()
    from app.db.models import GtmEmailAccount
    from app.state import db
    from core.clock import utcnow

    now = utcnow()
    with db()() as session:
        session.add(GtmEmailAccount(
            id="gtmba_one", workspace_id="workspace_a", app_user_id="gmail:workspace_a",
            provider="gmail", email="founder@example.com", composio_user_id="gmail:workspace_a",
            connected_account_id="ca_one", status="active", created_at=now, updated_at=now,
        ))
        session.commit()
    return {"db": db, "model": GtmEmailAccount, "now": now}


def test_gmail_verification_uses_workspace_identity_and_hides_provider_payload(monkeypatch, mailbox_db):
    from app.gtm.supymem import composio_gmail, pipeline

    calls = []

    def fake_execute(slug, **kwargs):
        calls.append((slug, kwargs))
        return {
            "successful": True,
            "data": {"emailAddress": "founder@example.com", "messagesTotal": 123},
            "log_id": "log_123",
            "secret": "never-return-this",
        }

    monkeypatch.setattr(composio_gmail, "execute_tool", fake_execute)
    result = pipeline.verify_gmail_connection("workspace_a")
    assert result == {
        "ok": True, "status": "verified", "email": "founder@example.com", "log_id": "log_123", "account_id": "ca_one",
    }
    assert calls == [("GMAIL_GET_PROFILE", {
        "user_id": "gmail:workspace_a", "connected_account_id": "ca_one",
        "arguments": {}, "raise_on_error": False,
    })]


def test_gmail_verification_does_not_claim_success_without_provider_proof(monkeypatch, mailbox_db):
    from app.gtm.supymem import composio_gmail, pipeline

    monkeypatch.setattr(composio_gmail, "execute_tool", lambda *args, **kwargs: {
        "successful": False, "error": "private provider detail", "log_id": "log_failed",
        "data": {"emailAddress": "founder@example.com"},
    })
    assert pipeline.verify_gmail_connection("workspace_a") == {
        "ok": False, "status": "provider_error", "email": None, "log_id": "log_failed", "account_id": "ca_one",
    }
    monkeypatch.setattr(composio_gmail, "execute_tool", lambda *args, **kwargs: {
        "successful": True, "data": {"emailAddress": "other@example.com"}, "log_id": "log_mismatch",
    })
    assert pipeline.verify_gmail_connection("workspace_a")["status"] == "account_mismatch"


def test_gmail_verification_checks_the_mailbox_used_for_sending(monkeypatch, mailbox_db):
    from app.gtm.supymem import composio_gmail, pipeline

    with mailbox_db["db"]()() as session:
        session.add(mailbox_db["model"](
            id="gtmba_two", workspace_id="workspace_a", app_user_id="gmail:workspace_a",
            provider="gmail", email="newer@example.com", composio_user_id="gmail:workspace_a",
            connected_account_id="ca_two", status="active",
            created_at=mailbox_db["now"] + timedelta(seconds=1),
            updated_at=mailbox_db["now"] + timedelta(seconds=1),
        ))
        session.commit()
        assert pipeline._active_mailbox(session, "workspace_a").connected_account_id == "ca_two"
    assert pipeline.list_mailboxes("workspace_a")[0]["connected_account_id"] == "ca_two"

    calls = []

    def fake_profile(user_id, connected_account_id):
        calls.append((user_id, connected_account_id))
        return {"successful": True, "email": "newer@example.com", "log_id": "log_newer"}

    monkeypatch.setattr(composio_gmail, "verify_gmail_profile", fake_profile)
    assert pipeline.verify_gmail_connection("workspace_a") == {
        "ok": True, "status": "verified", "email": "newer@example.com", "log_id": "log_newer", "account_id": "ca_two",
    }
    assert calls == [("gmail:workspace_a", "ca_two")]


def test_read_only_receipt_survives_reload_and_is_workspace_scoped(monkeypatch, mailbox_db):
    from sqlalchemy import select

    from app.connections import verification
    from app.db.models import EventLog
    from app.gtm.supymem import composio_gmail

    def fake_execute(slug, **kwargs):
        assert slug == "GMAIL_GET_PROFILE"
        assert kwargs["user_id"] == "gmail:workspace_a"
        assert kwargs["connected_account_id"] == "ca_one"
        return {
            "successful": True,
            "data": {"emailAddress": "founder@example.com", "messagesTotal": 123, "token": "private-token"},
            "log_id": "log_receipt",
        }

    monkeypatch.setattr(composio_gmail, "execute_tool", fake_execute)
    result = verification.verify("workspace_a", actor_id="person_a")
    assert result["receipt_saved"] is True
    assert result["receipt_id"]
    assert verification.latest("workspace_b") == {"receipt": None}

    # A fresh read gets the database receipt, not a value retained in UI memory.
    receipt = verification.latest("workspace_a")["receipt"]
    assert receipt == {
        "receipt_id": result["receipt_id"],
        "checked_at": receipt["checked_at"],
        "request": "GMAIL_GET_PROFILE",
        "venture_id": None,
        "status": "verified",
        "account_id": "ca_one",
        "log_id": "log_receipt",
        "mail_sent": False,
    }
    assert receipt["checked_at"]
    with mailbox_db["db"]()() as session:
        row = session.scalars(select(EventLog).where(EventLog.id == result["receipt_id"])).one()
        assert row.workspace_id == "workspace_a"
        assert row.actor_id == "person_a"
        assert "founder@example.com" not in row.payload_json
        assert "private-token" not in row.payload_json
        assert "messagesTotal" not in row.payload_json


def test_profile_receipts_follow_the_selected_venture_and_validate_before_provider_call(monkeypatch, mailbox_db):
    from app.connections import verification
    from app.db.models import EventLog, Venture
    from app.gtm.supymem import composio_gmail

    with mailbox_db["db"]()() as session:
        for venture_id, workspace_id in (("ven_a", "workspace_a"), ("ven_b", "workspace_a"),
                                         ("ven_other", "workspace_b")):
            session.add(Venture(
                id=venture_id, workspace_id=workspace_id, name=venture_id,
                raw_idea="Test", stage="idea", autonomy_level="L1",
                created_at=mailbox_db["now"], updated_at=mailbox_db["now"],
            ))
        session.commit()

    calls = []
    def fake_profile(user_id, connected_account_id):
        calls.append((user_id, connected_account_id))
        return {"successful": True, "email": "founder@example.com", "log_id": "log_venture"}

    monkeypatch.setattr(composio_gmail, "verify_gmail_profile", fake_profile)
    with pytest.raises(ValueError, match="venture_unavailable"):
        verification.verify("workspace_a", actor_id="person_a", venture_id="ven_other")
    assert calls == []
    result = verification.verify("workspace_a", actor_id="person_a", venture_id="ven_a")
    assert result["ok"] is True
    assert calls == [("gmail:workspace_a", "ca_one")]
    assert verification.latest("workspace_a", venture_id="ven_a")["receipt"]["receipt_id"] == result["receipt_id"]
    assert verification.latest("workspace_a", venture_id="ven_b") == {"receipt": None}
    assert verification.latest("workspace_a") == {"receipt": None}
    with mailbox_db["db"]()() as session:
        row = session.query(EventLog).filter_by(id=result["receipt_id"]).one()
        assert row.venture_id == "ven_a"


def test_composio_refusal_is_definite_but_timeout_stays_uncertain(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    import pytest
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.gtm.supymem import composio_gmail as cg
    for text, kind in (("Invalid To header", cg.DefiniteFailure), ("Request timed out", RuntimeError)):
        monkeypatch.setattr(cg, "_request", lambda *a, _t=text, **k: {"successful": False, "error": _t})
        with pytest.raises(kind) as caught:
            cg.execute_tool("GMAIL_SEND_EMAIL", user_id="u", connected_account_id="c", arguments={})
        assert (type(caught.value) is cg.DefiniteFailure) == (kind is cg.DefiniteFailure)
