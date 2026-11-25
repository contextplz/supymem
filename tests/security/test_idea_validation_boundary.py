"""Real durable metered GStack/GBrain validation without external research."""
import json
from importlib.util import find_spec
from types import SimpleNamespace

import pytest

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import IDEA, request, run_queued, venture
from tests.idea_fixtures import install_provider, validation_completion
from tests.security.test_research_boundary import job


def validation_provider(monkeypatch, callback=None):
    from app.idea.validation_agent import NO_MEMORY_ASSESSMENT, NO_MEMORY_GAP
    if find_spec("app.gbrain") is not None:
        from app.gbrain import think
        async def gather(**kwargs):
            return think.Gathered()
        monkeypatch.setattr(think, "gather", gather)
    def completion(messages, options):
        data = json.loads(messages[-1]["content"])
        if callback:
            callback()
        # Inputs are explicit founder evidence, with no fabricated memory.
        rid = data["founder_revision_source"]["id"]
        methods = data["requested_methods"]
        report = {"verdict": "needs_evidence", "summary": "The order board remains an unvalidated hypothesis.",
            "findings": [{"method": method, "assessment": NO_MEMORY_ASSESSMENT if method == "gbrain_think" else "The founder identified one bakery but has not supplied observed demand.", "source_ids": [] if method == "gbrain_think" else [rid]} for method in methods],
            "gaps": [NO_MEMORY_GAP] if "gbrain_think" in methods else ["No customer payment evidence was supplied."],
            "proposed_tests": [{"hypothesis": "A bakery would pay to reduce missing orders", "action": "Interview the first bakery owner", "success_measure": "The owner agrees to a paid pilot"}],
            "questions": ["What does the current missed-order process cost the bakery?"]}
        report["forcing_questions"] = json.loads(validation_completion(messages).choices[0].message.content)["forcing_questions"]
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(report)),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=500, completion_tokens=300, total_tokens=800),
        )
    return install_provider(monkeypatch, completion)


@pytest.mark.parametrize("active_status", ["pending", "running"])
def test_validation_keeps_one_saved_job_across_all_active_queue_states(client, active_status):
    from sqlalchemy import func, select
    from app.db.models import Job
    from app.state import db

    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    request(client, "PATCH", base + "/revision", json={"patch": IDEA})
    revision_id = request(client, "GET", base).json()["current_revision"]["id"]
    first = request(client, "POST", base + "/validate", json={"revision_id": revision_id})
    assert first.status_code == 200, first.text
    job_id = first.json()["job_id"]
    with db()() as session:
        session.get(Job, job_id).status = active_status
        session.commit()

    repeated = request(client, "POST", base + "/validate", json={"revision_id": revision_id})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["job_id"] == job_id
    blocked = request(client, "POST", base + "/confirm", json={"revision_id": revision_id})
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error"]["code"] == "validation_pending"
    with db()() as session:
        assert session.scalar(select(func.count(Job.id)).where(Job.venture_id == vid, Job.kind == "idea.validate")) == 1


def test_validation_is_metered_durable_review_and_founder_decision(client, monkeypatch):
    vid, wid, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    request(client, "POST", base + "/case", json={"case": "explore"})
    assert request(client, "PATCH", base + "/revision", json={"patch": IDEA}).status_code == 200
    calls = validation_provider(monkeypatch)
    from app.research import metered
    original_synthesize = metered.synthesize
    policies = []
    def capture_policy(*args, **kwargs):
        policies.append(kwargs)
        return original_synthesize(*args, **kwargs)
    monkeypatch.setattr(metered, "synthesize", capture_policy)
    queued = request(client, "POST", base + "/validate", json={})
    assert queued.status_code == 200, queued.text
    assert queued.json()["session"]["state"] == "validating" and not calls
    jid = queued.json()["job_id"]
    assert request(client, "POST", base + "/confirm", json={}).status_code == 409
    run_queued("idea.validate")
    assert job(jid).status == "succeeded", job(jid).last_error
    assert calls[0][1]["max_tokens"] == 8192
    assert policies[0]["provider_timeout_seconds"] == 90
    assert policies[0]["pin_provider"] is True
    assert policies[0]["cap_at_reservation"] is True
    state = request(client, "GET", base).json()
    assert state["state"] == "validating" and len(state["validation_runs"]) == 3 and len(calls) == 1
    # Missing memory is established by the scoped read, not by exact LLM prose.
    from app.idea.validation_agent import NO_MEMORY_ASSESSMENT, NO_MEMORY_GAP
    report = state["validation_runs"][0]["report"]
    assert [f for f in report["findings"] if f["method"] == "gbrain_think"] == [
        {"method": "gbrain_think", "assessment": NO_MEMORY_ASSESSMENT, "source_ids": []}]
    assert NO_MEMORY_GAP in report["gaps"]
    run = state["validation_runs"][0]
    assert run["verdict"] == "needs_evidence" and run["report"]["usage"]["credits_micro"] > 0
    assert request(client, "POST", base + f"/validations/{run['id']}/decide", "usr_bob", json={"decision": "accept"}).status_code == 404
    accepted = request(client, "POST", base + f"/validations/{run['id']}/decide", json={"decision": "accept"})
    assert accepted.status_code == 200 and accepted.json()["state"] == "reviewing"
    revision = state["current_revision"]["id"]
    assert request(client, "POST", base + "/confirm", json={"revision_id": revision}).status_code == 409
    for remaining in state["validation_runs"][1:]:
        assert request(client, "POST", base + f"/validations/{remaining['id']}/decide", json={"decision": "accept"}).status_code == 200
    result = request(client, "POST", base + "/confirm", json={"revision_id": revision})
    assert result.status_code == 200 and job(result.json()["dossier_job_id"]).status == "scheduled"


