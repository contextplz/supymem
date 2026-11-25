"""Canonical/legacy Operations reads against the fully migrated PostgreSQL schema."""
from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


def test_scoped_operations_counts_and_redaction(journey_db):
    from app.workspaces.service import create_workspace
    from tests.unit.ops.test_operations_read_model import (
        test_company_scope_counts_are_not_limited_and_payloads_stay_private as verify,
    )
    workspace = create_workspace(kind='company', name='Operations acceptance', owner_id=journey_db['actor'])
    verify({'workspace_id': workspace.id})
    from app.ops.services import operations as op, tasks
    tasks.create_agent_run(workspace.id, 'finance', task_id='legacy', input_context={'private': 'DO_NOT_EXPOSE'})
    statuses = op.agent_status(workspace.id)
    finance = next(row for row in statuses if row['agent_type'] == 'finance')
    assert finance['tasks_total'] == 1 and finance['observed_status'] == 'in_progress'
    company = op.agent_status(workspace.id, venture_id='a')
    assert len(company) == 4 and all(row['provenance'] == 'durable_job' for row in company)
    import json
    assert 'DO_NOT_EXPOSE' not in json.dumps(statuses)


def test_signed_operations_reads_enforce_founder_workspace_and_company(journey_db, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Job, Venture, WorkspaceMember
    from app.workspaces.service import create_workspace
    from core.assertion import sign_assertion
    from core.clock import utcnow
    from core.ids import new_id
    d = journey_db
    secret = 'operations-signed-test-only-secret-over-32-bytes'
    monkeypatch.setenv('COS_INTERNAL_ASSERTION_SECRET', secret)
    monkeypatch.setenv('COMPANY_OS_VAULT_SECRET', 'operations-test-only-vault-secret-over-32-bytes')
    monkeypatch.setenv('ADMIN_REQUIRE_TOTP', '1')
    monkeypatch.setenv('ADMIN_BOOTSTRAP_EMAILS', 'operations@example.test')
    monkeypatch.setenv('LLM_CREDITS_REQUIRE_GATE', '1')
    from app.main import create_app
    foreign, viewer = new_id('usr'), new_id('usr')
    ensure_subject_person(foreign)
    ensure_subject_person(viewer)
    other = create_workspace(kind='company', name='Foreign Operations', owner_id=foreign)
    foreign_vid, sibling = new_id('ven'), new_id('ven')
    now = utcnow()
    with d['factory']() as session:
        for vid, wid in [(foreign_vid, other.id), (sibling, d['wid'])]:
            session.add(Venture(id=vid, workspace_id=wid, name='Scoped fixture', raw_idea='test', stage='draft', created_at=now, updated_at=now))
        session.add(WorkspaceMember(id=new_id('wsm'), workspace_id=d['wid'], person_id=viewer, role='viewer', created_at=now))
        row = session.get(Job, d['job_id'])
        row.last_error = 'DO_NOT_EXPOSE'
        row.payload_json = json.dumps({'secret': 'DO_NOT_EXPOSE'})
        session.commit()

    def signed(path, subject, method='GET'):
        return {'X-COS-Assertion': sign_assertion(secret, sub=subject, email='', role='user', path=path, method=method)}

    with TestClient(create_app()) as client:
        for path in ['/api/v1/tasks', f"/api/v1/tasks/{d['job_id']}", '/api/v1/dashboard/summary', '/api/v1/agents/status']:
            params = {'workspace_id': d['wid'], 'venture_id': d['vid']}
            for subject in [d['actor'], viewer]:
                response = client.get(path, params=params, headers=signed(path, subject))
                assert response.status_code == 200, response.text
                assert 'DO_NOT_EXPOSE' not in response.text
            assert client.get(path, params=params, headers=signed(path, foreign)).status_code == 404
            assert client.get(path, params={**params, 'venture_id': foreign_vid}, headers=signed(path, d['actor'])).status_code == 404
            assert client.get(path, params=params, headers={'X-Subject': d['actor']}).status_code == 401
            conflicting = {**signed(path, d['actor']), 'X-Workspace-Id': other.id}
            assert client.get(path, params=params, headers=conflicting).status_code == 404
        path = f"/api/v1/tasks/{d['job_id']}"
        assert client.get(path, params={'workspace_id': d['wid'], 'venture_id': sibling}, headers=signed(path, d['actor'])).status_code == 404
        path = '/api/v1/tasks'
        query = {'workspace_id': d['wid'], 'venture_id': sibling}
        assert client.get(path, params=query, headers=signed(path, viewer)).json() == []
        assert client.get(path, headers=signed(path, viewer)).status_code == 400
        query['venture_id'] = d['vid']
        assertion = signed(path, viewer)
        assert client.get(path, params=query, headers=assertion).status_code == 200
        assert client.get(path, params=query, headers=assertion).status_code == 401
        assert client.post(path, params={'workspace_id': d['wid']}, json={'title': 'Must not execute', 'agent_type': 'finance'}, headers=signed(path, viewer, 'POST')).status_code == 403
