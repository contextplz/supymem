"""Charged rejected review → no-model scoped discovery note → attributed retry."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from tests.security.test_connector_oauth_boundary import client as client  # noqa: F401
from tests.security.test_durable_journey_boundary import IDEA, request, run_queued, venture
from tests.security.test_idea_validation_boundary import validation_provider
from tests.idea_fixtures import install_provider


@pytest.fixture
def failed_validation(client, monkeypatch):
    from app.db.models import Job
    from app.state import db
    vid, wid, sid = venture(client)
    base = f'/ventures/{vid}/idea/sessions/{sid}'
    idea = {**IDEA, 'requirements': ['Provider Planner adult home-childcare providers. Anonymized verbal descriptions only; no child data.']}
    assert request(client, 'PATCH', base + '/revision', json={'patch': idea}).status_code == 200
    validation_provider(monkeypatch)
    def rejected(_messages, _options):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='{broken'))],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=150, total_tokens=450))
    calls = install_provider(monkeypatch, rejected)
    queued = request(client, 'POST', base + '/validate', json={})
    jid = queued.json()['job_id']
    run_queued('idea.validate')
    with db()() as session:
        job = session.get(Job, jid)
        assert job.status == 'failed_dead' and job.last_error == 'research_invalid_output'
        payload = json.loads(job.payload_json)
    assert len(calls) == 3
    return {'vid': vid, 'wid': wid, 'sid': sid, 'rid': payload['revision_id'], 'jid': jid, 'base': base}


def payload(d, text=None):
    return {'step': 'idea.validate', 'job_id': d['jid'], 'error_code': 'research_invalid_output', **({'text': text} if text else {})}


def test_rejected_validation_diy_uses_no_model_preserves_charges_and_does_not_pass_review(failed_validation, client, monkeypatch):
    from app.chat import recovery
    from app.db.models import Job, CreditLedgerEntry
    from app.db.supymem_models import IdeaSession, ValidationRun
    from app.research import execution
    from app.state import db
    from app.gstack.office_hours import brief
    d = failed_validation
    async def forbidden(*_args, **_kwargs):
        raise AssertionError('rejected-review discovery recovery must not call a model')
    monkeypatch.setattr(recovery, '_llm', forbidden)
    monkeypatch.setattr('app.chat.assistant._complete', forbidden)
    with db()() as session:
        ledger_before = session.scalar(select(func.count(CreditLedgerEntry.id)))
    before = execution.read(execution.identifier(d['jid'])).summary
    operations = json.loads(before)['_runtime']['operations']
    assert all(op['usage']['actual_micro'] > 0 for op in operations.values())
    hidden_code = {**payload(d), "error_code": "provider_down"}
    reply = asyncio.run(recovery.alternative(d['vid'], hidden_code, d['wid']))
    assert '15-minute' in reply['body_md'] and 'anonymized verbal' in reply['body_md']
    assert 'credit' not in reply['body_md'].lower() and 'no refund' in reply['body_md']
    assert reply['actions'][0]['action'] == 'recovery.submit'
    text = 'No interview conducted yet. Proposed next step: one voluntary adult interview about anonymized general workflow. Demand remains unknown.'
    submitted = recovery.submit(d['vid'], {**payload(d, text), "error_code": ""}, d['wid'])
    assert 'founder-provided and unverified' in submitted['body_md']
    assert 'No idea was accepted or confirmed' in submitted['body_md']
    recovery.submit(d['vid'], payload(d, text), d['wid'])  # same submission is idempotent
    with db()() as session:
        assert session.scalar(select(func.count(CreditLedgerEntry.id))) == ledger_before
        assert session.get(Job, d['jid']).status == 'failed_dead'
        idea = session.get(IdeaSession, d['sid'])
        assert idea.current_revision_id == d['rid'] and idea.state != 'confirmed'
        rows = session.scalars(select(ValidationRun).where(ValidationRun.session_id == d['sid'])).all()
        assert len(rows) == 1 and rows[0].method == 'founder_provided' and rows[0].founder_decision is None
        report = json.loads(rows[0].report)
        assert report['verified'] is False and report['usage']['credits_micro'] == 0
        assert report['founder_answers'] == text and rows[0].verdict == 'needs_evidence'
        note_id = rows[0].id
        assert brief(session, d['sid'], d['rid']) is None  # not mislabeled as a GStack AI assessment
    assert request(client, 'POST', d['base'] + f'/validations/{note_id}/decide', json={'decision': 'accept'}).status_code == 409
    assert execution.read(execution.identifier(d['jid'])).summary == before
    assert request(client, 'POST', d['base'] + '/confirm', json={'revision_id': d['rid']}).status_code == 409
    from app.ventures.next_step import _facts, _resolve
    facts = _facts(d['vid'])
    assert facts['validation_done'] is False and facts['validation_failed'] is True
    # Fixture creation retains unrelated startup jobs; retain their normal
    # priority and inspect the validation banner once those jobs are idle.
    step = _resolve(d['vid'], '/v/' + d['vid'], {**facts, 'active_jobs': False})
    assert step['key'] == 'idea.validation_failed'
    assert 'did not finish' in step['title'] and 'finished clarification' not in step['detail']


def test_recovery_rejects_foreign_job_wrong_kind_and_superseded_revision(failed_validation, client, monkeypatch):
    from app.chat import recovery
    from app.db.models import Job
    from app.db.supymem_models import ValidationRun
    from app.state import db
    d = failed_validation
    async def forbidden(*_args, **_kwargs):
        raise AssertionError('invalid recovery scope must not call a model')
    monkeypatch.setattr(recovery, '_llm', forbidden)
    other_vid, other_wid, _sid = venture(client)
    reply = asyncio.run(recovery.alternative(other_vid, payload(d), other_wid))
    assert 'no longer matches' in reply['body_md']
    assert 'Nothing was saved' in recovery.submit(other_vid, payload(d, 'No interview yet.'), other_wid)['body_md']
    with db()() as session:
        job = session.get(Job, d['jid'])
        job.kind = 'research.deep'
        session.commit()
    assert 'no longer matches' in asyncio.run(recovery.alternative(d['vid'], payload(d), d['wid']))['body_md']
    with db()() as session:
        session.get(Job, d['jid']).kind = 'idea.validate'
        session.commit()
    assert request(client, 'PATCH', d['base'] + '/revision', json={'patch': {'requirements': ['New adult workflow scope.']}}).status_code == 200
    assert 'no longer matches' in asyncio.run(recovery.alternative(d['vid'], payload(d), d['wid']))['body_md']
    assert 'Nothing was saved' in recovery.submit(d['vid'], payload(d, 'No interview yet.'), d['wid'])['body_md']
    with db()() as session:
        assert not session.scalar(select(ValidationRun.id).where(ValidationRun.method == 'founder_provided'))


def test_founder_note_enters_next_review_as_unverified_without_blocking_valid_methods(failed_validation, client, monkeypatch):
    from app.chat import recovery
    from app.research import execution
    from app.db.supymem_models import ValidationRun
    from app.state import db
    d = failed_validation
    text = 'Adult interview not yet conducted; no users or payment. One anonymized workflow conversation is proposed.'
    recovery.submit(d['vid'], payload(d, text), d['wid'])
    seen = []
    calls = validation_provider(monkeypatch, lambda: None)
    from llm_gateway import sole
    primary = sole._primary_chat
    async def capture(messages, **kwargs):
        data = json.loads(messages[-1]['content'])
        if 'founder_revision_source' in data:
            seen.append(json.loads(data['founder_revision_source']['text']))
        return await primary(messages, **kwargs)
    monkeypatch.setattr(sole, '_primary_chat', capture)
    queued = request(client, 'POST', d['base'] + '/validate', json={})
    run_queued('idea.validate')
    assert seen and seen[0]['founder_discovery_notes']['text'] == text
    assert seen[0]['founder_discovery_notes']['verified'] is False
    assert seen[0]['founder_discovery_notes']['source'] == 'founder_provided'
    snapshot = json.loads(execution.read(execution.identifier(queued.json()['job_id'])).summary)['_runtime']['snapshot']
    assert snapshot['founder_discovery_notes']['failed_job_id'] == d['jid']
    with db()() as session:
        rows = session.scalars(select(ValidationRun).where(ValidationRun.session_id == d['sid'])).all()
        assert len(rows) == 4 and sum(row.method == 'founder_provided' for row in rows) == 1
    assert len(calls) == 1
    # The note is not an extra required AI method and cannot permanently poison
    # a later valid three-method review. This is explicit founder confirmation.
    confirmed = request(client, 'POST', d['base'] + '/confirm', json={'revision_id': d['rid']})
    assert confirmed.status_code == 200, confirmed.text


def test_discovery_note_cannot_smuggle_unsafe_proposed_child_observation(failed_validation):
    from app.chat import recovery
    from app.db.supymem_models import ValidationRun
    from app.state import db
    d = failed_validation
    for text in ['Next observe childcare pickups and record parent conversations.', 'Contact: synthetic@example.invalid']:
        assert 'Nothing was saved' in recovery.submit(d['vid'], payload(d, text), d['wid'])['body_md']
    with db()() as session:
        assert not session.scalar(select(ValidationRun.id).where(ValidationRun.method == 'founder_provided'))


def test_stripping_failed_job_metadata_cannot_bypass_discovery_privacy(failed_validation):
    from app.chat import recovery
    from app.db.supymem_models import ValidationRun
    from app.state import db
    d = failed_validation
    response = recovery.submit(d['vid'], {'step': 'idea.validate',
        'text': 'Next observe childcare pickups and record parent conversations.'}, d['wid'])
    assert 'could not save' in response['body_md']
    with db()() as session:
        assert not session.scalar(select(ValidationRun.id).where(ValidationRun.method == 'founder_provided'))


def test_next_step_settled_requires_supported_review_current_revision_and_tenant(failed_validation, client):
    from app.db.supymem_models import IdeaSession, ValidationRun
    from app.idea.validation import METHODS
    from app.state import db
    from app.ventures.next_step import validation_settled
    from core.clock import utcnow
    d = failed_validation
    other_vid, other_wid, other_sid = venture(client)
    with db()() as session:
        idea = session.get(IdeaSession, d['sid'])
        other = session.get(IdeaSession, other_sid)
        row = ValidationRun(id='vlr_banner_scope_test', session_id=idea.id,
            revision_id=idea.current_revision_id, workspace_id=idea.workspace_id, venture_id=idea.venture_id,
            method=METHODS[0], state='complete', report='{}', created_at=utcnow(), completed_at=utcnow())
        session.add(row)
        session.flush()
        for method in METHODS:
            row.method = method
            session.flush()
            assert validation_settled(session, idea)
        for field, invalid in [('method', 'founder_provided'), ('method', 'unknown_review'),
                               ('state', 'failed'), ('state', 'pending'), ('founder_decision', 'accept'),
                               ('workspace_id', other_wid), ('venture_id', other_vid),
                               ('revision_id', other.current_revision_id)]:
            original = getattr(row, field)
            setattr(row, field, invalid)
            session.flush()
            assert not validation_settled(session, idea), field
            setattr(row, field, original)
            session.flush()
        assert validation_settled(session, idea)
