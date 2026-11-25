"""Current readiness and locked review against migrated, isolated PostgreSQL.

The existing owned source/QA/storage fixtures perform no external effects.
"""
import json
import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from tests.integration import test_postgres_subscription_static as source
from tests.integration.test_postgres_static_publication import _hosting_environment
from tests.security.test_core_signed_boundary import SECRET, headers

pg_database = source.pg_database
journey_db = source.journey_db
planning_standard = source.planning_standard
planning = source.planning
ready_plan = source.ready_plan
source_ready = source.source_ready
source_cloud = source.source_cloud


@pytest.fixture(autouse=True)
def hosting_before_plan(monkeypatch):
    _hosting_environment(monkeypatch)


@pytest.fixture
def completed_source(source_cloud):
    d = source_cloud
    source.source_step(d)
    d['qa_state']['phase'] = 'completed'
    source.source_step(d)
    assert source.source_step(d)['state'] == 'succeeded'
    return d


def args(d, view):
    return dict(actor=d['actor'], source_artifact_id=view['artifact_id'],
                report_digest=view['review']['report_digest'], decision='accept')


@contextmanager
def no_dml_or_locks(engine):
    observed = []

    def guard(_conn, _cursor, statement, _params, _context, _many):
        sql = statement.lstrip().upper()
        assert not sql.startswith(('INSERT', 'UPDATE', 'DELETE'))
        assert 'FOR UPDATE' not in sql and 'PG_ADVISORY' not in sql
        observed.append(sql.split()[0])

    event.listen(engine, 'before_cursor_execute', guard)
    try:
        yield observed
    finally:
        event.remove(engine, 'before_cursor_execute', guard)


def test_fresh_signed_get_lists_readiness_without_writes_locks_or_provider_configuration(completed_source, monkeypatch):
    from app.builder import execution
    d = completed_source
    monkeypatch.setenv('COS_INTERNAL_ASSERTION_SECRET', SECRET)
    monkeypatch.setenv('ADMIN_REQUIRE_TOTP', '1')
    monkeypatch.setenv('ADMIN_BOOTSTRAP_EMAILS', 'operator@example.test')
    from app.main import create_app
    monkeypatch.setattr('app.build.subscription_static_policy.validate', lambda *_: pytest.fail('GET policy configuration'))
    monkeypatch.setattr('app.builder.model_proxy._locked', lambda *_: pytest.fail('GET lock'))
    client = TestClient(create_app())
    path = f"/builds/{d['build_run_id']}"
    with no_dml_or_locks(d['engine']) as observed:
        response = client.get(path, headers=headers(path, subject=d['actor']))
        assert response.status_code == 200, response.text
        view = response.json()
        assert view['review_readiness']['status'] == 'ready', view['review_readiness']
        assert execution.listing(d['app_id'], actor=d['actor'])[0]['review_readiness'] == view['review_readiness']
    assert observed and view['qa_state'] == 'passed' and view['review']['status'] == 'pending'
    assert view['publication_required'] is True and view['task_completion'] is None
    assert client.get(path, headers=headers(path, subject='foreign-reader')).status_code == 404


@pytest.mark.parametrize('fault', ['contract', 'task', 'receipt'])
def test_current_block_has_same_closed_post_reason_and_saved_evidence_is_unchanged(completed_source, fault):
    from app.builder.execution import BuildError, read, review
    from app.db.supymem_models import BuildRun, ExecutionPlan, PlanTask
    d = completed_source
    before = read(d['build_run_id'], actor=d['actor'])
    with d['factory']() as session:
        if fault == 'contract':
            plan = session.get(ExecutionPlan, d['plan_id'])
            doc = json.loads(plan.structured)
            doc['proposal']['milestones'][0]['tasks'][0]['landing_expectations']['contract']['product_name'] = 'Changed requirements'
            plan.structured = json.dumps(doc)
        elif fault == 'task':
            session.get(PlanTask, d['plan_task_id']).build_run_id = None
        else:
            run = session.get(BuildRun, d['build_run_id'])
            spec = json.loads(run.spec)
            spec['lifecycle']['completion']['source_import_digest'] = '0' * 64
            run.spec = json.dumps(spec)
        session.commit()
        frozen_spec = session.get(BuildRun, d['build_run_id']).spec
    with no_dml_or_locks(d['engine']):
        after = read(d['build_run_id'], actor=d['actor'])
    assert after['review_readiness']['status'] == 'blocked', after['review_readiness']
    assert after['qa_state'] == before['qa_state'] == 'passed'
    assert after['review'] == before['review'] and after['usage'] == before['usage']
    with pytest.raises(BuildError) as rejected:
        review(d['build_run_id'], **args(d, before))
    assert rejected.value.code == after['review_readiness']['error_code']
    assert rejected.value.reasons == [after['review_readiness']['code']]
    with d['factory']() as session:
        assert session.get(BuildRun, d['build_run_id']).spec == frozen_spec


