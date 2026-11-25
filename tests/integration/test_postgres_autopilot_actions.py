"""Migrated PG proves God mode reaches real durable action boundaries, without providers."""
import asyncio
import json

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


@pytest.fixture()
def actionable(journey_db):
    from app.autopilot import service
    from app.db.models import Job
    from app.tenancy import bind
    from core.clock import utcnow
    d = journey_db
    # This fixture's unrelated plan worker is not part of the research journey.
    with d['factory']() as session:
        job = session.get(Job, d['job_id'])
        job.status, job.finished_at = 'succeeded', utcnow()
        session.commit()
    with bind(workspace_id=d['wid'], venture_id=d['vid'], actor=d['actor']):
        service.update_settings(d['vid'], god_mode=True, daily_cap_credits=200,
                                allow_send=False, send_max_per_day=0)
    yield d


def _scoped_next_step(d):
    from app.tenancy import bind
    from app.ventures.next_step import next_step

    with bind(workspace_id=d['wid'], venture_id=d['vid'], actor=d['actor']):
        return next_step(d['vid'])


def test_unscoped_next_step_does_not_advertise_an_executable_action(actionable):
    from app.db.models import Job
    from app.tenancy import current_or_none
    from app.ventures.next_step import next_step

    d = actionable
    assert current_or_none() is None
    unscoped = next_step(d['vid'])
    assert unscoped['key'] == 'venture.review' and unscoped['chat_action'] is None
    assert _scoped_next_step(d)['chat_action'] == 'research.start'
    assert current_or_none() is None
    with d['factory']() as session:
        assert not session.scalar(select(Job.id).where(Job.venture_id == d['vid'], Job.kind == 'research.deep'))


def test_real_next_step_queues_one_research_job_and_retains_wait_state(actionable):
    from app.autopilot import service
    from app.db.models import Job
    from app.db.supymem_models import AutopilotEvent
    d = actionable
    service.update_settings(d['vid'], daily_cap_credits=0)
    assert _scoped_next_step(d)['chat_action'] == 'research.start'
    result = asyncio.run(service.tick(d['vid']))
    assert result['done'] == []
    assert result['status'] == 'queued'
    assert _scoped_next_step(d)['key'] == 'research.running'
    assert asyncio.run(service.tick(d['vid']))['done'] == []
    with d['factory']() as session:
        jobs = session.scalars(select(Job).where(Job.venture_id == d['vid'], Job.kind == 'research.deep')).all()
        assert len(jobs) == 1 and jobs[0].status == 'scheduled'
        payload = json.loads(jobs[0].payload_json)
        assert (jobs[0].workspace_id, payload['revision_id'], payload['session_id']) == (d['wid'], d['rid'], d['sid'])
        events = session.scalars(select(AutopilotEvent).where(AutopilotEvent.venture_id == d['vid'],
            AutopilotEvent.action == 'research.start')).all()
        assert len(events) == 2
        attempt = next(e for e in events if e.outcome == 'attempt')
        terminal = next(e for e in events if e.id == 'apv_result_' + attempt.id)
        assert terminal.outcome == 'queued' and terminal.credits_micro == 0
        assert not service._unresolved_admission(d['vid'], 'research.start')
    settings = service.public_settings(d['vid'])
    assert settings['allow_send'] is False and settings['send_max_per_day'] == 0
    # Admission is real; queued research is not completed research or adoption.


@pytest.mark.parametrize('stop', ['off', 'paused'])
def test_real_actionable_journey_honors_stop_without_queuing(actionable, stop):
    from app.autopilot import service
    from app.db.models import Job
    from app.ventures import lifecycle
    d = actionable
    assert _scoped_next_step(d)['chat_action'] == 'research.start'
    if stop == 'off':
        service.update_settings(d['vid'], god_mode=False)
    else:
        lifecycle.pause(d['vid'], actor_id=d['actor'])
    result = asyncio.run(service.tick(d['vid']))
    assert result['done'] == []
    assert result['status'] == {'off': 'god_mode_off', 'paused': 'paused'}[stop]
    with d['factory']() as session:
        assert not session.scalar(select(Job.id).where(Job.venture_id == d['vid'], Job.kind == 'research.deep'))


