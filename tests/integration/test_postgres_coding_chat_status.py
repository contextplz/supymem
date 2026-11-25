"""Exact source QA/read-only chat projection against the real migrated database."""
import asyncio
import json

import pytest

from tests.integration import test_postgres_subscription_static as source

pg_database = source.pg_database
journey_db = source.journey_db
planning_standard = source.planning_standard
planning = source.planning
ready_plan = source.ready_plan
source_ready = source.source_ready
source_cloud = source.source_cloud


def status(d):
    from app.chat.coding_source_status import snapshot
    from app.db.supymem_models import CodingSourceReceipt
    with d["factory"]() as session:
        before = set(session.dirty), set(session.new)
        result = snapshot(session, session.get(CodingSourceReceipt, d["order_id"]))
        assert (set(session.dirty), set(session.new)) == before
        return result


@pytest.fixture
def passed_source(source_cloud):
    d = source_cloud
    source.source_step(d)
    d["qa_state"]["phase"] = "completed"
    source.source_step(d)
    assert source.source_step(d)["state"] == "succeeded"
    return d


def test_exact_passed_source_qa_remains_distinct_from_acceptance_publication_and_mac_goal(passed_source, monkeypatch):
    from app.chat import assistant
    from app.db.supymem_models import CodingSourceReceipt
    from app.ventures.state_brief import get_venture_state_brief
    d = passed_source
    facts = status(d)
    assert facts["qa_status"] == "passed" and facts["qa_passed"] > 0
    assert facts["qa_failed"] == facts["qa_unsupported"] == 0
    assert facts["current_scope"] is True
    assert facts["source_accepted"] is facts["source_published"] is False
    brief = get_venture_state_brief(d["vid"], force=True)
    coding = brief["device_work"]["latest_coding_source"]
    assert coding["id"] == d["order_id"] and coding["source_qa"] == facts
    with d["factory"]() as session:
        assert coding["source_sha256"] == session.get(CodingSourceReceipt, d["order_id"]).sha256
    assert "Independent static runtime-QA passed" in brief["_rendered"]
    assert "independent QA and hosting not attested" not in brief["_rendered"]
    monkeypatch.setattr(assistant, "get_venture_state_brief", lambda *_args, **_kw: brief)
    async def no_model(*_args, **_kw):
        pytest.fail("saved product readiness must not depend on model history")
    monkeypatch.setattr(assistant, "_complete", no_model)
    reply = asyncio.run(assistant.answer_question(d["vid"], "Is the product ready? State source, QA and publication status only."))
    assert "Independent static runtime-QA passed" in reply["body_md"]
    assert "Source acceptance is not verified" in reply["body_md"]
    assert "Publication of this exact source is not verified" in reply["body_md"]
    assert "cannot start that run" not in reply["body_md"]


@pytest.mark.parametrize("fault,expected", [
    ("source_changed", "not_run"), ("other_order", "not_run"),
    ("report_tampered", "unverified"), ("zero_passed", "unverified"),
    ("artifact_changed", "unverified"), ("plan_changed", "passed"), ("review_malformed", "passed"),
])
def test_mismatched_or_stale_saved_rows_never_borrow_qa_or_current_approval(passed_source, fault, expected):
    from app.db.supymem_models import BuildArtifact, BuildRun, CodingSourceReceipt, ExecutionPlan, QaRun
    d = passed_source
    with d["factory"]() as session:
        run = session.get(BuildRun, d["build_run_id"])
        spec = json.loads(run.spec)
        if fault == "source_changed":
            session.get(CodingSourceReceipt, d["order_id"]).sha256 = "f" * 64
        elif fault == "other_order":
            spec["source_import"]["identity"]["scope"]["order_id"] = "dwo_other"
            run.spec = json.dumps(spec)
        elif fault == "report_tampered":
            qa = session.get(QaRun, spec["lifecycle"]["qa_id"])
            receipt = json.loads(qa.receipt_json)
            receipt["input"]["source_sha256"] = "f" * 64
            qa.receipt_json = json.dumps(receipt)
        elif fault == "zero_passed":
            session.get(QaRun, spec["lifecycle"]["qa_id"]).passed = 0
        elif fault == "artifact_changed":
            session.get(BuildArtifact, run.artifact_id).sha256 = "f" * 64
        elif fault == "review_malformed":
            spec["lifecycle"]["review"] = []
            run.spec = json.dumps(spec)
        else:
            plan = session.get(ExecutionPlan, d["plan_id"])
            doc = json.loads(plan.structured)
            doc["proposal"]["milestones"][0]["tasks"][0]["landing_expectations"]["contract"]["product_name"] = "Changed after QA"
            plan.structured = json.dumps(doc)
        session.commit()
    facts = status(d)
    assert facts["qa_status"] == expected
    assert facts["source_accepted"] is facts["source_published"] is False
    if fault == "plan_changed":
        assert facts["current_scope"] is False  # Historical proof is not erased.


def test_running_qa_is_pending_without_a_success_or_effect(source_cloud):
    d = source_cloud
    source.source_step(d)
    facts = status(d)
    assert facts["qa_status"] == "pending"
    assert "qa_passed" not in facts and facts["source_accepted"] is False


@pytest.mark.parametrize("published_url,expected", [("https://wrong-host.invalid/", False), (None, True)])
def test_exact_publication_and_bad_url_do_not_erase_independent_qa(passed_source, published_url, expected):
    from app.builder.execution import read, review
    from app.db.supymem_models import Deployment, FounderApp
    from app.credits._txn import naive_utc
    d = passed_source
    view = read(d["build_run_id"], actor=d["actor"])
    review(d["build_run_id"], actor=d["actor"], source_artifact_id=view["artifact_id"],
           report_digest=view["review"]["report_digest"], decision="accept")
    with d["factory"]() as session:
        app = session.get(FounderApp, d["app_id"])
        from core.ids import new_id
        session.add(Deployment(id=new_id("dep"), app_id=d["app_id"], workspace_id=d["wid"],
            venture_id=d["vid"], build_artifact_id=view["artifact_id"], target="supymem_static",
            state="live", version=1, url=published_url or f"https://{app.slug}.supymem.com/", live_at=naive_utc()))
        session.commit()
    facts = status(d)
    assert facts["qa_status"] == "passed" and facts["source_accepted"] is True
    assert facts["source_published"] is expected


@pytest.mark.parametrize("case_status", ["failed", "unsupported"])
def test_closed_failed_qa_reports_exact_negative_counts(source_cloud, monkeypatch, case_status):
    from app.builder import qa_lifecycle
    from tests.qa_storage_fixtures import report_fixture
    d = source_cloud
    source.source_step(d)
    def failure(binding, _reader):
        raw, images = report_fixture(d["qa_state"], binding["build_id"])
        doc = json.loads(raw)
        doc["outcome"] = "failed"
        doc["cases"][0]["status"] = case_status
        doc["coverage"]["unsupported_cases"] = int(case_status == "unsupported")
        from core.artifact_snapshot import canonical
        return canonical(doc), images
    monkeypatch.setattr(qa_lifecycle.qa_transport, "result", failure)
    d["qa_state"]["phase"] = "completed"
    source.source_step(d)
    assert source.source_step(d)["state"] == "failed"
    facts = status(d)
    assert facts["qa_status"] == "failed"
    assert facts["qa_failed"] == int(case_status == "failed")
    assert facts["qa_unsupported"] == int(case_status == "unsupported")
    assert facts["source_accepted"] is facts["source_published"] is False
