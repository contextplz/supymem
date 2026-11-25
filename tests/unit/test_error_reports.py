"""Error fingerprints, new/spike alerts, and the founder problem report."""

import base64
import json
import logging
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 20).decode()


@pytest.fixture()
def core(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/cos.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FOUNDER_ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("ADMIN_BOOTSTRAP_EMAILS", raising=False)
    main = load_company_os_app()
    from app.error_reports import service

    alerts = []
    monkeypatch.setattr(service, "send_alert", lambda kind, info: alerts.append(kind))
    return main, service, alerts


def _rows():
    from app.db.supymem_models import ErrorReport
    from app.state import db

    with db()() as s:
        return s.query(ErrorReport).all()


def test_same_fingerprint_counts_once_and_alerts_once(core):
    _, service, alerts = core
    for person in ("usr_a", "usr_b", "usr_a"):
        service.record(source="server", feature="chat", code="RuntimeError", place="/ventures/{venture_id}/chat",
                       frame="api/chat_api.py:post_chat", person_id=person, venture_id="ven_1")
    (row,) = _rows()
    assert row.count == 3 and json.loads(row.affected_json) == ["usr_a", "usr_b"]
    assert alerts == ["new"]
    service.record(source="server", feature="chat", code="RuntimeError", place="/ventures/{venture_id}/chat",
                   frame="api/other.py:elsewhere")
    assert len(_rows()) == 2 and alerts == ["new", "new"]  # a different top frame is a different fingerprint


def test_spike_alerts_once_per_window_and_warning_never_alerts(core):
    _, service, alerts = core
    for _ in range(12):
        service.record(source="worker", feature="idea", code="handler_failed:KeyError", place="idea.validate")
    assert alerts == ["new"]  # already alerted in this window
    from app.db.supymem_models import ErrorReport
    from app.state import db
    from core.clock import utcnow

    with db()() as s:  # the window rolled over 11 minutes ago
        row = s.query(ErrorReport).one()
        row.window_start = row.alerted_at = utcnow() - timedelta(minutes=11)
        s.commit()
    for _ in range(7):
        service.record(source="worker", feature="idea", code="handler_failed:KeyError", place="idea.validate")
    assert alerts == ["new", "spike"]
    service.record(source="server", feature="chat", code="insufficient_credits", severity="warning")
    assert alerts == ["new", "spike"]


def test_resolved_fingerprint_reopens_as_regression(core):
    _, service, alerts = core
    service.record(source="server", feature="chat", code="RuntimeError")
    (row,) = _rows()
    from app.db.supymem_models import ErrorReport
    from app.state import db

    with db()() as s:
        s.get(ErrorReport, row.id).status = "resolved"
        s.commit()
    service.record(source="server", feature="chat", code="RuntimeError")
    assert _rows()[0].status == "open" and alerts == ["new", "regression"]


def test_severity_and_top_frame():
    load_company_os_app()
    from app.error_reports.service import severity_for, top_frame

    assert severity_for("worker", "x", logging.ERROR) == "error"
    assert severity_for("server", "x", logging.WARNING) == "warning"
    assert severity_for("client", "render_error", logging.WARNING) == "error"
    assert severity_for("client", "x", logging.INFO) is None
    assert top_frame(None) == ""


def test_log_error_event_feeds_reports_without_secrets(core):
    _, _, _ = core
    from app.analytics.error_events import log_error_event

    log_error_event("server", feature="chat", code="RuntimeError", person_id="usr_a", place="/ventures/{venture_id}/chat",
                    level=logging.ERROR)
    (row,) = _rows()
    assert row.place == "/ventures/{venture_id}/chat" and json.loads(row.sample_json) == {"person": "usr_a"}


def test_alert_email_goes_to_founders_only(core, monkeypatch):
    main, service, _ = core
    from app.db.models import Person
    from app.email import service as mail
    from app.state import db
    from core.clock import utcnow

    monkeypatch.setenv("FOUNDER_ADMIN_EMAILS", "boss@example.com")
    with db()() as s:
        s.add(Person(id="usr_boss", email="boss@example.com", display_name="B", created_at=utcnow()))
        s.add(Person(id="usr_other", email="other@example.com", display_name="O", created_at=utcnow()))
        s.commit()
    sent = []
    monkeypatch.setattr(mail, "send_email", lambda pid, tpl, ctx, **kw: sent.append((pid, tpl)))
    import importlib

    importlib.reload(service)  # undo the fixture's send_alert stub
    service.send_alert("new", {"fingerprint": "f" * 16, "source": "server", "feature": "chat", "code": "X", "count": 1,
                               "window": "w"})
    assert sent == [("usr_boss", "error_alert")]


@pytest.mark.hosted_only
def test_error_alert_template_renders():
    load_company_os_app()
    from app.email import templates
    from app.email.fixtures import FIXTURES

    subject, html, text = templates.render("error_alert", FIXTURES["error_alert"])
    assert "New error" in subject and "RuntimeError" in html and "/admin/errors" in text


def test_problem_report_consent_attachments_and_limits(core):
    main, service, _ = core
    service.record(source="server", feature="chat", code="RuntimeError", person_id="usr_a")
    client = TestClient(main.create_app())
    h = {"X-Subject": "usr_a"}
    ok = client.post("/problem-reports", json={"message": "Chat froze", "page": "/v/{id}/now", "app_version": "0.1.55"}, headers=h)
    assert ok.status_code == 200 and ok.json()["attached_errors"] == 1 and ok.json()["screenshot_saved"] is False
    no_consent = client.post("/problem-reports", json={"message": "Chat froze", "screenshot_b64": PNG}, headers=h)
    assert no_consent.status_code == 422 and no_consent.json()["detail"]["error"]["code"] == "screenshot_consent_required"
    bad = client.post("/problem-reports", json={"message": "Chat froze", "screenshot_b64": base64.b64encode(b"%PDF").decode(),
                                                "screenshot_consent": True}, headers=h)
    assert bad.status_code == 422
    shot = client.post("/problem-reports", json={"message": "Chat froze", "screenshot_b64": PNG, "screenshot_consent": True}, headers=h)
    assert shot.status_code == 200 and shot.json()["screenshot_saved"] is True
    assert client.post("/problem-reports", json={"message": "no auth"}).status_code == 401
    from app.db.supymem_models import ProblemReport
    from app.state import db

    with db()() as s:
        rows = s.query(ProblemReport).order_by(ProblemReport.created_at).all()
    assert len(rows) == 2 and rows[0].screenshot is None and rows[1].screenshot_type == "image/png"
    assert json.loads(rows[0].fingerprints_json) == [_rows()[0].fingerprint]
    codes = [client.post("/problem-reports", json={"message": "again again"}, headers=h).status_code for _ in range(5)]
    assert 429 in codes