def test_concurrent_real_ticks_do_not_count_one_durable_admission_twice(actionable):
    from app.autopilot import service
    from app.db.models import Job
    from app.db.supymem_models import AutopilotEvent
    from app.tenancy import bind
    from tests.integration.test_postgres_auth_security import concurrent
    d = actionable
    def tick():
        with bind(workspace_id=d['wid'], venture_id=d['vid'], actor=d['actor']):
            return asyncio.run(service.tick(d['vid']))
    concurrent(tick, count=4)
    with d['factory']() as session:
        jobs = session.scalars(select(Job).where(Job.venture_id == d['vid'], Job.kind == 'research.deep')).all()
        assert len(jobs) == 1
        events = session.scalars(select(AutopilotEvent).where(AutopilotEvent.venture_id == d['vid'],
            AutopilotEvent.action == 'research.start', AutopilotEvent.outcome == 'queued')).all()
        assert len(events) == 1


def test_venture_fence_leaves_kill_switch_writable_and_releases_after_failure(actionable, monkeypatch):
    from app.autopilot import service
    d = actionable
    with service._tick_fence(d['vid']) as first:
        assert first
        with service._tick_fence(d['vid']) as duplicate:
            assert not duplicate
        with service._tick_fence('another-isolated-venture') as independent:
            assert independent
        service.update_settings(d['vid'], god_mode=False)
        assert service._stopped(d['vid']) == 'god_mode_off'
    async def interrupted(_vid):
        raise RuntimeError('isolated-worker-failure')
    monkeypatch.setattr(service, '_tick', interrupted)
    with pytest.raises(RuntimeError, match='isolated-worker-failure'):
        asyncio.run(service.tick(d['vid']))
    with service._tick_fence(d['vid']) as recovered:
        assert recovered


def test_scheduler_without_http_scope_admits_worker_authorized_actor(actionable):
    from app.autopilot import service
    from app.core.jobs import lease
    from app.tenancy import current_or_none
    from app.worker.main import _job_scope
    d = actionable
    assert current_or_none() is None
    result = asyncio.run(service.tick(d['vid']))
    assert result['done'] == []
    assert result['status'] == 'queued'
    assert current_or_none() is None
    with d['factory']() as session:
        jobs = [job for job in lease(session, 'actor-boundary-test', kinds=['research.deep'], n=100)
                if job.venture_id == d['vid']]
        assert len(jobs) == 1
        scope = _job_scope(jobs[0])
        assert (scope.workspace_id, scope.venture_id, scope.actor) == (d['wid'], d['vid'], d['actor'])
        session.rollback()


@pytest.mark.parametrize('bad_scope', ['workspace', 'venture', 'actor'])
def test_scheduler_never_replaces_conflicting_tenant_or_actor(actionable, bad_scope):
    from app.autopilot import service
    from app.db.models import Job
    from app.tenancy import bind, current_or_none
    d = actionable
    values = {'workspace_id': d['wid'], 'venture_id': d['vid'], 'actor': d['actor']}
    values[{'workspace': 'workspace_id', 'venture': 'venture_id', 'actor': 'actor'}[bad_scope]] = 'other-account'
    with bind(**values):
        before = current_or_none()
        with pytest.raises(PermissionError, match='autopilot_scope_mismatch'):
            asyncio.run(service.tick(d['vid']))
        assert current_or_none() == before
    assert current_or_none() is None
    with d['factory']() as session:
        assert not session.scalar(select(Job.id).where(Job.venture_id == d['vid'], Job.kind == 'research.deep'))


@pytest.mark.parametrize('invalid', ['viewer', 'missing_membership', 'blocked', 'archived'])
def test_invalid_persisted_founder_authority_cannot_enqueue_on_repeated_ticks(actionable, invalid):
    from fastapi import HTTPException
    from app.autopilot import service
    from app.db.models import Job, Workspace, WorkspaceMember
    from app.tenancy import current_or_none
    d = actionable
    with d['factory']() as session:
        member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == d['wid'],
                                                            WorkspaceMember.person_id == d['actor']))
        workspace = session.get(Workspace, d['wid'])
        if invalid == 'viewer':
            member.role = 'viewer'
        elif invalid == 'missing_membership':
            session.delete(member)
        elif invalid == 'blocked':
            workspace.settings_json = '{"blocked":true}'
        else:
            from core.clock import utcnow
            workspace.archived_at = utcnow()
        session.commit()
    for _ in range(2):
        with pytest.raises((PermissionError, HTTPException)):
            asyncio.run(service.tick(d['vid']))
        assert current_or_none() is None
    with d['factory']() as session:
        assert not session.scalar(select(Job.id).where(Job.venture_id == d['vid'], Job.kind == 'research.deep'))


