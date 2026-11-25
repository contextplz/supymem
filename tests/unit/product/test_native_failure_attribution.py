"""Native failure summaries preserve closed causes without private observations."""
import json
from pathlib import Path

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.mark.parametrize('code', ['desktop_cua_observation_unpinned', 'desktop_cua_transport_unavailable',
                                'cu_browser_decision_unavailable'])
def test_mac_failure_summary_retains_installed_version_cause_and_feature(tmp_path, monkeypatch, code):
    from desktop_runtime import crash_report
    monkeypatch.setenv('COMPANY_OS_APP_VERSION', '0.1.77')
    monkeypatch.setattr(crash_report, '_ensure_error_log', lambda: None)
    crash_report.save_crash_owner('usr_fixture', 'dev_fixture', root=tmp_path)
    crash_report.report_error('device_worker', 'run_mac_task', code=code, root=tmp_path)
    summaries = crash_report.pending_crash_summaries('usr_fixture', 'dev_fixture', root=tmp_path)
    assert len(summaries) == 1
    path, summary = summaries[0]
    raw = json.loads(path.read_text())
    assert raw['error_code'] == code
    assert summary['props']['app_version'] == '0.1.77'
    assert summary['props']['feature'] == 'computer_use'
    assert summary['props']['error_code'] == code
    assert summary['props']['error_source'] == 'device_worker'
    assert summary['props']['error_intent'] == 'run_mac_task'
    assert crash_report.pending_crash_summaries('usr_other', 'dev_fixture', root=tmp_path) == []
    load_company_os_app()
    from app.api.events_ingest import TelemetryBody
    TelemetryBody.model_validate(summary)
    if not (Path(__file__).resolve().parents[3] / "services/company_os/app/admin/router.py").is_file():
        return
    from app.admin.router import _ui_error_groups

    grouped = _ui_error_groups([{'props': summary['props'], 'occurred_at': '2026-10-01T20:50:45Z'}])
    assert grouped[0]['feature'] == 'computer_use'
    assert grouped[0]['error_code'] == code
    assert grouped[0]['app_versions'] == {'0.1.77': 1}


def test_unknown_or_private_failure_code_never_exits_local_queue(tmp_path, monkeypatch):
    from desktop_runtime import crash_report
    monkeypatch.setattr(crash_report, '_ensure_error_log', lambda: None)
    crash_report.save_crash_owner('usr_fixture', 'dev_fixture', root=tmp_path)
    crash_report.report_error('device_worker', 'run_mac_task', code='private_ax_text', root=tmp_path)
    path = next(crash_report.queue_dir(tmp_path).glob('crash-*.json'))
    raw = json.loads(path.read_text())
    raw.update({'stack': 'private AX secret text', 'logs': 'private observed browser content',
                'error_code': 'private_ax_text'})
    path.write_text(json.dumps(raw))
    _, summary = crash_report.pending_crash_summaries('usr_fixture', 'dev_fixture', root=tmp_path)[0]
    assert summary['props']['error_code'] == 'native_error'
    assert 'private' not in json.dumps(summary)


def test_existing_hosted_bridge_version_is_used_when_app_version_not_propagated(monkeypatch):
    from desktop_runtime.crash_report import app_version
    monkeypatch.delenv("COMPANY_OS_APP_VERSION", raising=False)
    monkeypatch.setenv("SUPYMEM_VERSION", "0.1.77")
    assert app_version() == "0.1.77"


@pytest.mark.parametrize('code', ['desktop_cua_observation_unpinned', 'desktop_cua_transport_unavailable',
                                'cu_browser_decision_unavailable'])
@pytest.mark.parametrize('field,value', [('feature', 'settings'), ('platform', 'web'),
                                       ('error_source', 'other'), ('error_intent', 'run_runtime'),
                                       ('error_text', 'Mac command_failed')])
def test_cua_failure_code_cannot_label_unrelated_diagnostic(code, field, value):
    from uuid import uuid4
    from pydantic import ValidationError
    load_company_os_app()
    from app.api.events_ingest import TelemetryBody
    props = {'feature': 'computer_use', 'platform': 'macos', 'app_version': '0.1.77',
             'session_id': str(uuid4()), 'error_code': code,
             'error_context': 'native', 'error_text': 'Mac native_failure',
             'error_source': 'device_worker', 'error_intent': 'run_mac_task'}
    props[field] = value
    with pytest.raises(ValidationError):
        TelemetryBody.model_validate({'event': 'ui.error', 'event_id': str(uuid4()), 'props': props})


@pytest.mark.parametrize('field,value', [('error_source', 'other'), ('error_intent', 'run_runtime'),
                                       ('error_label', 'Mac uncaught_exception'),
                                       ('error_code', 'private_ax_text')])
def test_browser_failure_queue_normalizes_unrelated_or_private_records(tmp_path, monkeypatch, field, value):
    from desktop_runtime import crash_report
    monkeypatch.setattr(crash_report, '_ensure_error_log', lambda: None)
    crash_report.save_crash_owner('usr_fixture', 'dev_fixture', root=tmp_path)
    crash_report.report_error('device_worker', 'run_mac_task', code='cu_browser_decision_unavailable', root=tmp_path)
    (path, _), = crash_report.pending_crash_summaries('usr_fixture', 'dev_fixture', root=tmp_path)
    raw = json.loads(path.read_text())
    raw.update({field: value, 'stack': 'private page prose', 'logs': 'private action arguments'})
    path.write_text(json.dumps(raw))
    (_, summary), = crash_report.pending_crash_summaries('usr_fixture', 'dev_fixture', root=tmp_path)
    assert summary['props']['error_code'] == 'native_error'
    assert 'private' not in json.dumps(summary)
    load_company_os_app()
    from app.api.events_ingest import TelemetryBody
    TelemetryBody.model_validate(summary)
