"""Synthetic receipt fixtures: no providers, storage, or outbound effects run."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def receipt_db(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.db.models import Base, GtmContact, Job, PlannedTask, Venture, VentureDocument
    from app.db.supymem_models import BuildRun, Deployment, IdeaSession, ResearchDossier, ResearchFinding
    from app.tasks import skills

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[row.__table__ for row in (
        Venture, Job, PlannedTask, VentureDocument, ResearchDossier, ResearchFinding,
        IdeaSession, GtmContact, BuildRun, Deployment)])
    with Session(engine) as session:
        now = datetime(2026, 10, 8, 12)
        session.add(Venture(id="ven_skills", workspace_id="ws_skills", name="Fixture",
            raw_idea="Synthetic test", stage="draft", created_at=now, updated_at=now))
        session.flush()
        yield skills, session, now
    engine.dispose()


def _job(d, kind, inputs, *, status="succeeded"):
    from app.db.models import Job
    skills, session, now = d
    payload = {**inputs, "_actor_subject": "usr_skills"}
    job = Job(id="job_skills", kind=kind, workspace_id="ws_skills", venture_id="ven_skills",
        payload_json=json.dumps(payload), status=status, created_at=now)
    session.add(job)
    session.flush()
    return job, {**payload, "_job_id": job.id}


def _verify(d, kind, payload):
    skills, session, _ = d
    session.flush()
    return skills.verify_result(session, kind, payload, workspace_id="ws_skills", venture_id="ven_skills")


def _document(d, *, id, kind, title="Fixture", body=None, provenance=None):
    from app.db.models import VentureDocument
    _, session, now = d
    row = VentureDocument(id=id, venture_id="ven_skills", kind=kind, title=title, body_md=body,
        provenance_json=json.dumps(provenance or {}), created_at=now, updated_at=now)
    session.add(row)
    session.flush()
    return row


def test_registry_is_closed_and_catalog_tools_are_enforced(receipt_db):
    skills, _, _ = receipt_db
    assert skills.get_skill("arbitrary.server.code") is None
    assert skills.get_skill("pagebuilder.publish").consequential
    assert not skills.get_skill("research.deep").consequential
    assert skills.enforce_allowed_tools("researcher", ["explore_web", "explore_web"]) == ("explore_web",)
    with pytest.raises(PermissionError, match="task_tool_not_allowed"):
        skills.enforce_allowed_tools("researcher", ["propose_code"])
    with pytest.raises(PermissionError, match="task_tool_not_allowed"):
        skills.enforce_allowed_tools("unknown", ["explore_web"])
    with pytest.raises(ValueError, match="task_tools_invalid"):
        skills.enforce_allowed_tools("researcher", "explore_web")


def test_scope_and_original_inputs_are_required(receipt_db):
    skills, session, _ = receipt_db
    _, payload = _job(receipt_db, "task.run", {"task_id": "task_skills"})
    result = skills.verify_result(session, "task.run", payload, workspace_id="other", venture_id="ven_skills")
    assert result["blocker"]["class"] == "policy"
    assert _verify(receipt_db, "task.run", {**payload, "task_id": "other"})["blocker"]["class"] == "policy"


def _research(d, *, error=None, operation=None, sources=True):
    from app.db.supymem_models import ResearchDossier, ResearchFinding
    from app.research.execution import identifier
    _, session, now = d
    job, payload = _job(d, "research.deep", {"session_id": "idea_skills", "revision_id": "rev_skills"})
    doc = {"_runtime": {"job_id": job.id, "session_id": "idea_skills",
        "operations": {"search": operation} if operation else {}}, "coverage_gaps": ["Synthetic source coverage limit"]}
    if error:
        doc["error_code"] = error
    dossier = ResearchDossier(id=identifier(job.id), workspace_id=job.workspace_id,
        venture_id=job.venture_id, revision_id="rev_skills", kind="deep", state="partial",
        created_at=now, completed_at=now, summary=json.dumps(doc))
    session.add(dossier)
    if sources:
        session.add(ResearchFinding(id="finding_skills", dossier_id=dossier.id,
            workspace_id=job.workspace_id, venture_id=job.venture_id, category="source", title="Synthetic",
            url="https://example.test/source", source="synthetic", summary="Not live evidence",
            evidence=json.dumps({"observed_at": now.isoformat(), "evidence_kind": "search_excerpt"}), created_at=now))
    return dossier, payload


def test_partial_research_requires_sources_and_keeps_coverage_limits(receipt_db):
    _, payload = _research(receipt_db)
    result = _verify(receipt_db, "research.deep", payload)
    assert result["verified"] and result["evidence"][0]["source_count"] == 1
    assert result["evidence"][0]["coverage_gap_count"] == 1
    assert "Synthetic source" not in str(result)
    assert len(result["evidence"][0]["source_hashes"]) == 1 and len(result["evidence"][0]["source_hashes"][0]) == 64


def test_repeated_canonical_research_urls_have_one_private_source_identity(receipt_db):
    from app.db.supymem_models import ResearchFinding
    _, session, now = receipt_db
    dossier, payload = _research(receipt_db)
    session.add(ResearchFinding(id="finding_same_source", dossier_id=dossier.id,
        workspace_id="ws_skills", venture_id="ven_skills", category="source", title="Duplicate source",
        url="https://EXAMPLE.test:443/source#section", source="synthetic", summary="Synthetic duplicate",
        evidence=json.dumps({"observed_at": now.isoformat(), "evidence_kind": "search_excerpt"}), created_at=now))
    receipt = _verify(receipt_db, "research.deep", payload)
    assert receipt["verified"] and receipt["evidence"][0]["source_count"] == 1 and len(receipt["evidence"][0]["source_hashes"]) == 1


@pytest.mark.parametrize("options", [
    {"sources": False}, {"error": "research_outcome_uncertain"},
    {"operation": {"state": "calling", "usage": {}}},
])
def test_research_returning_does_not_complete_stopped_or_empty_work(receipt_db, options):
    _, payload = _research(receipt_db, **options)
    result = _verify(receipt_db, "research.deep", payload)
    assert not result["verified"] and not result["pending"] and result["blocker"]


def _task(d, *, status="done", title="Draft one interview email", simulated=False, note=True):
    from app.db.models import PlannedTask
    _, session, now = d
    _, payload = _job(d, "task.run", {"task_id": "task_skills"})
    session.add(PlannedTask(id="task_skills", workspace_id="ws_skills", venture_id="ven_skills",
        title=title, why_now="Synthetic", status=status, created_at=now, updated_at=now,
        finished_at=now if status == "done" else None))
    if note:
        body = "Status: simulated draft prepared (not published)" if simulated else "Status: completed"
        _document(d, id="task-note:task_skills", kind="task_note", title=title,
            body=f"# {title}\n\n{body}\n\nSynthetic artifact\n",
            provenance={"task_id": "task_skills", "source": "task.run"})
    return payload


def test_completed_planned_task_requires_durable_artifact(receipt_db):
    result = _verify(receipt_db, "task.run", _task(receipt_db))
    assert result["verified"] and result["evidence"][0]["state"] == "completed"


@pytest.mark.parametrize("options", [{"status": "open"}, {"note": False},
    {"title": "Publish an outreach post", "simulated": True}])
def test_handler_success_cannot_complete_parked_missing_or_simulated_work(receipt_db, options):
    result = _verify(receipt_db, "task.run", _task(receipt_db, **options))
    assert not result["verified"] and result["blocker"]


def test_simulated_draft_only_satisfies_explicit_draft_goal(receipt_db):
    result = _verify(receipt_db, "task.run", _task(receipt_db, simulated=True))
    assert result["verified"] and result["evidence"][0]["state"] == "draft_prepared"


def _publish(d, *, state="pending", live=False):
    from app.builder.model_proxy import digest
    from app.db.supymem_models import BuildRun, Deployment
    from app.pagebuilder_admission import publication
    _, session, now = d
    spec = {"source_origin": "pagebuilder_candidate_v1", "lifecycle_version": 3,
        "actor": "usr_skills", "source_import": {"identity": {"hostname": "skills.supymem.test",
        "scope": {"caller_subject": "usr_skills"}}}, "runtime": {"qa_compute_ceiling_micro": 100},
        "qa_state": "passed", "execution_verified": False, "source_import_verified": True,
        "lifecycle": {"review": {"decision": "accept"}}}
    run = BuildRun(id="build_skills", app_id="app_skills", workspace_id="ws_skills",
        venture_id="ven_skills", trigger="pagebuilder_source_qa", agent="synthetic", state="succeeded",
        artifact_id="artifact_skills", request_digest="synthetic-digest", closed_at=now)
    binding = publication._binding(run, spec)
    _, payload = _job(d, "pagebuilder.publish", {"build_run_id": run.id, "approval_digest": digest(binding)})
    spec["publication_request"] = {"schema_version": 1, "binding": binding,
        "job_id": "job_skills", "deployment_id": "deploy_skills"}
    run.spec = json.dumps(spec)
    session.add(run)
    deployment = Deployment(id="deploy_skills", app_id=run.app_id, workspace_id="ws_skills",
        venture_id="ven_skills", build_artifact_id=run.artifact_id, target="supymem_static", state=state,
        version=1, url="https://skills.supymem.test/", live_at=now if live else None)
    session.add(deployment)
    return run, deployment, payload


def test_publication_stays_pending_after_handler_queues_deploy(receipt_db):
    _, _, payload = _publish(receipt_db)
    result = _verify(receipt_db, "pagebuilder.publish", payload)
    assert result["pending"] and not result["verified"] and result["blocker"] is None


def test_publication_requires_live_source_bound_deployment(receipt_db):
    _, _, payload = _publish(receipt_db, state="live", live=True)
    result = _verify(receipt_db, "pagebuilder.publish", payload)
    assert result["verified"] and result["evidence"][-1]["ref"] == "deployment:deploy_skills"


def test_publication_verification_keeps_safe_failure_reason_and_logs_code(receipt_db, caplog):
    run, _, payload = _publish(receipt_db, state="failed")
    spec = json.loads(run.spec)
    spec["publication_request"].update(error_code="pagebuilder_publication_refused",
        failure_code="pagebuilder_publication_scope_mismatch")
    run.spec = json.dumps(spec)
    result = _verify(receipt_db, "pagebuilder.publish", payload)
    assert not result["verified"] and result["blocker"]["class"] == "policy"
    assert "approved request" in result["blocker"]["reason"]
    assert any(record.error_code == "pagebuilder_publication_scope_mismatch" for record in caplog.records)


def test_live_publication_recovers_lost_approval_checkpoint_without_replay(receipt_db):
    run, _, payload = _publish(receipt_db, state="live", live=True)
    spec = json.loads(run.spec)
    spec["publication_request"].pop("deployment_id")
    run.spec = json.dumps(spec)
    result = _verify(receipt_db, "pagebuilder.publish", payload)
    assert result["verified"] and result["evidence"][-1]["ref"] == "deployment:deploy_skills"


@pytest.mark.parametrize("mutation", ["source", "approval", "failed"])
def test_publication_rejects_changed_source_approval_or_failed_deploy(receipt_db, mutation):
    run, deployment, payload = _publish(receipt_db, state="live", live=True)
    if mutation == "source":
        deployment.build_artifact_id = "other"
    elif mutation == "approval":
        run.request_digest = "changed"
    else:
        deployment.state = "failed"
    result = _verify(receipt_db, "pagebuilder.publish", payload)
    assert not result["verified"] and result["blocker"]


def _gtm(d, *, state="ready", prospect=True):
    from app.db.models import GtmContact
    _, session, now = d
    _, payload = _job(d, "gtm.journey.prepare", {"journey_id": "journey_skills", "prepare_id": "prepare_skills"})
    _document(d, id="journey_skills", kind="gtm_journey", provenance={"job_id": "job_skills",
        "run_id": "prepare_skills", "state": state})
    if prospect:
        session.add(GtmContact(id="contact_skills", workspace_id="ws_skills", source="venture:ven_skills:public",
            created_at=now, stage_changed_at=now))
    return payload


def test_gtm_preparation_verifies_saved_prospects_without_send_authority(receipt_db):
    result = _verify(receipt_db, "gtm.journey.prepare", _gtm(receipt_db))
    assert result["verified"] and result["evidence"][0]["prospect_count"] == 1


def test_gtm_contact_scope_does_not_treat_venture_id_as_like_pattern(receipt_db):
    from app.db.models import GtmContact
    _, session, now = receipt_db
    payload = _gtm(receipt_db, prospect=False)
    session.add(GtmContact(id="other_contact", workspace_id="ws_skills", source="venture:venXskills:public",
        created_at=now, stage_changed_at=now))
    result = _verify(receipt_db, "gtm.journey.prepare", payload)
    assert not result["verified"] and result["blocker"]


@pytest.mark.parametrize("options", [{"state": "needs_input"}, {"prospect": False}])
def test_gtm_handler_success_cannot_complete_empty_or_blocked_journey(receipt_db, options):
    result = _verify(receipt_db, "gtm.journey.prepare", _gtm(receipt_db, **options))
    assert not result["verified"] and result["blocker"]["class"] == "needs_input"


@pytest.mark.parametrize("audience", ["warehouse dispatchers", "weekend gardeners"])
@pytest.mark.parametrize("cause", ["public_search_unavailable", "public_sources_unavailable"])
def test_customer_search_preserves_the_real_source_blocker(receipt_db, audience, cause):
    from app.db.models import VentureDocument
    _, session, _ = receipt_db
    payload = _gtm(receipt_db, state="needs_input", prospect=False)
    row = session.get(VentureDocument, "journey_skills")
    data = json.loads(row.provenance_json)
    data.update(icp={"who": audience}, error_code=cause, source_access="unavailable")
    row.provenance_json = json.dumps(data)
    result = _verify(receipt_db, "gtm.journey.prepare", payload)
    assert not result["verified"]
    copy = str(result["blocker"])
    assert "clearer audience" not in copy and "pick an audience" not in copy
    assert "Keys and connections" in copy and "retry" in copy.lower()


@pytest.mark.parametrize("audience", ["warehouse dispatchers", "weekend gardeners"])
@pytest.mark.parametrize("evidence_kind", [None, "sample", "search_lead_unverified", "search_excerpt", "excluded"])
def test_saved_research_claim_requires_observed_source_evidence(receipt_db, audience, evidence_kind):
    from app.db.supymem_models import ResearchFinding
    from app.ventures.next_step import research_report_usable
    _, session, now = receipt_db
    dossier, payload = _research(receipt_db, sources=False)
    data = json.loads(dossier.summary)
    data.update(review={"overview": audience + " need help"}, verdict={"title": "Worth testing", "why": audience})
    dossier.summary = json.dumps(data)
    if evidence_kind:
        session.add(ResearchFinding(id="observed", dossier_id=dossier.id, workspace_id=dossier.workspace_id,
            venture_id=dossier.venture_id, category="source", title=audience, url="https://example.test/review",
            source="web", summary="Observed excerpt", created_at=now,
            evidence=json.dumps({"observed_at": now.isoformat(), "evidence_kind": "search_excerpt" if evidence_kind == "excluded" else evidence_kind,
                "founder_flag": "exclude" if evidence_kind == "excluded" else None})))
    session.flush()
    assert research_report_usable(session, dossier) is (evidence_kind == "search_excerpt")
    assert _verify(receipt_db, "research.deep", payload)["verified"] is (evidence_kind == "search_excerpt")


@pytest.mark.parametrize("audience", ["warehouse dispatchers", "weekend gardeners"])
@pytest.mark.parametrize("sourced", [False, True])
def test_customer_result_success_has_verifiable_source_references(receipt_db, audience, sourced):
    from app.db.models import VentureDocument
    _, session, _ = receipt_db
    payload = _gtm(receipt_db, prospect=False)
    row = session.get(VentureDocument, "journey_skills")
    data = json.loads(row.provenance_json)
    data["channels"] = [{"name": audience, **({"source_url": "https://example.test/community", "quote": "We need a simpler plan"} if sourced else {})}]
    row.provenance_json = json.dumps(data)
    result = _verify(receipt_db, "gtm.journey.prepare", payload)
    assert result["verified"] is sourced
    if sourced:
        assert result["evidence"][0]["source_count"] == 1
        assert result["evidence"][0]["source_hashes"] == [receipt_db[0].source_hash("https://example.test/community")]


def _page(d, monkeypatch):
    from app.pagebuilder import native_reference as references
    _, payload = _job(d, "pagebuilder.build", {"revision_id": "rev_skills"})
    capture = {"sha256": "a" * 64, "manifest_digest": "b" * 64, "size_bytes": 10}
    doc = {"revision_id": "rev_skills", "variant_count": 1, "_runtime": {"analytics_app_id": "app_skills"},
        "result": {"bundles": [{"manifest": {"artifact_capture": capture}, "spec_markdown": "Synthetic spec"}]}}
    monkeypatch.setattr(references, "_completed_candidate", lambda session, job: doc)
    binding = {"schema_version": 1, "job_id": "job_skills", "variant": 0, "revision_id": "rev_skills",
        "workspace_id": "ws_skills", "venture_id": "ven_skills", "app_id": "app_skills",
        "source_sha256": "a" * 64, "source_manifest_digest": "b" * 64, "source_size_bytes": 10,
        "spec_sha256": hashlib.sha256(b"Synthetic spec").hexdigest(), "reference_sha256": "c" * 64,
        "reference_size_bytes": 20}
    monkeypatch.setattr(references, "_encode", lambda job, doc, variant: (binding, b"synthetic"))
    receipt = {"kind": "raw", "sha256": "c" * 64, "object_digest": "c" * 64,
        "size_bytes": 20, "version_id": "synthetic-version"}
    note = _document(d, id=references._reference_id("job_skills", 0), kind=references.KIND,
        provenance={"state": "stored", "binding": binding, "receipt": receipt})
    return note, payload


def test_page_completion_requires_stored_immutable_candidate(receipt_db, monkeypatch):
    _, payload = _page(receipt_db, monkeypatch)
    result = _verify(receipt_db, "pagebuilder.build", payload)
    assert result["verified"] and result["evidence"][0]["variant"] == 0


def test_reused_unchanged_page_receipt_remains_verified(receipt_db, monkeypatch):
    _, payload = _page(receipt_db, monkeypatch)
    first = _verify(receipt_db, "pagebuilder.build", payload)
    assert _verify(receipt_db, "pagebuilder.build", payload) == first
    assert first["verified"]


def test_invalid_candidate_reports_safe_source_failure(receipt_db, monkeypatch, caplog):
    from app.pagebuilder import native_reference as references
    _, payload = _page(receipt_db, monkeypatch)
    def invalid(*_args):
        raise ValueError("pagebuilder_candidate_invalid")
    monkeypatch.setattr(references, "_completed_candidate", invalid)
    result = _verify(receipt_db, "pagebuilder.build", payload)
    assert not result["verified"] and result["blocker"]["class"] == "needs_input"
    assert "source could not be verified" in result["blocker"]["reason"]
    assert any(record.error_code == "pagebuilder_candidate_invalid" for record in caplog.records)


@pytest.mark.parametrize("field", ["state", "sha256", "version_id", "binding"])
def test_page_build_return_cannot_complete_unstored_or_changed_receipt(receipt_db, monkeypatch, field):
    note, payload = _page(receipt_db, monkeypatch)
    provenance = json.loads(note.provenance_json)
    if field == "state":
        provenance["state"] = "persisting"
    elif field == "binding":
        provenance["binding"]["reference_sha256"] = "d" * 64
        provenance["receipt"].update(sha256="d" * 64, object_digest="d" * 64)
    else:
        provenance["receipt"][field] = "null" if field == "version_id" else "d" * 64
    note.provenance_json = json.dumps(provenance)
    result = _verify(receipt_db, "pagebuilder.build", payload)
    assert not result["verified"] and result["blocker"]