def test_change_creates_revision_and_revalidation_instead_of_static_verdict(client, monkeypatch):
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    request(client, "PATCH", base + "/revision", json={"patch": IDEA})
    calls = validation_provider(monkeypatch)
    request(client, "POST", base + "/validate", json={"methods": ["gstack_ceo_review"]})
    run_queued("idea.validate")
    state = request(client, "GET", base).json()
    original = state["current_revision"]["id"]
    run = state["validation_runs"][0]
    invalid = request(client, "POST", base + f"/validations/{run['id']}/decide", json={"decision": "change", "patch": {"admin": True}})
    assert invalid.status_code == 422
    unchanged = request(client, "GET", base).json()
    assert unchanged["current_revision"]["id"] == original and unchanged["validation_runs"][0]["founder_decision"] is None
    change = request(client, "POST", base + f"/validations/{run['id']}/decide", json={"decision": "change", "patch": {"offer": "A concierge intake service first"}})
    assert change.status_code == 200, change.text
    assert change.json()["session"]["current_revision"]["id"] != original
    assert job(change.json()["job_id"]).status == "scheduled" and len(calls) == 1
    run_queued("idea.validate")
    assert job(change.json()["job_id"]).status == "succeeded" and len(calls) == 2
    stale = request(client, "POST", base + f"/validations/{run['id']}/decide", json={"decision": "accept"})
    assert stale.status_code == 409


def test_exploration_cannot_skip_validation_or_reuse_accepted_older_revision(client, monkeypatch):
    from tests.idea_fixtures import completion
    install_provider(monkeypatch, lambda *args: completion(case="explore"))
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    request(client, "POST", base + "/case", json={"case": "explore"})
    request(client, "POST", base + "/propose")
    run_queued("idea.clarify_turn")
    state = request(client, "GET", base).json()
    assert state["case"] == "explore"
    revision = state["current_revision"]["id"]
    bypass = request(client, "POST", base + "/confirm", json={"revision_id": revision})
    # No GStack run starts before Looks right, so nothing is pending; confirmation is still refused.
    assert bypass.status_code == 409 and bypass.json()["detail"]["error"]["code"] == "validation_required"
    validation_provider(monkeypatch)
    request(client, "POST", base + "/validate", json={})
    run_queued("idea.validate")
    state = request(client, "GET", base).json()
    for result in state["validation_runs"]:
        request(client, "POST", base + f"/validations/{result['id']}/decide", json={"decision": "accept"})
    changed = request(client, "PATCH", base + "/revision", json={"patch": {"offer": "Different bakery product"}}).json()
    refused = request(client, "POST", base + "/confirm", json={"revision_id": changed["id"]})
    assert refused.status_code == 409 and refused.json()["detail"]["error"]["code"] == "validation_required"


def test_edited_revision_during_review_is_not_overwritten(client, monkeypatch):
    from app.idea.service import patch_revision
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    request(client, "PATCH", base + "/revision", json={"patch": IDEA})
    calls = validation_provider(monkeypatch, lambda: patch_revision(sid, {"offer": "Founder changed the offer"}))
    queued = request(client, "POST", base + "/validate", json={})
    run_queued("idea.validate")
    assert job(queued.json()["job_id"]).status == "succeeded"
    state = request(client, "GET", base).json()
    assert not state["validation_runs"] and state["current_revision"]["structured"]["offer"] == "Founder changed the offer"
    assert len(calls) == 1


