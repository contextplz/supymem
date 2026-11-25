"""Read-only correction advice uses migrated receipt/contract/credit custody."""
import json
import pytest
from sqlalchemy import select
from tests.integration.test_postgres_subscription_static import (pg_database as pg_database,
    journey_db as journey_db, planning_standard as planning_standard,
    ready_plan as ready_plan, source_ready as source_ready, source_cloud as source_cloud, source_step)

@pytest.fixture()
def planning(planning_standard, monkeypatch):
    from tests.integration import test_postgres_subscription_static as source
    monkeypatch.setattr(source, 'CONTRACT', {**source.CONTRACT, 'required_text': [
        '1. Groomer adds a booking.', '2. A reminder is sent to the client.',
        '3. The client confirms the appointment.']})
    return source.planning.__wrapped__(planning_standard, monkeypatch)

@pytest.fixture()
def failed_source(source_cloud, monkeypatch):
    from app.builder import qa_lifecycle as q
    from tests.qa_storage_fixtures import report_fixture
    from core.artifact_snapshot import canonical
    d = source_cloud
    def result(binding, _):
        blob, images = report_fixture(d['qa_state'], binding['build_id'])
        report = json.loads(blob)
        report['outcome'] = 'failed'
        report['cases'][0]['status'] = 'failed'
        report['cases'][0]['observed'] = 'Synthetic missing approved text observation: 2. A reminder is sent to the client.'
        return canonical(report), images
    monkeypatch.setattr(q.qa_transport, 'result', result)
    source_step(d)
    d['qa_state']['phase'] = 'completed'
    source_step(d)
    assert source_step(d)['closed']
    return d


def test_failed_report_advice_preserves_exact_contract_charge_and_no_effects(failed_source, monkeypatch):
    from app.chat.static_qa_guidance import answer
    from app.db.models import Job, CreditLedgerEntry
    from app.db.supymem_models import BuildRun, QaRun
    d = failed_source
    monkeypatch.setattr('app.chat.assistant._complete', lambda *a, **kw: pytest.fail('no advice model'))
    with d['factory']() as session:
        before = (list(session.scalars(select(Job.id))), list(session.scalars(select(CreditLedgerEntry.id))))
        run = session.get(BuildRun, d['build_run_id'])
        original = (run.state, run.spec, run.credits_used, run.closed_at)
        qa = session.scalar(select(QaRun).where(QaRun.build_run_id == run.id))
        counts = (qa.passed, qa.failed, qa.unsupported)
    result = answer(d['vid'], d['wid'], d['build_run_id'])
    text = result['body_md']
    assert 'Current approved exact contract' in text
    assert f'{counts[0]} passed, {counts[1]} failed and {counts[2]} unsupported' in text
    assert 'ledger units' not in text and 'credits consumed' not in text  # no credit talk; the charge is checked below
    assert 'Platform analytics is mandatory' in text and 'not a choice' in text
    assert 'contract/layout ambiguity' in text and 'do not need to invent replacement copy' in text
    assert 'Synthetic missing approved text observation' in text and result['actions'] == []
    with d['factory']() as session:
        run = session.get(BuildRun, d['build_run_id'])
        assert (run.state, run.spec, run.credits_used, run.closed_at) == original
        assert (list(session.scalars(select(Job.id))), list(session.scalars(select(CreditLedgerEntry.id)))) == before


@pytest.mark.parametrize('change', ['foreign_venture', 'foreign_workspace', 'unverified_report', 'changed_contract', 'superseded_plan', 'malformed_finding', 'source_custody_changed', 'capture_changed'])
def test_unowned_or_stale_receipt_cannot_supply_correction_contract(failed_source, change):
    from app.chat.static_qa_guidance import answer
    from app.db.supymem_models import BuildRun, QaRun, QaFinding, ExecutionPlan
    d = failed_source
    vid, wid = d['vid'], d['wid']
    if change == 'foreign_venture':
        vid = 'ven_foreign'
    elif change == 'foreign_workspace':
        wid = 'wsp_foreign'
    else:
        with d['factory']() as session:
            if change in {'source_custody_changed', 'capture_changed'}:
                run = session.get(BuildRun, d['build_run_id'])
                spec = json.loads(run.spec)
                if change == 'source_custody_changed':
                    spec['source_import_verified'] = False
                else:
                    spec['artifact_capture']['sha256'] = '0' * 64
                run.spec = json.dumps(spec)
            elif change == 'malformed_finding':
                finding = session.scalar(select(QaFinding).join(QaRun, QaFinding.qa_run_id == QaRun.id).where(QaRun.build_run_id == d['build_run_id']))
                finding.detail = 'not JSON'
            elif change == 'unverified_report':
                qa = session.scalar(select(QaRun).where(QaRun.build_run_id == d['build_run_id']))
                receipt = json.loads(qa.receipt_json)
                receipt['verified_report'] = False
                qa.receipt_json = json.dumps(receipt)
            else:
                plan = session.get(ExecutionPlan, d['plan_id'])
                if change == 'superseded_plan':
                    plan.superseded_by = 'pln_not_current'
                else:
                    doc = json.loads(plan.structured)
                    doc['proposal']['milestones'][0]['tasks'][0]['landing_expectations']['contract']['required_text'][0] = 'Changed current copy'
                    plan.structured = json.dumps(doc)
            session.commit()
    result = answer(vid, wid, d['build_run_id'])
    assert 'cannot ground' in result['body_md'] and result['actions'] == []
    assert 'Current approved exact contract' not in result['body_md']