def test_unknown_check_is_unavailable_and_refuses_without_exposing_exception(completed_source, monkeypatch):
    from app.builder.execution import BuildError, read, review
    d = completed_source
    before = read(d['build_run_id'], actor=d['actor'])

    def unknown(*_args, **_kwargs):
        raise RuntimeError('Private diagnostic must never leave the server')

    monkeypatch.setattr('app.build.subscription_static_controller.recheck', unknown)
    check = read(d['build_run_id'], actor=d['actor'])['review_readiness']
    assert check['status'] == 'check_unavailable' and check['code'] == 'build_review_check_unavailable'
    with pytest.raises(BuildError) as refused:
        review(d['build_run_id'], **args(d, before))
    assert refused.value.status == 503 and refused.value.reasons == [check['code']]
    assert 'Private' not in json.dumps(check)


def test_ready_get_is_no_authority_after_change_and_accept_repeat_retains_publication_gate(completed_source):
    from app.builder.execution import BuildError, read, review
    from app.db.supymem_models import PlanTask
    d = completed_source
    before = read(d['build_run_id'], actor=d['actor'])
    assert before['review_readiness']['status'] == 'ready'
    with pytest.raises(BuildError, match='^build_review_not_ready$'):
        review(d['build_run_id'], **{**args(d, before), 'report_digest': None})
    body = args(d, before)
    assert review(d['build_run_id'], **body) == review(d['build_run_id'], **body)
    saved = read(d['build_run_id'], actor=d['actor'])
    assert saved['review_readiness']['status'] == 'not_applicable'
    assert saved['task_completion']['state'] == 'pending_publication' and saved['publication_required'] is True
    with d['factory']() as session:
        assert session.get(PlanTask, d['plan_task_id']).state == 'waiting_for_founder'


def test_viewer_can_read_saved_qa_but_readiness_does_not_promise_manage_permission(completed_source):
    from app.authz.workspace import ensure_subject_person
    from app.builder.execution import read, review
    from app.db.models import WorkspaceMember
    from core.clock import utcnow
    d = completed_source
    viewer = 'viewer_' + uuid.uuid4().hex
    ensure_subject_person(viewer)
    with d['factory']() as session:
        session.add(WorkspaceMember(id='member_' + uuid.uuid4().hex, workspace_id=d['wid'], person_id=viewer,
                                    role='viewer', created_at=utcnow()))
        session.commit()
    view = read(d['build_run_id'], actor=viewer)
    assert view['qa_state'] == 'passed'
    assert view['review_readiness']['code'] == 'build_manage_apps_required'
    with pytest.raises(PermissionError, match='build_manage_apps_required'):
        review(d['build_run_id'], **{**args(d, view), 'actor': viewer})


@pytest.mark.parametrize('fault,expected', [
    ('identity', 'build_review_source_identity_changed'),
    ('fingerprint', 'build_review_request_fingerprint_changed'),
    ('inputs', 'build_review_task_inputs_changed'),
])
def test_exact_mismatch_surface_is_named_without_exporting_content(completed_source, fault, expected):
    from app.builder.execution import BuildError, read, review
    from app.builder.model_proxy import digest
    from app.db.supymem_models import BuildRun
    d = completed_source
    before = read(d['build_run_id'], actor=d['actor'])
    with d['factory']() as session:
        row = session.get(BuildRun, d['build_run_id'])
        spec = json.loads(row.spec)
        if fault == 'identity':
            spec['source_import']['identity']['source']['size_bytes'] += 1
            spec['lifecycle']['completion']['source_import_digest'] = digest(spec['source_import'])
        elif fault == 'fingerprint':
            row.request_digest = '0' * 64
        else:
            spec['inputs']['product_context']['name'] = 'Different saved input'
        row.spec = json.dumps(spec)
        session.commit()
    check = read(d['build_run_id'], actor=d['actor'])['review_readiness']
    assert check['code'] == expected and check['status'] == 'blocked'
    assert set(check) == {'schema_version', 'status', 'code', 'error_code'}
    with pytest.raises(BuildError) as blocked:
        review(d['build_run_id'], **args(d, before))
    assert blocked.value.reasons == [expected]


def test_failed_current_dependency_read_is_unknown_not_stale_authority(completed_source, monkeypatch):
    from app.builder.execution import BuildError, read, review
    d = completed_source
    before = read(d['build_run_id'], actor=d['actor'])

    def unavailable(*_args, **_kwargs):
        raise ValueError('Private dependency read failure')

    monkeypatch.setattr('app.planning.execution.inputs', unavailable)
    assert read(d['build_run_id'], actor=d['actor'])['review_readiness']['status'] == 'check_unavailable'
    with pytest.raises(BuildError) as blocked:
        review(d['build_run_id'], **args(d, before))
    assert blocked.value.status == 503 and blocked.value.reasons == ['build_review_check_unavailable']