def test_revalidation_blocks_followup_after_founder_role_revoked(actionable, monkeypatch):
    from app.autopilot import service
    from app.chat import execute
    from app.db.models import WorkspaceMember
    from app.tenancy import current_or_none
    d, calls = actionable, []
    async def first(vid, name, **kwargs):
        calls.append(name)
        assert current_or_none().actor == d['actor']
        with d['factory']() as session:
            row = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == d['wid'],
                                                               WorkspaceMember.person_id == d['actor']))
            row.role = 'viewer'
            session.commit()
        return {'kind': 'text', 'body_md': 'queued', 'actions': [{'action': 'plan.generate'}]}
    monkeypatch.setattr(execute, 'execute_action', first)
    with pytest.raises(PermissionError, match='autopilot_authority_revoked'):
        asyncio.run(service._run_step(d['vid'], {'chat_action': 'research.start', 'title': 'Research'}, d['actor'], []))
    assert calls == ['research.start']
    assert current_or_none() is None


def test_model_actor_is_ignored_and_context_resets_after_exception(actionable, monkeypatch):
    from app.autopilot import service
    from app.chat import execute
    from app.core.jobs import enqueue, lease
    from app.tenancy import current_or_none
    from app.worker.main import _job_scope
    d = actionable
    async def enqueue_real(vid, name, *, payload, actor_subject):
        assert '_actor_subject' not in payload
        assert actor_subject == d['actor']
        with d['factory']() as session:
            enqueue(session, 'research.deep', {'_actor_subject': 'model-forged-actor'}, workspace_id=d['wid'], venture_id=vid)
            session.commit()
        raise RuntimeError('deliberate-action-failure')
    async def fail_recovery(*args, **kwargs):
        assert kwargs['terminal_id'].startswith('apv_result_')
        assert current_or_none().actor == d['actor']
        raise RuntimeError('deliberate-recovery-failure')
    monkeypatch.setattr(execute, 'execute_action', enqueue_real)
    monkeypatch.setattr(service, '_failed', fail_recovery)
    with pytest.raises(RuntimeError, match='deliberate-recovery-failure'):
        asyncio.run(service._execute(d['vid'], 'research.start', {'_actor_subject':'model-forged-actor'}, '', d['actor'], []))
    assert current_or_none() is None
    with d['factory']() as session:
        jobs = [j for j in lease(session, 'actor-proposal-test', kinds=['research.deep'], n=100) if j.venture_id == d['vid']]
        assert len(jobs) == 1
        assert _job_scope(jobs[0]).actor == d['actor']
        session.rollback()


def test_driver_and_failure_projection_receive_same_authoritative_scope(actionable, monkeypatch):
    from app.autopilot import service, lifecycle
    from app.chat import execute, recovery
    from app.tenancy import current_or_none
    d, observed = actionable, []
    def check(tag):
        scope = current_or_none()
        assert (scope.workspace_id, scope.venture_id, scope.actor) == (d['wid'], d['vid'], d['actor'])
        observed.append(tag)
    async def driver(*args):
        check('driver')
        return {'kind': 'text', 'body_md': 'driver complete'}
    monkeypatch.setattr(lifecycle, 'run_driver', driver)
    asyncio.run(service._execute(d['vid'], 'idea.advance', {}, '', d['actor'], []))
    async def alternate(*args):
        pytest.fail('Unconfirmed effects cannot invoke an automatic alternative')
    async def action(vid, name, *, payload, actor_subject):
        check('alternative')
        assert actor_subject == d['actor'] and '_actor_subject' not in payload
        return {'kind': 'text'}
    monkeypatch.setattr(recovery, 'post_failure', lambda *a, **k: check('failure_projection'))
    monkeypatch.setattr(recovery, 'alternative', alternate)
    monkeypatch.setattr(execute, 'execute_action', action)
    asyncio.run(service._failed(d['vid'], 'research.start', RuntimeError('synthetic failure'), d['actor']))
    assert observed == ['driver', 'failure_projection']
    assert current_or_none() is None


