"""Concurrent occurrences of one fingerprint share one row and alert exactly once, on PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


def test_parallel_records_make_one_row_and_one_new_alert(journey_db, monkeypatch):
    d = journey_db
    from app.error_reports import service

    with d["engine"].begin() as conn:
        conn.execute(text("DELETE FROM error_reports"))
    alerts = []
    monkeypatch.setattr(service, "send_alert", lambda kind, info: alerts.append(kind))

    def hit(i):
        service.record(source="server", feature="chat", code="RuntimeError", place="/ventures/{venture_id}/chat",
                       frame="api/chat_api.py:post_chat", person_id=f"usr_{i % 3}")

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(hit, range(24)))
    with d["engine"].connect() as conn:
        rows = conn.execute(text("SELECT count, status FROM error_reports")).all()
    assert rows == [(24, "open")]
    assert alerts.count("new") == 1  # the insert race loser updates instead of alerting again
    assert alerts.count("spike") <= 1


def test_problem_report_round_trips_binary_screenshot(journey_db):
    d = journey_db
    from app.db.supymem_models import ProblemReport
    from core.clock import utcnow

    with d["factory"]() as session:
        session.add(ProblemReport(id="prb_pg1", person_id="usr_pg", message="broken", screenshot=b"\x89PNG\r\n\x1a\n\x00\xff",
                                  screenshot_type="image/png", created_at=utcnow()))
        session.commit()
        assert session.get(ProblemReport, "prb_pg1").screenshot.endswith(b"\x00\xff")