def test_gbrain_uses_actual_scoped_memory_and_excludes_foreign_venture(client, monkeypatch, tmp_path):
    import asyncio
    from app.gbrain import think
    from app.memory.adapters.local import LocalMemoryAdapter
    from app.memory.model import MemoryKind, MemoryWrite
    from app.tenancy import bind
    vid, wid, sid = venture(client)
    other, other_wid, _ = venture(client, "usr_bob")
    adapter = LocalMemoryAdapter(path=tmp_path / "gbrain-validation.sqlite")
    try:
        for selected, workspace, statement in [(vid, wid, "The founder observed missed custom cake orders."), (other, other_wid, "PRIVATE FOREIGN VENTURE MEMORY")]:
            with bind(workspace_id=workspace, venture_id=selected):
                asyncio.run(adapter.remember(MemoryWrite(venture_id=selected, kind=MemoryKind.INSIGHT,
                    subject="bakery-orders", statement=statement, confidence=1, confidence_source="founder", dedupe_key=selected)))
        monkeypatch.setattr(think, "get_memory", lambda: adapter)
        captured = []
        def response(messages, options):
            data = json.loads(messages[-1]["content"])
            captured.append(data)
            memories = data["memory_sources"]
            assert len(memories) == 1 and "PRIVATE FOREIGN" not in json.dumps(data)
            result = {"verdict": "needs_evidence", "summary": "One stored observation needs a payment experiment.",
                "findings": [{"method": "gbrain_think", "assessment": "The stored note describes missing orders, not willingness to pay.", "source_ids": [memories[0]["id"]]}],
                "gaps": ["No paying customer evidence was supplied."], "proposed_tests": [], "questions": []}
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(result)),
                    finish_reason="stop",
                )],
                usage=SimpleNamespace(prompt_tokens=300, completion_tokens=150, total_tokens=450),
            )
        install_provider(monkeypatch, response)
        base = f"/ventures/{vid}/idea/sessions/{sid}"
        request(client, "PATCH", base + "/revision", json={"patch": IDEA})
        queued = request(client, "POST", base + "/validate", json={"methods": ["gbrain_think"]})
        run_queued("idea.validate")
        assert job(queued.json()["job_id"]).status == "succeeded", job(queued.json()["job_id"]).last_error
        assert len(captured) == 1
        saved = request(client, "GET", base).json()["validation_runs"][0]
        assert saved["report"]["findings"][0]["source_ids"][0].startswith("rsf_")
    finally:
        adapter.close()


def test_only_gbrain_without_memory_records_actual_gap_without_model_or_charge(client, monkeypatch):
    from app.research import execution
    from app.idea.validation_agent import NO_MEMORY_ASSESSMENT
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    request(client, "PATCH", base + "/revision", json={"patch": IDEA})
    calls = validation_provider(monkeypatch)
    queued = request(client, "POST", base + "/validate", json={"methods": ["gbrain_think"]})
    run_queued("idea.validate")
    jid = queued.json()["job_id"]
    assert job(jid).status == "succeeded", job(jid).last_error
    saved = request(client, "GET", base).json()["validation_runs"]
    assert len(saved) == 1 and calls == []
    assert saved[0]["verdict"] == "needs_evidence"
    assert saved[0]["report"]["summary"] == NO_MEMORY_ASSESSMENT
    assert saved[0]["report"]["usage"]["source"] == "no_provider"
    assert saved[0]["report"]["usage"]["credits_micro"] == 0
    row = execution.read(execution.identifier(jid))
    assert row.calls_used == 0
    assert json.loads(row.summary)["_runtime"]["operations"] == {}


