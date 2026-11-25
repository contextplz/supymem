"""Email-Supymem port: Hunter mapping, congrats drafts, dry-run sequence."""

from __future__ import annotations

from unittest.mock import patch

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.gtm.relationships.tone import lint_draft
from app.gtm.supymem.draft import draft_step
from app.gtm.supymem.hunter import _confidence_from_score, normalize_person
from app.gtm.supymem.pipeline import ingest_leads, start_outreach
from app.gtm.supymem.brightdata import normalize_linkedin_url


def test_hunter_valid_email_is_safe():
    assert _confidence_from_score(92, "valid") == "safe"
    assert _confidence_from_score(40, "unknown") == "guess"
    assert _confidence_from_score(70, "accept_all") == "risky"


def test_hunter_normalize_linkedin_handle():
    person = normalize_person(
        {
            "data": {
                "name": {"fullName": "Ada Lovelace", "givenName": "Ada", "familyName": "Lovelace"},
                "employment": {"title": "Founder", "name": "Analytical", "domain": "analytical.example"},
                "linkedin": {"handle": "ada-lovelace"},
                "email": "ada@analytical.example",
            }
        },
        input_email="ada@analytical.example",
    )
    assert person["first_name"] == "Ada"
    assert person["linkedin_url"] == "https://www.linkedin.com/in/ada-lovelace/"
    assert person["email"] == "ada@analytical.example"


def test_linkedin_url_normalized():
    assert (
        normalize_linkedin_url("https://linkedin.com/in/Ada-Lovelace/?trk=x")
        == "https://www.linkedin.com/in/Ada-Lovelace/"
    )


