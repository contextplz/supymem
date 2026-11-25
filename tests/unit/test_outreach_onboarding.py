"""OT-26: First Five Minutes six fields gate the named-people queue."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.gtm.test_known import known_case, journey_db


def test_outreach_setup_reuses_known_audience_but_not_sending_approval(known_case, monkeypatch):
    from app.connections import context_docs
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    from app.credits.policy import set_policy
    from app.db.models import GtmOutreachSettings, Venture
    from app.gtm.relationships import service
    from app.ventures import founder_facts, startup_review

    data = known_case
    if is_local():
        update_settings("capabilities", {"connected_apps": True}, {})
        update_settings("composio", {}, {"api_key": "test-connection-key"})
    view = service.onboarding_status(data["wid"])
    assert view["icp"] and view["segment"]["who"] == data["audience"]
    assert view["known_checks"]["audience"]["status"] == "known"
    assert not view["ready"] and not view["sender_geo"] and not view["excludes"]
    with data["factory"]() as session:
        settings = session.get(GtmOutreachSettings, data["wid"])
        assert not settings.sender_postal_address
        startup_review._document(session, data["vid"], startup_review.INTERNAL_KIND,
            {"founder_facts": {"audience": founder_facts._fact(data["audience"], "website", .8)}})
        session.commit()
    view = service.onboarding_status(data["wid"])
    assert not view["icp"] and view["known_checks"]["audience"]["status"] == "confirm"
    monkeypatch.setattr(context_docs, "research_context", lambda *_args: {"connected_context": {"documents": [
        {"title": "Your selected strategy", "excerpt": "Audience: Museum visitors"}]}})
    view = service.onboarding_status(data["wid"])
    assert not view["icp"] and view["known_checks"]["audience"]["status"] == "conflict"
    service.patch_onboarding(data["wid"], {"who": data["audience"]})
    assert service.onboarding_status(data["wid"])["known_checks"]["audience"]["status"] == "known"
    set_policy("feature_flags", {"known_check_v1": False}, "test")
    view = service.onboarding_status(data["wid"])
    if is_local():
        assert view["known_checks"]["audience"]["status"] == "known"
        assert not view["ready"]
    else:
        assert "known_checks" not in view
    from app.config import settings as config
    from app.memory import context
    import json
    monkeypatch.setattr(config, "company_os_memory_facts_enabled", True)
    set_policy("feature_flags", {"known_check_v1": True}, "test")
    remembered = {"excludes": [], "sender_geo": "CA", "sender_postal_address": "24 Grove Street, Ottawa, CA",
                  "voice": "Brief, quote the message you received."}

    def memory(_session, **kwargs):
        field = kwargs["query"].replace(" ", "_")
        value = remembered.get(field)
        return {"facts": [{"id": "memory_" + field, "subject": "preference." + field,
            "statement": json.dumps(value) if isinstance(value, list) else value,
            "source": {"type": "chat"}}]} if value is not None else {}

    monkeypatch.setattr(context, "context_for", memory)
    view = service.onboarding_status(data["wid"])
    assert all(view[field] for field in ("icp", "excludes", "sender_geo", "send_path", "voice"))
    assert not view["ready"] and not view["evidence"]
    assert view["sender_postal_address"] == remembered["sender_postal_address"]


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot26.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    state_mod.reset_db_factory()
    return ensure_desk(LOCAL_PERSON_ID).id


def test_empty_icp_queue_empty_not_ready(workspace):
    from app.gtm.relationships.service import list_queue, onboarding_status

    st = onboarding_status(workspace)
    assert st["ready"] is False
    assert st["icp"] is False
    q = list_queue(workspace)
    assert q["items"] == []
    assert q["ready"] is False


def test_all_six_ready(workspace):
    from app.gtm.relationships.ingest import ingest_paste
    from app.gtm.relationships.service import onboarding_status, patch_onboarding

    patch_onboarding(
        workspace,
        {
            "who": "Founders at developer-tool companies who live in Slack",
            "roles": ["founder"],
            "excludes": ["student.gmail"],
            "sender_geo": "US",
            "sender_postal_address": "1 Main St, Oakland, CA 94612",
            "outbound_mode": "human_clipboard",
            "voice": {"write_like": "short, quote them, no pitch"},
        },
    )
    ingest_paste(
        workspace,
        source="job_post_paste",
        raw_text='Company: Acme\n"we still run this workflow in a spreadsheet"',
        company_name="Acme",
        enrich=False,
    )
    st = onboarding_status(workspace)
    assert st["icp"] is True
    assert st["excludes"] is True
    assert st["sender_geo"] is True
    assert st["send_path"] is True
    assert st["voice"] is True
    assert st["evidence"] is True
    assert st["ready"] is True


def test_slack_start_counts_as_evidence_without_paste(workspace):
    from app.gtm.relationships.evidence import record_evidence_source
    from app.gtm.relationships.service import onboarding_status, patch_onboarding

    patch_onboarding(
        workspace,
        {
            "who": "Founders at developer-tool companies who live in Slack",
            "roles": ["founder"],
            "excludes": ["student.gmail"],
            "sender_geo": "US",
            "sender_postal_address": "1 Main St, Oakland, CA 94612",
            "outbound_mode": "human_clipboard",
            "voice": {"write_like": "short, quote them, no pitch"},
        },
    )
    record_evidence_source(workspace, provider="slack", action="start")
    st = onboarding_status(workspace)
    assert st["evidence"] is True
    assert st["ready"] is True


def test_connect_later_does_not_count_as_evidence(workspace):
    from app.gtm.relationships.evidence import record_evidence_source
    from app.gtm.relationships.service import onboarding_status

    record_evidence_source(workspace, provider="github", action="connect_later")
    st = onboarding_status(workspace)
    assert st["evidence"] is False


def test_active_gmail_account_is_visible_in_onboarding_sources(workspace, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.db.models import GtmEmailAccount
    from app.gtm.relationships.service import onboarding_status
    from app.state import db
    from core.clock import utcnow

    now = utcnow()
    with db()() as session:
        session.add(
            GtmEmailAccount(
                id="gtmea_connected",
                workspace_id=workspace,
                app_user_id="person_test",
                email="founder@example.test",
                composio_user_id=f"wsp_{workspace}",
                connected_account_id="ca_test_gmail",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    st = onboarding_status(workspace)
    gmail = next(source for source in st["evidence_sources"] if source["provider"] == "gmail")
    assert gmail == {
        "provider": "gmail",
        "status": "connected",
        "connected_account_id": "ca_test_gmail",
        "fake": False,
    }
    assert st["evidence"] is True