def test_invalid_validation_citation_records_rule_and_never_saves_provider_prose(client, monkeypatch):
    from app.research import execution
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    request(client, "PATCH", base + "/revision", json={"patch": IDEA})
    validation_provider(monkeypatch)
    def invalid(messages, options):
        methods = json.loads(messages[-1]["content"])["requested_methods"]
        assert "gbrain_think" not in methods
        result = {"verdict": "needs_evidence", "summary": "PRIVATE_PROVIDER_TEXT",
            "findings": [{"method": m, "assessment": "PRIVATE_PROVIDER_TEXT", "source_ids": ["foreign_source"]} for m in methods],
            "gaps": ["Missing customer evidence"], "proposed_tests": [], "questions": []}
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(result)))],
                               usage=SimpleNamespace(prompt_tokens=300, completion_tokens=150, total_tokens=450))
    calls = install_provider(monkeypatch, invalid)
    queued = request(client, "POST", base + "/validate", json={})
    run_queued("idea.validate")
    jid = queued.json()["job_id"]
    assert job(jid).last_error == "research_invalid_output" and len(calls) == 3  # two automatic repairs, also rejected
    assert request(client, "GET", base).json()["validation_runs"] == []
    row = execution.read(execution.identifier(jid))
    operation = json.loads(row.summary)["_runtime"]["operations"]["review"]
    assert operation["usage"]["output"]["validation_rule"] == "unknown_citation"
    assert operation["usage"]["actual_micro"] > 0 and operation["state"] == "completed"
    assert "PRIVATE_PROVIDER_TEXT" not in row.summary and "foreign_source" not in row.summary


def test_invalid_validation_output_is_repaired_once_before_failing_the_founder(client, monkeypatch):
    from app.research import execution
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    request(client, "PATCH", base + "/revision", json={"patch": IDEA})
    validation_provider(monkeypatch)
    seen = []
    def answer(messages, options):
        data = json.loads(messages[-1]["content"])
        seen.append(messages[0]["content"])
        rid = data["founder_revision_source"]["id"]
        cite = "foreign_source" if len(seen) == 1 else rid
        methods = data["requested_methods"]
        result = {"verdict": "needs_evidence", "summary": "Needs a paying customer.",
            "findings": [{"method": m, "assessment": "Founder statement only.", "source_ids": [cite] if m != "gbrain_think" else []} for m in methods],
            "gaps": ["No payment evidence"], "proposed_tests": [], "questions": []}
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(result)))],
                               usage=SimpleNamespace(prompt_tokens=300, completion_tokens=150, total_tokens=450))
    calls = install_provider(monkeypatch, answer)
    queued = request(client, "POST", base + "/validate", json={})
    run_queued("idea.validate")
    assert job(queued.json()["job_id"]).status == "succeeded"
    assert len(calls) == 2 and "rule: unknown_citation" in seen[1]
    assert request(client, "GET", base).json()["validation_runs"]
    operations = json.loads(execution.read(execution.identifier(queued.json()["job_id"])).summary)["_runtime"]["operations"]
    assert operations["review"]["error_code"] == "research_invalid_output" and operations["review-repair"]["state"] == "completed"


def test_invalid_validation_output_is_repaired_twice_before_failing_the_founder(client, monkeypatch):
    from app.research import execution
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    request(client, "PATCH", base + "/revision", json={"patch": IDEA})
    validation_provider(monkeypatch)
    seen = []
    def answer(messages, options):
        data = json.loads(messages[-1]["content"])
        seen.append(messages[0]["content"])
        rid = data["founder_revision_source"]["id"]
        cite = "foreign_source" if len(seen) < 3 else rid
        methods = data["requested_methods"]
        result = {"verdict": "needs_evidence", "summary": "Needs a paying customer.",
            "findings": [{"method": m, "assessment": "Founder statement only.", "source_ids": [cite] if m != "gbrain_think" else []} for m in methods],
            "gaps": ["No payment evidence"], "proposed_tests": [], "questions": []}
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(result)))],
                               usage=SimpleNamespace(prompt_tokens=300, completion_tokens=150, total_tokens=450))
    calls = install_provider(monkeypatch, answer)
    queued = request(client, "POST", base + "/validate", json={})
    run_queued("idea.validate")
    assert job(queued.json()["job_id"]).status == "succeeded"
    assert len(calls) == 3 and "rule: unknown_citation" in seen[2]
    assert request(client, "GET", base).json()["validation_runs"]
    operations = json.loads(execution.read(execution.identifier(queued.json()["job_id"])).summary)["_runtime"]["operations"]
    assert operations["review"]["error_code"] == "research_invalid_output" and operations["review-repair2"]["state"] == "completed"


