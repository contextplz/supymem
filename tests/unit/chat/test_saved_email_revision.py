"""Exact saved-draft revisions cannot become searches or cross-venture edits."""
import asyncio
import hashlib
import json

import pytest
from sqlalchemy import select

from tests.company_os_harness import load_company_os_app, local_chat as local_chat
from tests.unit.chat.test_round3_matrix import venture as venture  # noqa: F401

load_company_os_app()
REQUEST = ("Revise the saved Hulk Fitness interview email for review. Replace 'Entirely voluntary and off the record' "
           "with 'This is voluntary early research; I have no working product or verified results yet.' "
           "Ask for only a general verbal workflow account, no member records or personal details. "
           "Keep it short, one email, no links, no sales pitch, and do not send anything.")
OLD = "Draft 1\nSubject: Quick chat about member follow-up at Hulk Fitness\n\nHi Hulk Fitness,\nEntirely voluntary and off the record\nWould you discuss your general check-in workflow?\nThanks,\nSam"
GOOD = "Draft 1\nSubject: Quick chat about member follow-up at Hulk Fitness\n\nHi Hulk Fitness,\nThis is voluntary early research; I have no working product or verified results yet.\nWould you discuss your general check-in workflow verbally? No member records or personal details needed.\nThanks,\nSam"


