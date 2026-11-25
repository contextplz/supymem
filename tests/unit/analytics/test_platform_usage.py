"""The generated console control label passes the real authenticated ingest to storage."""

from uuid import uuid4
from unittest.mock import MagicMock

import pytest
from core.assertion import sign_assertion

pytestmark = pytest.mark.hosted_only


def test_control_ingest_is_durable_idempotent_and_ranked(tmp_path, monkeypatch):
    from tests.unit.admin.test_catalog_live import SECRET, _client, _signed

    with _client(tmp_path, monkeypatch) as client:
        from app.analytics.feature_catalog import CONTROL_SLUG
        from app.authz.workspace import ensure_subject_person
        from app.db.models import AnalyticsEvent
        from app.state import db
        from sqlalchemy import select

        ensure_subject_person("click-founder")
        body = {"event": "ui.control_activate", "event_id": str(uuid4()), "context_subject": "click-founder",
                "props": {"feature": "website", "control_id": "website_publish_confirm", "platform": "web", "session_id": str(uuid4())}}
        def post():
            token = sign_assertion(SECRET, sub="click-founder", email="founder@example.test", role="user",
                                   path="/telemetry", method="POST")
            return client.post("/telemetry", json=body, headers={"X-COS-Assertion": token})

        response = post()
        assert response.status_code == 200, response.text
        assert post().json() == response.json()
        with db()() as session:
            rows = session.scalars(select(AnalyticsEvent).where(AnalyticsEvent.person_id == "click-founder")).all()
            assert len(rows) == 1 and rows[0].event_name == "ui.control_activate"
        ranking = _signed(client, "GET", "/admin/platform").json()["features"]["7d"]
        assert ranking[0]["feature"] == CONTROL_SLUG["website_publish_confirm"] == "publish"
        assert ranking[0]["count"] == ranking[0]["users"] == 1
        body["event_id"] = str(uuid4())
        body["props"]["feature"] = "research"
        assert post().status_code == 422


def test_platform_sql_compiles_for_postgresql(tmp_path, monkeypatch):
    from tests.unit.admin.test_catalog_live import _client

    with _client(tmp_path, monkeypatch):
        from app.analytics import platform
        from sqlalchemy.dialects.postgresql import dialect

        monkeypatch.setenv("SUPYMEM_ANALYTICS_INTERNAL_EMAIL_DOMAINS", "staff.test")
        session = MagicMock()
        session.__enter__.return_value = session
        session.get_bind.return_value.dialect = dialect()
        session.scalars.return_value = []
        session.scalar.return_value = 0
        queries = []

        def execute(statement):
            queries.append(str(statement.compile(dialect=dialect())))
            result = MagicMock()
            result.one.return_value = (0,) * len(statement.selected_columns)
            return result

        session.execute.side_effect = execute
        monkeypatch.setattr(platform, "db", lambda: lambda: session)
        assert platform.platform_metrics()["credits"]["consumed"]["all"] == 0
        sql = "\n".join(queries)
        assert "->>" in sql and "CAST(analytics_events.props_json AS JSON)" in sql
        assert "json_extract" not in sql and "instr(" not in sql