@pytest.mark.parametrize('safe_repair', [True, False])
def test_discovery_privacy_rejection_is_metered_repaired_or_never_persisted(client, monkeypatch, safe_repair):
    from app.research import execution
    vid, _, sid = venture(client)
    base = f'/ventures/{vid}/idea/sessions/{sid}'
    idea = {**IDEA, 'requirements': ['Provider Planner home-childcare concept. Anonymized verbal descriptions only. No child data collection.']}
    assert request(client, 'PATCH', base + '/revision', json={'patch': idea}).status_code == 200
    validation_provider(monkeypatch)
    seen = []

    def answer(messages, options):
        data = json.loads(messages[-1]['content'])
        seen.append(messages[0]['content'])
        action = ('Interview a consenting adult provider about an anonymized general workflow.'
                  if safe_repair and len(seen) > 1 else
                  "Show me what you actually do when a parent asks how their child's day went.")
        result = {'verdict': 'needs_evidence', 'summary': 'An unvalidated adult-provider workflow hypothesis.',
            'findings': [{'method': m, 'assessment': 'Founder statement only.',
                          'source_ids': [data['founder_revision_source']['id']]} for m in data['requested_methods']],
            'gaps': ['No interviews have been conducted.'], 'proposed_tests': [
                {'hypothesis': 'One adult provider experiences administrative friction.', 'action': action,
                 'success_measure': 'An anonymized verbal report of recurring friction.'}], 'questions': []}
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps(result)))],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=150, total_tokens=450))

    calls = install_provider(monkeypatch, answer)
    queued = request(client, 'POST', base + '/validate', json={})
    jid = queued.json()['job_id']
    run_queued('idea.validate')
    operations = json.loads(execution.read(execution.identifier(jid)).summary)['_runtime']['operations']
    rejected = operations['review']
    assert rejected['usage']['output']['validation_rule'] == 'discovery_privacy'
    assert rejected['usage']['actual_micro'] > 0
    assert 'rule: discovery_privacy' in seen[1] and 'anonymized verbal workflow descriptions only' in seen[1]
    runs = request(client, 'GET', base).json()['validation_runs']
    if safe_repair:
        assert len(calls) == 2 and job(jid).status == 'succeeded'
        assert all("Show me" not in json.dumps(run['report']) for run in runs)
        assert runs and operations['review-repair']['state'] == 'completed'
    else:
        assert len(calls) == 3 and job(jid).status == 'failed_dead'
        assert not runs and job(jid).last_error == 'research_invalid_output'


@pytest.mark.parametrize('complete_repair', [True, False])
def test_overlong_discovery_action_repairs_complete_or_exhausts_without_clipped_result(client, monkeypatch, complete_repair):
    from app.research import execution
    from app.idea.validation_agent import ProposedTest
    vid, _, sid = venture(client)
    base = f'/ventures/{vid}/idea/sessions/{sid}'
    assert request(client, 'PATCH', base + '/revision', json={'patch': IDEA}).status_code == 200
    validation_provider(monkeypatch)
    seen = []
    complete = 'Ask one consenting adult about an anonymized general workflow. Do not collect private records.'
    limit = ProposedTest.model_json_schema()['properties']['action']['maxLength']
    phrase = 'Ask one consenting adult about a general workflow. '
    overlong = phrase * (limit // len(phrase) + 1) + 'Do not collect private records.'
    assert len(complete) <= limit < len(overlong)

    def answer(messages, options):
        data = json.loads(messages[-1]['content'])
        seen.append(messages[0]['content'])
        action = complete if complete_repair and len(seen) > 1 else overlong
        result = {'verdict': 'needs_evidence', 'summary': 'Demand remains unknown.',
            'findings': [{'method': m, 'assessment': 'Founder statement only.',
                          'source_ids': [data['founder_revision_source']['id']]} for m in data['requested_methods']],
            'gaps': ['No interviews have been conducted.'], 'proposed_tests': [
                {'hypothesis': 'One adult reports recurring workflow friction.', 'action': action,
                 'success_measure': 'One anonymized verbal report; no verified demand.'}], 'questions': []}
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps(result)))],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=150, total_tokens=450))

    calls = install_provider(monkeypatch, answer)
    jid = request(client, 'POST', base + '/validate', json={}).json()['job_id']
    run_queued('idea.validate')
    operations = json.loads(execution.read(execution.identifier(jid)).summary)['_runtime']['operations']
    assert operations['review']['usage']['output']['validation_kind'] == 'schema'
    assert operations['review']['usage']['actual_micro'] > 0
    assert 'Keep every string within its schema maxLength' in seen[1]
    assert 'Rejected fields:' in seen[1] and 'action: shorten this string' in seen[1]
    runs = request(client, 'GET', base).json()['validation_runs']
    if complete_repair:
        assert len(calls) == 2 and job(jid).status == 'succeeded'
        assert runs and all(r['report']['proposed_tests'][0]['action'] == complete for r in runs)
    else:
        assert len(calls) == 3 and job(jid).status == 'failed_dead'
        assert not runs and job(jid).last_error == 'research_invalid_output'
