"""Real local database reads; no provider or executor is called."""
import json
from datetime import timedelta

import pytest


def seed(ops_env):
    from app.db.models import Job, OpsTask, Venture
    from app.state import db
    from core.clock import utcnow
    now = utcnow()
    wid = ops_env['workspace_id']
    with db()() as session:
        for vid, workspace in [('a', wid), ('b', wid), ('foreign', 'other')]:
            session.add(Venture(id=vid, workspace_id=workspace, name=vid, raw_idea='test', stage='IDEATING', created_at=now, updated_at=now))
        for i in range(8):
            session.add(Job(id=f'job{i}', workspace_id=wid, venture_id='a', kind='plan.compile', status='succeeded', payload_json='{"secret":"DO_NOT_EXPOSE"}', created_at=now))
        for ident, vid, workspace, kind, status, created in [
            ('failed', 'a', wid, 'research.deep', 'failed_dead', now),
            ('active', 'a', wid, 'build.run', 'leased', now),
            ('old', 'a', wid, 'idea.validate', 'succeeded', now-timedelta(days=2)),
            ('watch', 'a', wid, 'build.watch', 'leased', now),
            ('sibling', 'b', wid, 'plan.compile', 'scheduled', now),
            ('foreign', 'foreign', 'other', 'plan.compile', 'failed', now),
            ('future', 'a', wid, 'plan.compile', 'scheduled', now+timedelta(days=2)),
        ]:
            session.add(Job(id=ident, workspace_id=workspace, venture_id=vid, kind=kind, status=status, payload_json='{"revision_id":"DO_NOT_EXPOSE"}', last_error='DO_NOT_EXPOSE', created_at=created))
        session.add(OpsTask(id='legacy', workspace_id=wid, title='Old task', description='DO_NOT_EXPOSE', agent_type='finance', status='completed', metadata_json='{"venture_id":"a","secret":"DO_NOT_EXPOSE"}', error_message='DO_NOT_EXPOSE', result_summary='DO_NOT_EXPOSE', created_at=now, updated_at=now))
        session.commit()
    return wid


def test_company_scope_counts_are_not_limited_and_payloads_stay_private(ops_env):
    from app.ops.services import operations as op
    wid = seed(ops_env)
    rows = op.list_tasks(wid, venture_id='a', limit=2)
    assert len(rows) == 2
    summary = op.summary(wid, venture_id='a')
    assert summary['tasks_today_total'] == 10
    assert summary['tasks_today_completed'] == 8
    assert summary['tasks_all_time_by_status']['completed'] == 9
    assert summary['active_agents'] == ['build.run']
    assert summary['scope']['legacy_tasks_included'] is False
    assert op.summary(wid)['tasks_today_total'] == 12
    all_rows = op.list_tasks(wid, venture_id='a', limit=200)
    assert {row['id'] for row in all_rows}.isdisjoint({'watch', 'sibling', 'foreign', 'legacy'})
    assert 'DO_NOT_EXPOSE' not in json.dumps([all_rows, op.list_tasks(wid), summary])
    assert op.get_task(wid, 'active', venture_id='a')['title'] == 'Build dispatch'
    assert op.get_task(wid, 'failed', venture_id='a')['error_code'] == 'job_failed'
    assert op.get_task(wid, 'legacy', venture_id='a') is None
    assert op.get_task(wid, 'foreign') is None
    assert op.get_task(wid, 'watch') is None
    assert len(op.list_tasks(wid, venture_id='a', status='completed')) == 9
    assert [r['id'] for r in op.list_tasks(wid, venture_id='a', agent_type='build.run')] == ['active']


@pytest.mark.parametrize('method', ['list_tasks', 'summary', 'get_task'])
def test_unowned_or_missing_venture_does_not_fall_back_to_workspace(ops_env, method):
    from app.ops.services import operations as op
    wid = seed(ops_env)
    for venture in ('foreign', 'missing', ''):
        with pytest.raises(KeyError):
            getattr(op, method)(wid, *(['legacy'] if method == 'get_task' else []), venture_id=venture)


@pytest.mark.parametrize('kwargs', [{'limit': 0}, {'limit': 201}, {'status': 'invented'}])
def test_invalid_filters_are_not_silent(ops_env, kwargs):
    from app.ops.services import operations as op
    with pytest.raises(ValueError):
        op.list_tasks(ops_env['workspace_id'], **kwargs)


def test_agents_show_observed_records_without_double_counting_runs(ops_env):
    from app.ops.services import operations as op, tasks
    wid = seed(ops_env)
    tasks.create_agent_run(wid, 'finance', task_id='legacy', input_context={'private': 'DO_NOT_EXPOSE'})
    tasks.create_agent_run(wid, 'customer_support', input_context={'private': 'DO_NOT_EXPOSE'})
    company = op.agent_status(wid, venture_id='a')
    assert all(row['provenance'] == 'durable_job' for row in company)
    assert {row['agent_type'] for row in company} == {'plan.compile', 'research.deep', 'build.run', 'idea.validate'}
    workspace = op.agent_status(wid)
    finance = next(row for row in workspace if row['agent_type'] == 'finance')
    support = next(row for row in workspace if row['agent_type'] == 'customer_support')
    assert finance['tasks_total'] == 1 and finance['observed_status'] == 'in_progress'
    assert support['tasks_total'] == 0 and support['last_run_at'] is not None
    assert 'DO_NOT_EXPOSE' not in json.dumps(workspace)
    assert op.agent_status('no-records') == []
    with pytest.raises(KeyError):
        op.agent_status(wid, venture_id='foreign')


def test_every_canonical_job_state_has_a_defined_presentation(ops_env):
    from app.core.fsm import JOB_STATES
    from app.ops.services.operations import normalize
    assert all(normalize(state) != 'unknown' for state in JOB_STATES)
    assert normalize('failed_dead') == 'failed'
    assert normalize('future_unrecognized_state') == 'unknown'