@pytest.fixture(params=['Hulk Fitness', 'Trail Walking Circle'])
def saved(venture, monkeypatch, request):
    if request.param != 'Hulk Fitness':
        for field in ('REQUEST', 'OLD', 'GOOD'):
            monkeypatch.setitem(globals(), field, globals()[field].replace('Hulk Fitness', request.param)
                .replace('general check-in workflow', 'group-walk planning workflow'))
    from app.chat.email_revision import binding
    from app.db.models import PlannedTask, VentureDocument
    from app.planning.store import enqueue_task
    from app.state import db
    from core.clock import utcnow
    client, vid, _ = venture
    task = enqueue_task(vid, {'title': 'Draft one interview email for me to review', 'detail': 'Hulk Fitness public business interview, no verified solo fit.', 'why_now': 'reviewed', 'tags': ['chat'], 'runnable': True}, source='chat')
    with db()() as session:
        session.get(PlannedTask, task['id']).status = 'done'
        session.add(VentureDocument(id='task-note:'+task['id'], venture_id=vid, kind='task_note', title='Saved email', body_md=OLD, created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    return client, vid, task['id'], binding(vid, REQUEST)


def _no_effects(monkeypatch, complete):
    def forbidden(*_a, **_kw):
        pytest.fail('saved email revision must not invoke search or external delivery')
    monkeypatch.setattr('app.chat.assistant._complete', complete)
    monkeypatch.setattr('app.research.explore.explore_web', forbidden)
    monkeypatch.setattr('app.planning.execute._research_notes', forbidden)
    monkeypatch.setattr('app.planning.execute._harness_research', forbidden)
    monkeypatch.setattr('app.planning.execute._run_social_post', forbidden)
    monkeypatch.setattr('app.chat.search_recovery.manual_discovery_reply', forbidden)


def test_exact_live_revision_full_http_executes_writing_and_renders_one_saved_draft(saved, monkeypatch):
    from app.db.models import PlannedTask, VentureDocument, Job
    from app.state import db
    client, vid, original, reviewed = saved
    seen = []
    async def complete(messages, **_kw):
        seen.append(messages)
        return GOOD
    _no_effects(monkeypatch, complete)
    with db()() as session:
        before_jobs = list(session.scalars(select(Job.id)))
    result = client.post(f'/ventures/{vid}/chat', json={'message': REQUEST})
    assert result.status_code == 200, result.text
    assert not any(action.get('confirmation') for action in result.json()['actions'])
    assert GOOD in result.json()['body_md'] and 'Nothing was sent or searched' in result.json()['body_md']
    assert len(seen) == 1 and REQUEST in seen[0][1]['content'] and OLD in seen[0][1]['content']
    from app.chat.store import list_messages
    assert len([message for message in list_messages(vid, limit=100) if GOOD in message['body_md']]) == 1
    work_links = [action for action in result.json()['actions'] if action.get('action') == 'status']
    assert len(work_links) == 1 and work_links[0]['label'] == 'Show status here'
    with db()() as session:
        assert session.get(VentureDocument, 'task-note:'+original).body_md == OLD
        tasks = session.scalars(select(PlannedTask).where(PlannedTask.venture_id == vid, PlannedTask.title.like('Revise one saved%'))).all()
        assert len(tasks) == 1 and tasks[0].status == 'done'
        assert GOOD in session.get(VentureDocument, 'task-note:'+tasks[0].id).body_md
        assert list(session.scalars(select(Job.id))) == before_jobs


def test_http_queued_revision_registered_job_never_researches(saved, monkeypatch):
    from app.chat.email_revision import TITLE
    from app.planning.store import enqueue_task
    from app.planning.execute import run_task_job
    from app.db.models import PlannedTask, VentureDocument, Job
    from app.state import db
    client, vid, _, reviewed = saved
    async def complete(*_a, **_kw):
        return GOOD
    _no_effects(monkeypatch, complete)
    task = enqueue_task(vid, {'title': TITLE, 'detail': json.dumps({'request': REQUEST, 'source': reviewed}), 'why_now': 'reviewed saved email', 'tags': ['chat'], 'runnable': True}, source='chat')
    response = client.post(f'/ventures/{vid}/tasks/{task["id"]}/run')
    assert response.status_code == 202, response.text
    with db()() as session:
        jobs = [j for j in session.scalars(select(Job).where(Job.kind == 'task.run')) if task['id'] in j.payload_json]
        assert len(jobs) == 1
    asyncio.run(run_task_job({'venture_id': vid, 'task_id': task['id']}))
    with db()() as session:
        assert session.get(PlannedTask, task['id']).status == 'done'
        assert GOOD in session.get(VentureDocument, 'task-note:'+task['id']).body_md


@pytest.mark.parametrize('mutation', ['source_changed', 'foreign_venture', 'foreign_workspace', 'forged_hash'])
def test_stale_or_foreign_source_refuses_before_model_or_task(saved, monkeypatch, mutation):
    from app.chat.email_revision import run
    from app.db.models import PlannedTask, VentureDocument
    from app.state import db
    _, vid, original, reviewed = saved
    async def forbidden(*_a, **_kw):
        pytest.fail('invalid source must not call model')
    _no_effects(monkeypatch, forbidden)
    if mutation == 'source_changed':
        with db()() as session:
            session.get(VentureDocument, 'task-note:'+original).body_md += '\nChanged'
            session.commit()
    elif mutation == 'foreign_venture':
        with db()() as session:
            session.get(VentureDocument, 'task-note:'+original).venture_id = 'ven_foreign'
            session.commit()
    elif mutation == 'foreign_workspace':
        with db()() as session:
            session.get(PlannedTask, original).workspace_id = 'wsp_foreign'
            session.commit()
    else:
        reviewed['sha256'] = hashlib.sha256(b'forged').hexdigest()
    response = asyncio.run(run(vid, {'request': REQUEST, 'source': reviewed}))
    assert 'could not bind' in response['body_md']
    with db()() as session:
        assert not session.scalar(select(PlannedTask.id).where(PlannedTask.venture_id==vid, PlannedTask.title.like('Revise one saved%')))


@pytest.mark.parametrize('bad', [OLD, 'Search result: three hits', GOOD.replace('Sam', 'sam@other.example'), GOOD+'\nDraft 2\nSubject: Other', GOOD+'\nhttps://signup.example', GOOD.replace('Hulk Fitness', 'Another Gym'), GOOD+'\nPlease share private client chats.'])
def test_invalid_revision_cannot_be_marked_completed_or_research(saved, monkeypatch, bad):
    from app.chat.email_revision import run, TITLE
    from app.db.models import PlannedTask
    from app.state import db
    _, vid, _, reviewed = saved
    async def complete(*_a, **_kw):
        return bad
    _no_effects(monkeypatch, complete)
    response = asyncio.run(run(vid, {'request': REQUEST, 'source': reviewed}))
    assert 'did not produce' in response['body_md']
    with db()() as session:
        task = session.scalar(select(PlannedTask).where(PlannedTask.venture_id==vid, PlannedTask.title==TITLE))
        assert task.status != 'done' and 'saved_interview_revision_unverified' in task.last_error


def test_source_mutation_while_model_runs_cannot_accept_revision(saved, monkeypatch):
    from app.chat.email_revision import run, TITLE
    from app.db.models import PlannedTask, VentureDocument
    from app.state import db
    _, vid, original, reviewed = saved
    async def complete(*_a, **_kw):
        with db()() as session:
            session.get(VentureDocument, 'task-note:'+original).body_md += '\nChanged during model'
            session.commit()
        return GOOD
    _no_effects(monkeypatch, complete)
    response = asyncio.run(run(vid, {'request': REQUEST, 'source': reviewed}))
    assert 'did not produce' in response['body_md']
    with db()() as session:
        assert session.scalar(select(PlannedTask).where(PlannedTask.venture_id==vid, PlannedTask.title==TITLE)).status != 'done'


@pytest.mark.parametrize('tail', [' Then send it.', ' Search for a contact.', ' Publish it.', ' Read another workspace.', ' and then send it now.'])
def test_revision_does_not_swallow_positive_actions(tail):
    from app.chat.email_revision import target
    assert target(REQUEST + tail) is None


def test_source_changes_at_final_note_write_cannot_be_marked_completed(saved, monkeypatch):
    from app.chat.email_revision import run, TITLE
    from app.db.models import PlannedTask, VentureDocument
    from app.state import db
    from app.planning import execute
    _, vid, original, reviewed = saved
    async def complete(*_a, **_kw):
        return GOOD
    _no_effects(monkeypatch, complete)
    write = execute._write_artifact
    def drift(*args, **kwargs):
        if kwargs.get('email_revision_detail'):
            with db()() as session:
                session.get(VentureDocument, 'task-note:'+original).body_md += '\nChanged before final write'
                session.commit()
        return write(*args, **kwargs)
    monkeypatch.setattr(execute, '_write_artifact', drift)
    response = asyncio.run(run(vid, {'request': REQUEST, 'source': reviewed}))
    assert 'did not produce' in response['body_md']
    with db()() as session:
        task = session.scalar(select(PlannedTask).where(PlannedTask.venture_id==vid, PlannedTask.title==TITLE))
        assert task.status != 'done'
        assert session.get(VentureDocument, 'task-note:'+task.id) is None


def test_authenticated_requested_revision_executes_without_a_confirmation_card(saved, monkeypatch):
    from app.chat.execute import execute_action
    from app.db.models import PlannedTask
    from app.state import db
    from app.db.models import Venture, Workspace
    _, vid, _, reviewed = saved
    calls = []
    async def complete(*args, **_kw):
        calls.append(args)
        return GOOD
    _no_effects(monkeypatch, complete)
    with db()() as session:
        actor = session.get(Workspace, session.get(Venture, vid).workspace_id).owner_id
    reply = asyncio.run(execute_action(vid, 'outreach.draft', payload={
        'run': True, 'email_revision': {'request': REQUEST, 'source': reviewed}}, actor_subject=actor))
    assert reply['kind'] != 'confirmation' and not any(action.get('confirmation') for action in reply['actions'])
    assert GOOD in reply['body_md'] and 'Nothing was sent or searched' in reply['body_md']
    assert len(calls) == 1
    with db()() as session:
        tasks = session.scalars(select(PlannedTask).where(PlannedTask.venture_id==vid, PlannedTask.title.like('Revise one saved%'))).all()
        assert len(tasks) == 1 and tasks[0].status == 'done'


@pytest.mark.parametrize('run_flag', [False, None, 'true'])
def test_unauthenticated_revision_payload_only_prepares_never_executes(saved, monkeypatch, run_flag):
    from app.chat.execute import execute_action
    from app.db.models import PlannedTask
    from app.state import db

    _, vid, _, reviewed = saved
    async def forbidden(*args, **kwargs):
        pytest.fail('Unauthenticated preparation cannot invoke writing')
    _no_effects(monkeypatch, forbidden)
    reply = asyncio.run(execute_action(vid, 'outreach.draft', payload={
        'run': run_flag, 'email_revision': {'request': REQUEST, 'source': reviewed}}))
    assert reply['kind'] == 'confirmation'
    with db()() as session:
        assert not session.scalar(select(PlannedTask.id).where(PlannedTask.venture_id==vid, PlannedTask.title.like('Revise one saved%')))