def test_scoped_worker_wakeup_never_schedules_other_ventures(actionable, monkeypatch):
    from app.autopilot import service
    from app.tenancy import bind, current_or_none
    from app.worker.handlers import _autopilot_tick
    d, seen = actionable, []
    async def one(vid):
        assert vid == d['vid'] and current_or_none().actor == d['actor']
        seen.append(vid)
    async def all_ventures():
        pytest.fail('scoped wakeup must not become a global scheduler')
    monkeypatch.setattr(service, 'tick', one)
    monkeypatch.setattr(service, 'tick_all', all_ventures)
    with bind(workspace_id=d['wid'], venture_id=d['vid'], actor=d['actor']):
        asyncio.run(_autopilot_tick({'venture_id': 'model-forged-other'}))
    assert seen == [d['vid']]
    assert current_or_none() is None


def test_global_worker_scheduler_context_binds_founder_and_restores_system(actionable):
    from app.autopilot import service
    from app.tenancy import Scope, bind, current_or_none
    d = actionable
    with bind(workspace_id='none', venture_id='', actor='system'):
        result = asyncio.run(service.tick(d['vid']))
        assert result['done'] == []
        assert result['status'] == 'queued'
        assert current_or_none() == Scope('none', '', 'system')
    assert current_or_none() is None



def test_driver_thread_propagates_actor_to_real_job_admission(actionable, monkeypatch):
    from app.autopilot import lifecycle
    from app.core.jobs import enqueue, lease
    from app.tenancy import current_or_none
    from app.worker.main import _job_scope
    d = actionable
    def threaded(vid, actor):
        assert actor == d['actor'] and current_or_none().actor == actor
        with d['factory']() as session:
            enqueue(session, 'research.deep', {}, workspace_id=d['wid'], venture_id=vid)
            session.commit()
        return {'kind': 'text', 'body_md': 'queued'}
    monkeypatch.setattr(lifecycle, 'advance_idea', threaded)
    asyncio.run(lifecycle.run_driver(d['vid'], 'idea.advance', {}, None))
    assert current_or_none() is None
    with d['factory']() as session:
        jobs = [j for j in lease(session, 'actor-thread-test', kinds=['research.deep'], n=100) if j.venture_id == d['vid']]
        assert len(jobs) == 1 and _job_scope(jobs[0]).actor == d['actor']
        session.rollback()