def test_full_chat_http_preserves_exact_owned_contract_and_failed_case_bytes(failed_source, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.chat_api import router
    from app.chat.static_qa_guidance import answer, VerifiedStaticQaEvidence
    from app.db.models import ChatMessage, Job, PlannedTask, CreditLedgerEntry
    from core.assertion import sign_assertion
    REQUEST = 'The fresh stored-source QA run bsi_8471afb1aeac98797dce92ab9707cbb0de1a5479992f3ecb is closed failed, with cleanup complete, zero reserved and 0.39 credits shown. Its real browser report has 26 passed, 12 failed and zero unsupported checks. It flags missing exact planned three-step text, a missing substantial workflow illustration, missing platform analytics, and the managed waitlist link needing target=_blank with noopener noreferrer. The source still describes an unlaunched concept; it is not accepted or published. The native coding instructions did not include the exact platform analytics recipe. What are the simple next steps and what instructions must you supply before a reviewed source correction? Advice only: do not prepare or run another coding task, retry QA, change the plan, accept anything or publish. Preserve this failed run and its actual charge.'
    d = failed_source
    expected = answer(d['vid'], d['wid'], d['build_run_id'])
    assert isinstance(expected, VerifiedStaticQaEvidence)
    assert '2. A reminder is sent to the client.' in expected['body_md']
    from app.chat.router import scrub_hallucinations, assemble
    from app.tenancy import ScopeRequired, bind, current_or_none
    assert current_or_none() is None
    with pytest.raises(ScopeRequired):
        assemble(d['vid'])
    with bind(workspace_id=d['wid'], venture_id=d['vid'], actor=d['actor']):
        assert '2. A reminder is prepared to the client.' in scrub_hallucinations(expected['body_md'], assemble(d['vid']))
    assert current_or_none() is None
    secret = 'grounded-qa-http-test-at-least-thirty-two'
    monkeypatch.setenv('COS_INTERNAL_ASSERTION_SECRET', secret)
    # Planning setup installs a planning-only provider and enables live LLMs.
    # This HTTP case qualifies deterministic, provider-free advice routing;
    # a chat-router request cannot be sent to that unrelated planner fixture.
    monkeypatch.setenv('CHAT_ROUTER_MODE', 'off')
    def forbidden(*a, **kw):
        pytest.fail('verified QA evidence must not reach model or generic claim/offer rewriting')
    monkeypatch.setattr('app.chat.assistant._complete', forbidden)
    monkeypatch.setattr('app.chat.llm_router._ask_model', forbidden)
    monkeypatch.setattr('llm_gateway.sole.transmit', forbidden)
    monkeypatch.setattr('app.chat.claims.claims_action', forbidden)
    monkeypatch.setattr('app.chat.offers.with_offers', forbidden)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    path = f"/ventures/{d['vid']}/chat"
    headers = {'X-Subject': d['actor'], 'X-COS-Assertion': sign_assertion(
        secret, sub=d['actor'], email='', role='user', path=path, method='POST')}
    with d['factory']() as session:
        before = tuple(list(session.scalars(select(m.id))) for m in (Job, PlannedTask, CreditLedgerEntry))
    reply = client.post(path, json={'message': REQUEST.replace(
        'bsi_8471afb1aeac98797dce92ab9707cbb0de1a5479992f3ecb', d['build_run_id'])}, headers=headers)
    assert reply.status_code == 200, reply.text
    # Speaker prefix may be added; every exact verified body byte must survive.
    assert reply.json()['body_md'].endswith(expected['body_md'])
    assert reply.json()['actions'] == []
    assert 'A reminder is prepared to the client' not in reply.json()['body_md']
    with d['factory']() as session:
        saved = session.scalar(select(ChatMessage).where(ChatMessage.venture_id == d['vid'],
            ChatMessage.role == 'assistant').order_by(ChatMessage.created_at.desc()))
        assert saved.body_md == reply.json()['body_md']
        assert tuple(list(session.scalars(select(m.id))) for m in (Job, PlannedTask, CreditLedgerEntry)) == before