def test_day_one_draft_refuses_product_name(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    step = draft_step(
        first_name="Ada",
        email="ada@example.com",
        company="Analytical",
        title="Founder",
        hook="got into YC W26",
        linkedin_url=None,
        research_notes=None,
        day=1,
        product_name="Supymem",
    )
    lint = lint_draft(
        step["body"], hook="got into YC W26", step="congrats", product_name="Supymem"
    )
    assert lint.ok
    assert "supymem" not in step["body"].lower()


def test_opengtm_messages_drive_draft_and_seed():
    from app.gtm.supymem.draft import seed_sequence_from_opengtm

    msgs = {
        "pattern": "A",
        "best_finding": "meta description missing",
        "connection_note": "Noticed a quick win on analytical.example. Connect?",
        "first_dm": "I noticed analytical.example has no meta description. Quick win.",
        "followup": "Had another look at analytical.example.",
        "followup_2": "Quick data point on analytical.example.",
        "followup_3": "Last message on analytical.example.",
    }
    step = draft_step(
        first_name="Ada",
        email="ada@analytical.example",
        company="Analytical",
        title="Founder",
        hook="meta description missing",
        linkedin_url=None,
        research_notes=None,
        day=1,
        sequence_kind="cold",
        product_name="Supymem",
        opengtm_messages=msgs,
    )
    assert step["source"] == "opengtm"
    assert step["pattern"] == "A"
    assert "meta description" in step["body"].lower()
    assert "supymem" not in step["body"].lower()
    seeded = seed_sequence_from_opengtm(msgs, first="Ada", company="Analytical", hook="meta")
    assert seeded["1"]["source"] == "opengtm"
    assert seeded["2"]["body"]
    assert seeded["4"]["body"]
    assert seeded["7"]["body"]


def test_ingest_and_simulated_sequence(monkeypatch, tmp_path):
    dbfile = tmp_path / "supymem.db"
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{dbfile}")
    monkeypatch.setenv("DB_URL", f"sqlite:///{dbfile}")
    monkeypatch.setenv("CHANNELS_DRY_RUN", "true")
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from app.state import reset_db_factory

    reset_db_factory()
    leads = [
        {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@analytical.example",
            "title": "Founder",
            "organization": "Analytical",
            "website": "https://analytical.example",
            "email_tier": "safe",
            "email_confidence": "safe",
            "source": "opengtm",
            "icp_score": 82,
            "icp_tier": "hot",
            "outreach_pattern": "A",
            "opengtm_messages": {
                "pattern": "A",
                "best_finding": "meta description missing",
                "first_dm": "I noticed analytical.example has no meta description.",
                "followup": "Had another look.",
                "followup_2": "Quick data point.",
                "followup_3": "Last message.",
                "connection_note": "Noticed a quick win. Connect?",
            },
        }
    ]
    ingested = ingest_leads("ws_test_supymem", leads, enrich=False)
    assert ingested["ingested"] == 1
    contact = next(c for c in ingested["contacts"] if c["email"] == "ada@analytical.example")
    assert contact["icp_tier"] == "hot"
    assert contact["outreach_pattern"] == "A"
    assert contact["sequence"]["1"]["source"] == "opengtm"
    assert "meta description" in contact["sequence"]["1"]["body"].lower()

    with patch("app.gtm.supymem.pipeline.email_confidence_allowed", return_value=True):
        result = start_outreach("ws_test_supymem", auto_send=True)
    assert result["dry_run"] is True
    assert result["enrolled"]
    from sqlalchemy import select
    from app.db.models import GtmEmailSendQueue
    from app.state import db

    with db()() as session:
        jobs = list(session.scalars(select(GtmEmailSendQueue)))
        assert jobs and all(job.workspace_id == "ws_test_supymem" for job in jobs)
    sent = result["sent"]
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_capabilities import enabled

        assert not enabled("send_email") and sent == []
    else:
        assert sent, "dry-run tick should record a simulated send"
        assert sent[0].get("simulated") is True
        assert sent[0].get("email") == "ada@analytical.example"
    from app.gtm.supymem.pipeline import get_contact

    row = get_contact("ws_test_supymem", contact["id"])
    assert row is not None
    assert "meta description" in (row["sequence"]["1"].get("body") or "").lower()


def test_congrats_repair_and_run_stop(monkeypatch, tmp_path):
    from app.gtm.supymem.factory import get_or_create_state, set_run
    from app.gtm.supymem.research import congrats_is_fresh, repair_congrats_hook

    assert congrats_is_fresh("got into YC yesterday", None) is True
    hook, repaired = repair_congrats_hook("building Analytical", "no dates here")
    assert repaired is True
    assert "congrats" not in hook.lower()

    dbfile = tmp_path / "engine.db"
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{dbfile}")
    monkeypatch.setenv("DB_URL", f"sqlite:///{dbfile}")
    from app.state import reset_db_factory

    reset_db_factory()
    state = set_run("ws_engine", discovery=True, send=True, week_index=6, icp_query="SaaS founders")
    assert state["discovery_enabled"] is True
    assert state["send_enabled"] is True
    assert state["email_monthly_target"] == 1600
    assert state["linkedin_monthly_target"] == 400
    stopped = set_run("ws_engine", discovery=False, send=False)
    assert stopped["discovery_enabled"] is False
    assert get_or_create_state("ws_engine")["send_enabled"] is False


def test_cold_draft_has_usecase(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    step = draft_step(
        first_name="Ada",
        email="ada@example.com",
        company="Analytical",
        title="Founder",
        hook="shipping AI GTM",
        linkedin_url=None,
        research_notes=None,
        day=1,
        sequence_kind="cold",
    )
    assert "use case" in step["subject"].lower() or "use case" in step["body"].lower() or "painful" in step["body"].lower()


def test_linkedin_enroll_dry(monkeypatch, tmp_path):
    from app.gtm.supymem.linkedin import enroll_linkedin, tick_linkedin
    from app.gtm.supymem.pipeline import ingest_leads

    dbfile = tmp_path / "li.db"
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{dbfile}")
    monkeypatch.setenv("DB_URL", f"sqlite:///{dbfile}")
    monkeypatch.setenv("CHANNELS_DRY_RUN", "true")
    monkeypatch.delenv("UNIPILE_API_KEY", raising=False)
    from app.state import reset_db_factory

    reset_db_factory()
    ingest_leads(
        "ws_li",
        [
            {
                "first_name": "Ada",
                "email": "ada@analytical.example",
                "linkedin_url": "https://www.linkedin.com/in/ada-lovelace/",
                "email_confidence": "safe",
                "organization": "Analytical",
                "source": "test",
            }
        ],
        enrich=False,
    )
    enrolled = enroll_linkedin("ws_li")
    assert enrolled["enrolled"]
    tick = tick_linkedin("ws_li")
    assert tick["processed"]
    assert tick["processed"][0].get("simulated") is True
