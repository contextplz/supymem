"""Closed native causes remain queryable through the production analytics view."""
import json
import logging
from uuid import uuid4

from sqlalchemy import text

from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_auth_security import pg_database as pg_database


def test_reviewed_mac_failure_retains_cause_and_version_in_unified_view(journey_db):
    from app.db.models import AnalyticsEvent
    from core.clock import utcnow
    event_id = 'usage_' + uuid4().hex
    with journey_db['factory']() as session:
        session.add(AnalyticsEvent(id=event_id, person_id=None, workspace_id=journey_db['wid'],
            venture_id=journey_db['vid'], event_name='ui.error', occurred_at=utcnow(),
            props_json=json.dumps({'feature': 'computer_use', 'platform': 'macos', 'app_version': '0.1.77',
                'error_code': 'desktop_cua_observation_unpinned', 'error_context': 'native',
                'error_text': 'Mac native_failure', 'error_source': 'device_worker', 'error_intent': 'run_mac_task'})))
        session.commit()
        row = session.execute(text('SELECT feature,error_code,error_source,intent,app_version FROM v_errors_unified WHERE event_id=:id'),
                              {'id': event_id}).one()
        assert tuple(row) == ('computer_use', 'desktop_cua_observation_unpinned', 'device_worker', 'run_mac_task', '0.1.77')


def test_queued_browser_failure_reaches_signed_telemetry_and_error_pipeline(journey_db, tmp_path, monkeypatch, caplog):
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from desktop_runtime import crash_report
    from app.admin.router import _ui_error_groups
    from app.db.models import AnalyticsEvent
    from app.db.supymem_models import ErrorReport
    from app.error_reports import service as reports

    d = journey_db
    code = 'cu_browser_decision_unavailable'
    secret = 'signed-browser-telemetry-test-secret-more-than-32-bytes'
    monkeypatch.setenv('COS_INTERNAL_ASSERTION_SECRET', secret)
    monkeypatch.setenv('ADMIN_REQUIRE_TOTP', '1')
    monkeypatch.setenv('ADMIN_BOOTSTRAP_EMAILS', 'ops@example.test')
    monkeypatch.setenv('COMPANY_OS_VAULT_SECRET', 'test-vault-secret-with-more-than-32-bytes')
    monkeypatch.setenv('COMPANY_OS_APP_VERSION', '0.1.84')
    from app import main
    monkeypatch.setattr(crash_report, '_ensure_error_log', lambda: None)
    alerts = []
    monkeypatch.setattr(reports, 'send_alert', lambda kind, info: alerts.append((kind, info)))
    crash_report.save_crash_owner(d['actor'], 'dev_browser_fixture', root=tmp_path)
    crash_report.report_error('device_worker', 'run_mac_task', code=code, root=tmp_path)
    (path, event), = crash_report.pending_crash_summaries(d['actor'], 'dev_browser_fixture', root=tmp_path)
    raw = json.loads(path.read_text())
    raw.update(stack='PRIVATE_FIXTURE_STACK', logs='PRIVATE_FIXTURE_PAGE_TEXT',
               title='PRIVATE_FIXTURE_TITLE', contact='private@example.test')
    path.write_text(json.dumps(raw))
    assert crash_report.pending_crash_summaries('usr_other', 'dev_browser_fixture', root=tmp_path) == []
    assert crash_report.pending_crash_summaries(d['actor'], 'dev_other', root=tmp_path) == []
    (_, event), = crash_report.pending_crash_summaries(d['actor'], 'dev_browser_fixture', root=tmp_path)
    assert event['props']['error_code'] == code
    assert 'PRIVATE_FIXTURE' not in json.dumps(event) and 'private@example.test' not in json.dumps(event)

    client = TestClient(main.create_app())

    def post(body, *, actor=d['actor']):
        token = sign_assertion(secret, sub=actor, email='', role='user',
                               path='/telemetry', method='POST', device_id='dev_browser_fixture')
        return client.post('/telemetry', headers={'X-COS-Assertion': token}, json=body)

    assert client.post('/telemetry', headers={'X-Subject': d['actor']}, json=event).status_code == 401
    assert post(event, actor='usr_other').status_code == 409
    with caplog.at_level(logging.WARNING, logger='app.error_event'):
        saved = post(event)
        assert saved.status_code == 200, saved.text
        assert post(event).json() == saved.json()
    for field, value in [('feature', 'settings'), ('platform', 'web'), ('error_source', 'other'),
                         ('error_intent', 'run_runtime'), ('error_text', 'Mac uncaught_exception'),
                         ('error_context', 'runtime'), ('error_code', 'private_fixture_code'),
                         ('error_text', 'PRIVATE_FIXTURE_PAGE_TEXT')]:
        invalid = {**event, 'event_id': str(uuid4()), 'props': {**event['props'], field: value}}
        assert post(invalid).status_code == 422, field
    assert post({**event, 'event_id': str(uuid4()),
                 'props': {**event['props'], 'prompt': 'PRIVATE_FIXTURE_PAGE_TEXT'}}).status_code == 422

    with d['factory']() as session:
        (row,) = session.query(AnalyticsEvent).filter_by(event_name='ui.error', person_id=d['actor']).all()
        assert row.id == saved.json()['id'] and row.person_id == d['actor']
        assert row.workspace_id is None and row.venture_id is None
        props = json.loads(row.props_json)
        assert props['error_code'] == code
        viewed = session.execute(text(
            'SELECT feature,error_code,error_source,intent,app_version FROM v_errors_unified WHERE event_id=:id'),
            {'id': row.id}).one()
        assert tuple(viewed) == ('computer_use', code, 'device_worker', 'run_mac_task', '0.1.84')
        (report,) = session.query(ErrorReport).filter_by(source='client', code=code).all()
        assert report.feature == 'computer_use' and report.place == 'run_mac_task'
        assert report.severity == 'error' and report.status == 'open' and report.count == 1
        assert json.loads(report.affected_json) == [d['actor']]
        grouped = _ui_error_groups([{'props': props, 'occurred_at': row.occurred_at.isoformat()}])
        assert len(grouped) == 1 and grouped[0]['error_code'] == code
    logged = [json.loads(record.getMessage().removeprefix('error_event ')) for record in caplog.records
              if record.name == 'app.error_event' and record.getMessage().startswith('error_event ')]
    logged = [body for body in logged if body['source'] == 'client']
    assert len(logged) == 1 and logged[0]['code'] == code
    assert logged[0]['source'] == 'client' and logged[0]['feature'] == 'computer_use'
    assert logged[0]['component'] == 'device_worker' and logged[0]['intent'] == 'run_mac_task'
    assert 'PRIVATE_FIXTURE' not in caplog.text and 'private@example.test' not in caplog.text
    assert len(alerts) == 1 and alerts[0][0] == 'new' and alerts[0][1]['code'] == code
