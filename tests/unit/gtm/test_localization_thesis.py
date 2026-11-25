"""Evidence-first GTM localization and thesis lifecycle."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

def _db(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    dbfile = tmp_path / "localization.db"
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{dbfile}")
    monkeypatch.setenv("DB_URL", f"sqlite:///{dbfile}")
    from app.state import reset_db_factory

    reset_db_factory()


def _profile(workspace_id: str) -> dict:
    from app.gtm.localization import service

    return service.create_market_profile(
        workspace_id,
        name="US and India Series A SaaS",
        seller={"name": "Acme"},
        product={
            "name": "Acme Agent",
            "time_to_value": "days",
            "switching_cost": "medium",
        },
        segment={
            "industry": "B2B SaaS",
            "company_stage": "Series A",
            "countries": ["United States", "India"],
            "buyer_roles": ["Founder", "CPO", "Head of GTM"],
            "urgency": "Need more sales capacity",
            "observed_behaviors": ["Pilots convert better than generic demos."],
        },
        constraints={"languages": ["en"]},
        evidence=[
            {
                "claim": "Pilots convert better than generic demos.",
                "evidence_class": "observed_outcome",
                "source": "CRM cohort",
                "observed_at": "2026-08-31",
                "scope": "Series A SaaS",
                "confidence": 0.85,
            }
        ],
    )


def test_policy_rejects_protected_and_age_propensity():
    from app.gtm.localization import policy

    result = policy.assess_profile(
        {
            "segment": {
                "religion": "any",
                "age_bracket": "25-34",
                "industry": "software",
            },
            "constraints": {},
            "evidence": [],
        }
    )
    assert result["allowed"] is False
    assert {item["code"] for item in result["violations"]} == {
        "protected_or_sensitive_targeting",
        "age_propensity_targeting",
    }


@pytest.mark.asyncio
async def test_profile_requires_approval_before_execution(monkeypatch, tmp_path):
    from app.gtm.localization import experiments, service
    from app.gtm.supymem import pipeline

    _db(monkeypatch, tmp_path)
    workspace_id = "ws_thesis_gate"
    profile = _profile(workspace_id)

    blocked = experiments.execution_context(workspace_id)
    assert blocked["allowed"] is False
    assert blocked["mode"] == "approval_required"
    outreach = pipeline.start_outreach(workspace_id)
    assert outreach["enrolled"] == []
    assert outreach["blocker"]

    thesis = await service.generate_thesis(workspace_id, profile["id"], use_llm=False)
    assert thesis["status"] == "draft"
    assert thesis["archetype"]["dominant"] == "accelerator"
    assert thesis["evidence"][0]["evidence_class"] == "observed_outcome"
    assert thesis["policy"]["allowed"] is True

    approved = service.approve_thesis(
        workspace_id, thesis["id"], actor_id="person_test"
    )
    assert approved["status"] == "approved"
    execution = experiments.execution_context(workspace_id)
    assert execution["allowed"] is True
    assert execution["mode"] == "thesis"


@pytest.mark.asyncio
async def test_experiment_waits_for_sample_and_uses_outcomes(monkeypatch, tmp_path):
    from app.gtm.localization import experiments, service

    _db(monkeypatch, tmp_path)
    workspace_id = "ws_thesis_learning"
    profile = _profile(workspace_id)
    thesis = await service.generate_thesis(workspace_id, profile["id"], use_llm=False)
    service.approve_thesis(workspace_id, thesis["id"], actor_id="person_test")
    experiment = experiments.compile_experiment(workspace_id, thesis["id"])
    experiments.set_experiment_status(workspace_id, experiment["id"], "running")

    for index in range(30):
        experiments.record_outcome(
            workspace_id,
            "sent",
            experiment_id=experiment["id"],
            thesis_id=thesis["id"],
            variant="motivation" if index % 2 == 0 else "proof",
        )
    for _ in range(2):
        experiments.record_outcome(
            workspace_id,
            "qualified_meeting",
            experiment_id=experiment["id"],
            thesis_id=thesis["id"],
            variant="motivation",
        )
    experiments.record_outcome(
        workspace_id,
        "revenue",
        experiment_id=experiment["id"],
        thesis_id=thesis["id"],
        value=1200,
        currency="USD",
    )

    summary = experiments.summarize_experiment(workspace_id, experiment["id"])
    assert summary["sample_ready"] is True
    assert summary["observed_primary_metric"] == pytest.approx(0.0667)
    assert summary["decision"] == "continue"
    assert summary["revenue"] == 1200


@pytest.mark.asyncio
async def test_same_geography_can_produce_different_archetypes(monkeypatch, tmp_path):
    from app.gtm.localization import service

    _db(monkeypatch, tmp_path)
    workspace_id = "ws_no_geo_stereotype"
    accelerator = _profile(workspace_id)
    accelerator_thesis = await service.generate_thesis(
        workspace_id, accelerator["id"], use_llm=False
    )

    transformer = service.create_market_profile(
        workspace_id,
        name="Indian legal transformation",
        seller={"name": "Acme"},
        product={
            "name": "Legal Workflow",
            "time_to_value": "months",
            "switching_cost": "high",
        },
        segment={
            "industry": "Legal professional services",
            "company_stage": "established enterprise",
            "countries": ["India"],
            "buyer_roles": ["Managing Partner", "IT", "Compliance"],
            "urgency": "regulated workflow transformation",
            "regulation": "high",
        },
        constraints={"languages": ["en"]},
    )
    transformer_thesis = await service.generate_thesis(
        workspace_id, transformer["id"], use_llm=False
    )
    assert accelerator_thesis["archetype"]["dominant"] == "accelerator"
    assert transformer_thesis["archetype"]["dominant"] == "transformer"


@pytest.mark.asyncio
async def test_full_dry_run_thesis_to_email_linkedin_and_learning(
    monkeypatch, tmp_path
):
    """Exercise the complete safe path without contacting a real recipient."""
    from app.gtm.localization import experiments, service
    from app.gtm.supymem import pipeline

    _db(monkeypatch, tmp_path)
    monkeypatch.setenv("CHANNELS_DRY_RUN", "true")
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("UNIPILE_API_KEY", raising=False)
    workspace_id = "ws_full_gtm_dry_run"

    profile = _profile(workspace_id)
    thesis = await service.generate_thesis(workspace_id, profile["id"], use_llm=False)
    service.approve_thesis(workspace_id, thesis["id"], actor_id="person_test")
    experiment = experiments.compile_experiment(workspace_id, thesis["id"])
    experiments.set_experiment_status(workspace_id, experiment["id"], "running")

    from app.gtm.supymem import learning, linkedin
    ingested = pipeline.ingest_leads(
        workspace_id,
        [
            {
                "first_name": "Ada",
                "last_name": "Lovelace",
                "email": "ada@example.test",
                "email_confidence": "safe",
                "title": "Founder",
                "organization": "Analytical",
                "website": "https://example.test",
                "linkedin_url": "https://www.linkedin.com/in/ada-lovelace/",
                "source": "opengtm",
                "icp_score": 88,
                "icp_tier": "hot",
                "outreach_pattern": "A",
                "opengtm_messages": {
                    "pattern": "A",
                    "best_finding": "hiring a first GTM team",
                    "connection_note": "Saw the GTM hiring milestone—congrats. Open to connecting?",
                    "first_dm": "Curious how you are keeping founder-led outreach warm while hiring.",
                    "followup": "One use case: keep research and follow-up in one paced loop.",
                    "followup_2": "A similar team used a bounded pilot before changing workflow.",
                    "followup_3": "Happy to share the workflow if it is useful.",
                },
            }
        ],
        enrich=False,
    )
    contact = next(row for row in ingested["contacts"] if row["email"] == "ada@example.test")
    assert contact["sequence"]["1"]["source"] == "opengtm"

    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"send_email": False}, {})
        stopped = pipeline.start_outreach(workspace_id, auto_send=True)
        assert stopped["sent"] == []
        monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    email = pipeline.start_outreach(workspace_id, auto_send=True)
    assert email["dry_run"] is True
    assert email["sent"][0]["simulated"] is True

    li_enrolled = linkedin.enroll_linkedin(workspace_id, [contact["id"]])
    assert li_enrolled["provider"] == "human_queue"
    assert li_enrolled["enrolled"] == [contact["id"]]
    li_sent = linkedin.tick_linkedin(workspace_id, limit=1)
    assert li_sent["dry_run"] is True
    assert li_sent["processed"][0]["simulated"] is True

    pipeline.apply_reply(
        contact["id"],
        body="Yes, the bounded pilot sounds useful.",
        classification="human_positive",
    )
    learned = learning.summarize(workspace_id)
    assert learned["kinds"]["sent"] == 1
    assert learned["kinds"]["linkedin_sent"] == 1
    assert learned["kinds"]["reply_positive"] == 1
    assert learned["experiment"]["outcomes"]["sent"] == 1
    assert learned["experiment"]["outcomes"]["linkedin_sent"] == 1