def test_off_mode_reply_poll_gets_its_own_founder_scope(actionable, monkeypatch):
    from sqlalchemy import update
    from app.autopilot import service, outreach
    from app.db.models import VentureDocument
    from app.db.supymem_models import AutopilotSetting
    from app.tenancy import Scope, bind, current_or_none
    from core.clock import utcnow
    from core.ids import new_id
    d, seen = actionable, []
    with d['factory']() as session:
        session.execute(update(AutopilotSetting).values(god_mode=False))
        session.add(VentureDocument(id=new_id('doc'), venture_id=d['vid'], kind=outreach.KIND,
                                   title='Synthetic scheduler test', body_md='', created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    async def poll(vid):
        assert vid == d['vid']
        scope = current_or_none()
        assert (scope.workspace_id, scope.venture_id, scope.actor) == (d['wid'], vid, d['actor'])
        seen.append(vid)
        return 0
    monkeypatch.setattr(outreach, 'poll_replies', poll)
    with bind(workspace_id='none', venture_id='', actor='system'):
        assert asyncio.run(service.tick_all()) == 0
        assert current_or_none() == Scope('none', '', 'system')
    assert seen == [d['vid']]
    assert current_or_none() is None


def test_reviewed_cards_share_pg_admission_fence_and_nested_pmf_reuses_owner(actionable, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from app.autopilot import actions, receipts, service
    from app.chat import execute
    from app.core.jobs import enqueue
    from app.db.models import Job
    from app.db.supymem_models import AutopilotEvent
    d = actionable
    entered, release = threading.Event(), threading.Event()
    calls, admitted_ids = [], []
    async def admitted(vid, name, **kwargs):
        calls.append(name)
        entered.set()
        assert await asyncio.to_thread(release.wait, 5)
        with d['factory']() as session:
            identifier = enqueue(session, 'plan.compile', {}, workspace_id=d['wid'], venture_id=vid)
            session.commit()
        admitted_ids.append(identifier)
        receipts.queued_job(identifier)
        return {'kind': 'text', 'body_md': 'Queued', 'actions': []}
    monkeypatch.setattr(execute, 'execute_action', admitted)
    proposal = {'kind': 'plan.generate', 'payload': {}, 'title': 'Owned test'}
    def card():
        return asyncio.run(actions.execute_proposed_action(d['vid'], proposal, d['actor']))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(card)
        assert entered.wait(5)
        try:
            assert pool.submit(card).result(5)['reason'] == 'already_running'
            assert asyncio.run(service.pmf_iterate(d['vid'], d['actor'], True))['kind'] == 'wait'
        finally:
            release.set()
        assert first.result(5)['reason'] == 'queued'
    assert calls == ['plan.generate']
    with d['factory']() as session:
        jobs = session.scalars(select(Job).where(Job.venture_id == d['vid'], Job.kind == 'plan.compile')).all()
        assert len(jobs) == 2  # existing fixture job plus one new admitted job
        assert json.loads(session.get(Job, admitted_ids[0]).payload_json)['_actor_subject'] == d['actor']
        events = session.scalars(select(AutopilotEvent).where(AutopilotEvent.venture_id == d['vid'],
            AutopilotEvent.action == 'plan.generate')).all()
        assert len(events) == 2
        attempt = next(e for e in events if e.outcome == 'attempt')
        assert next(e for e in events if e.id == 'apv_result_' + attempt.id).outcome == 'queued'
    assert not service._unresolved_admission(d['vid'], 'plan.generate')
    # Actual public nesting uses the same verified scope and owning task.
    async def nested_pmf(vid, actor, human):
        reply, stop = await service._execute(vid, 'plan.generate', {}, 'Nested owned action', actor, [], True)
        assert stop == 'queued' and reply['kind'] == 'text'
        return reply
    async def nested_tick(vid):
        return await service.pmf_iterate(vid, d['actor'], True)
    monkeypatch.setattr(service, '_pmf_iterate_scoped', nested_pmf)
    monkeypatch.setattr(service, '_tick', nested_tick)
    assert asyncio.run(service.tick(d['vid']))['kind'] == 'text'


def test_pg_lease_rejects_inherited_tasks_threads_and_stale_context(actionable):
    from app.autopilot import service
    d = actionable
    async def trial():
        delayed = asyncio.Event()
        async def inherited():
            await delayed.wait()
            with service._admission_fence(d['vid'], d['actor']) as acquired:
                return acquired
        def thread():
            with service._admission_fence(d['vid'], d['actor']) as acquired:
                return acquired
        with service._admission_fence(d['vid'], d['actor']) as acquired:
            assert acquired
            assert not await asyncio.to_thread(thread)
            delayed.set()
            assert not await asyncio.create_task(inherited())
            delayed.clear()
            stale = asyncio.create_task(inherited())
        # A new lease must be acquired; the inherited inactive lease is not authority.
        with service._tick_fence(d['vid']) as raw:
            assert raw
            delayed.set()
            assert not await stale
        with service._admission_fence(d['vid'], d['actor']) as recovered:
            assert recovered
    asyncio.run(trial())


def test_actual_company_confirmation_remains_queued_with_owned_research_receipt(actionable):
    from app.autopilot import service
    from app.db.supymem_models import IdeaSession, ValidationRun
    from app.idea.validation import METHODS
    from core.clock import utcnow
    d=actionable
    with d['factory']() as s:
        row=s.get(IdeaSession,d['sid'])
        row.state='validating'
        row.confirmed_at=row.confirmed_revision_id=row.confirmed_by_person_id=None
        for n,method in enumerate(METHODS):
            s.add(ValidationRun(id=f'confirmation_validation_{n}',session_id=d['sid'],revision_id=d['rid'],
                workspace_id=d['wid'],venture_id=d['vid'],method=method,state='complete',
                founder_decision=None,created_at=utcnow(),completed_at=utcnow()))
        s.commit()
    done=[]
    _,stop=asyncio.run(service._execute(d['vid'],'idea.advance',{},'Owned confirmation',d['actor'],done))
    with d['factory']() as s:
        row=s.get(IdeaSession,d['sid'])
        assert row.state=='confirmed' and row.confirmed_revision_id==d['rid']
        assert row.confirmed_by_person_id==d['actor']
        from app.db.models import Job
        jobs=s.scalars(select(Job).where(Job.venture_id==d['vid'],Job.kind=='research.deep')).all()
        assert len(jobs)==1 and jobs[0].workspace_id==d['wid']
        assert json.loads(jobs[0].payload_json)['_actor_subject']==d['actor']
    assert stop=='queued' and done==[]
    assert not service._unresolved_admission(d['vid'],'idea.advance')


def test_migrated_pmf_events_require_exact_venture_and_real_receipt(pg_database, monkeypatch):
    from sqlalchemy.orm import Session
    from tests.integration.test_postgres_auth_security import service_modules
    _, engine = pg_database
    with service_modules('company_os'):
        from app.autopilot import pmf
        from app.db.models import Venture, GtmContact, GtmTouch
        from core.clock import utcnow
        now = utcnow()
        with Session(engine) as session:
            for vid, ws in [('metric-a', 'metric-owned'), ('metric-b', 'metric-owned'), ('metric-foreign', 'metric-other')]:
                session.add(Venture(id=vid, workspace_id=ws, name=vid, raw_idea='Synthetic local fixture', stage='idea', created_at=now, updated_at=now))
            session.add(GtmContact(id='metric-contact', workspace_id='metric-owned', created_at=now, stage_changed_at=now))
            session.flush()
            for key, vid, ws, simulated in [('metric-legacy', None, 'metric-owned', 0), ('metric-b-reply', 'metric-b', 'metric-owned', 0), ('metric-sim', 'metric-a', 'metric-owned', 1), ('metric-foreign-reply', 'metric-a', 'metric-other', 0)]:
                session.add(GtmTouch(id=key, workspace_id=ws, venture_id=vid, contact_id='metric-contact', direction='in', simulated=simulated, sent_at=now, created_at=now))
            session.commit()
        monkeypatch.setattr(pmf, 'db', lambda: lambda: Session(engine))
        assert pmf.read_metric('metric-a', 'outbound_replies') == 0
        assert pmf.read_metric('metric-b', 'outbound_replies') == 1
        pmf.log_touch('metric-a', 'metric-owned', 'local-fixture@example.invalid', 'in', logged_by='synthetic local reporter')
        assert pmf.read_metric('metric-a', 'outbound_replies') == 1
        assert pmf.read_metric('metric-b', 'outbound_replies') == 1
        with Session(engine) as session:
            assert session.get(GtmTouch, 'metric-legacy').venture_id is None


def test_concurrent_owned_reply_receipt_is_counted_once(pg_database, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.orm import Session
    from tests.integration.test_postgres_auth_security import service_modules
    _, engine = pg_database
    with service_modules('company_os'):
        from app.autopilot import pmf
        from app.gtm.supymem import pipeline
        from app.db.models import Venture, GtmContact, GtmTouch
        from core.clock import utcnow
        now = utcnow()
        with Session(engine) as session:
            session.add(Venture(id='reply-race-venture', workspace_id='reply-race-workspace', name='Local fixture', raw_idea='No real company', stage='idea', created_at=now, updated_at=now))
            session.add(GtmContact(id='reply-race-contact', workspace_id='reply-race-workspace', created_at=now, stage_changed_at=now))
            session.flush()
            session.add(GtmTouch(id='reply-race-sent', workspace_id='reply-race-workspace', venture_id='reply-race-venture', contact_id='reply-race-contact', direction='out', simulated=0, sent_at=now, created_at=now, provider_thread_id='source-thread'))
            session.commit()
        monkeypatch.setattr(pipeline, 'db', lambda: lambda: Session(engine))
        monkeypatch.setattr(pmf, 'db', lambda: lambda: Session(engine))
        monkeypatch.setattr(pipeline.learning, 'record', lambda *a, **kw: None)
        barrier = Barrier(4)
        def ingest(_):
            barrier.wait(timeout=10)
            return pipeline.apply_reply('reply-race-contact', venture_id='reply-race-venture', provider_thread_id='source-thread', provider_message_id='source-reply')
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(ingest, range(4))).count(True) == 1
        assert pmf.read_metric('reply-race-venture', 'outbound_replies') == 1
