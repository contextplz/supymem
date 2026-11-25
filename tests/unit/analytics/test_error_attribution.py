"""Server errors, 4xx receipts, job failures and chat failures carry closed attribution only."""

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def core(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/cos.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    main = load_company_os_app()
    import app.diagnostics.server_errors as capture

    capture._seen.clear()
    capture._budget[:] = [0.0, 0]
    return main, capture


def _rows(*names):
    from app.db.models import AnalyticsEvent
    from app.state import db

    with db()() as session:
        rows = session.query(AnalyticsEvent).filter(AnalyticsEvent.event_name.in_(names)).order_by(
            AnalyticsEvent.occurred_at).all()
    return [(row, json.loads(row.props_json)) for row in rows]


def _capture_error_events():
    """Collect error_event lines with a handler on the logger itself (no caplog/propagation dependency)."""
    import logging

    lines = []

    class Collect(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    logger = logging.getLogger("app.error_event")
    handler = Collect(level=logging.DEBUG)
    old_level, old_disabled = logger.level, logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.disabled = False
    _CLEANUP.append(lambda: (logger.removeHandler(handler), logger.setLevel(old_level), setattr(logger, "disabled", old_disabled)))
    return lines


_CLEANUP = []


@pytest.fixture(autouse=True)
def _restore_logger():
    yield
    while _CLEANUP:
        _CLEANUP.pop()()


def _scope(path, route, **params):
    return {"method": "POST", "path": path, "route": SimpleNamespace(path=route), "path_params": params,
            "state": {"request_id": "req_abcdefghjkmn"}}


def test_5xx_receipt_carries_venture_workspace_and_feature(core):
    _, capture = core
    capture.record_http_failure(_scope("/ventures/ven_1/waitlist", "/ventures/{venture_id}/waitlist",
                                       venture_id="ven_1", workspace_id="ws_1"),
                                status=500, failure_kind="unhandled_exception", error_class="RuntimeError")
    (row, props), = _rows("server.error")
    assert (row.venture_id, row.workspace_id) == ("ven_1", "ws_1")
    assert props["feature"] == "waitlist" and props["route"] == "/ventures/{venture_id}/waitlist"
    assert props["error_class"] == "RuntimeError" and props["request_id"] == "req_abcdefghjkmn"


def test_path_ids_must_be_well_formed(core):
    _, capture = core
    capture.record_http_failure(_scope("/x", "/ventures/{venture_id}/idea", venture_id="ven 1; drop", workspace_id=7),
                                status=500, failure_kind="http_5xx")
    (row, _), = _rows("server.error")
    assert row.venture_id is None and row.workspace_id is None


def test_request_error_recorded_for_matched_4xx_only_and_bounded(core):
    _, capture = core
    scope = _scope("/ventures/ven_1/chat/confirm", "/ventures/{venture_id}/chat/confirm", venture_id="ven_1")
    for status in (401, 200, 302, 500):  # never a request error
        capture.record_http_failure(scope, status=status, failure_kind="http_4xx")
    capture.record_http_failure({"method": "GET", "path": "/wp-login.php"}, status=404, failure_kind="http_4xx")
    assert _rows("server.request_error") == []
    capture.record_http_failure(scope, status=402, failure_kind="http_4xx", error_code="insufficient_credits")
    capture.record_http_failure(scope, status=402, failure_kind="http_4xx", error_code="insufficient_credits")  # deduped
    capture.record_http_failure(scope, status=404, failure_kind="http_4xx", error_code="Not Found!")  # bad code
    (first, p1), (second, p2) = _rows("server.request_error")
    assert p1["error_code"] == "insufficient_credits" and p1["status"] == 402 and p1["feature"] == "chat"
    assert p2["error_code"] == "none" and "error_class" not in p1
    assert first.venture_id == "ven_1" and first.id.startswith("srv_")
    assert _rows("server.error") == []  # 5xx counts stay clean


def test_request_error_volume_is_capped_per_minute(core, monkeypatch):
    _, capture = core
    monkeypatch.setattr(capture, "_MAX_PER_MINUTE", 3)
    for i in range(10):
        capture.record_http_failure(_scope(f"/a/{i}", f"/ventures/{{venture_id}}/r{i}", venture_id="ven_1"),
                                    status=422, failure_kind="http_4xx")
    assert len(_rows("server.request_error")) == 3


def test_response_error_code_reads_closed_codes_only():
    load_company_os_app()
    from app.diagnostics.server_errors import response_error_code as code

    assert code(b'{"detail":{"error":{"code":"waitlist_closed"}}}') == "waitlist_closed"
    assert code(b'{"error":{"code":"forbidden"}}') == "forbidden"
    assert code(b'{"detail":"not_found"}') == "not_found"
    assert code(b'{"detail":{"code":"invalid_input"}}') == "invalid_input"
    assert code(b'{"detail":"Some prose with a secret 123"}') is None
    assert code(b'{"detail":{"error":{"code":"Has Spaces"}}}') is None
    assert code(b"not json") is None and code(b"") is None


def test_4xx_through_the_middleware_keeps_code_and_no_request_content(core):
    main, capture = core
    app = main.create_app()

    @app.post("/probe-waitlist/{private_value}")
    def closed(private_value: str):
        raise HTTPException(409, detail={"error": {"code": "waitlist_closed", "message": private_value}})

    @app.get("/probe-unauthorized")
    def unauthorized():
        raise HTTPException(401, detail="unauthenticated")

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/probe-waitlist/secret-789?q=do-not-save", json={"password": "do-not-save"}).status_code == 409
        assert client.get("/probe-unauthorized").status_code == 401
        assert client.get("/no-such-route-anywhere").status_code == 404
    (_, props), = _rows("server.request_error")
    assert props["error_code"] == "waitlist_closed" and props["status"] == 409
    assert props["route"] == "/probe-waitlist/{private_value}" and props["method"] == "POST"
    blob = json.dumps(props)
    assert "secret-789" not in blob and "do-not-save" not in blob


def test_middleware_attributes_5xx_to_venture_from_matched_route_params(core):
    """The capture middleware itself derives venture/workspace from the matched route's path params."""
    import asyncio

    _, capture = core

    async def routed_app(scope, receive, send):
        # What Starlette's router does on a match: record the route and its params on the scope.
        scope["route"] = SimpleNamespace(path="/ventures/{venture_id}/waitlist")
        scope["path_params"] = {"venture_id": "ven_77", "workspace_id": "ws_9"}
        await send({"type": "http.response.start", "status": 500, "headers": []})
        await send({"type": "http.response.body", "body": b"boom", "more_body": False})

    async def send(message):
        pass

    asyncio.run(capture.ServerErrorCaptureMiddleware(routed_app)(
        {"type": "http", "method": "POST", "path": "/ventures/ven_77/waitlist"}, None, send))
    (row, props), = _rows("server.error")
    assert (row.venture_id, row.workspace_id) == ("ven_77", "ws_9")
    assert props["feature"] == "waitlist" and props["route"] == "/ventures/{venture_id}/waitlist"


@pytest.mark.hosted_only
def test_error_event_log_line_is_closed_json(core):
    load_company_os_app()

    from app.analytics.error_events import log_error_event

    lines = _capture_error_events()
    log_error_event("worker", feature="idea", code="handler_failed:ValueError", venture_id="ven_1",
                    person_id="usr_secret", kind="idea.validate", final=True, note="has spaces and secrets")
    line = lines[0]
    assert line.startswith("error_event ")
    body = json.loads(line.removeprefix("error_event "))
    assert body["feature"] == "idea" and body["code"] == "handler_failed:ValueError" and body["venture"] == "ven_1"
    assert body["kind"] == "idea.validate" and body["final"] is True and len(body["acct"]) == 12
    assert "usr_secret" not in line and "note" not in body and "secrets" not in line


@pytest.mark.hosted_only
def test_client_error_route_logging_is_bounded_and_drops_private_values(core):
    from app.analytics.error_events import log_error_event

    lines = _capture_error_events()
    log_error_event("client", feature="chat", code="server_error",
        error_route="/ventures/{venture_id}/charter", error_status=503,
        error_origin="native_auth_unavailable", error_occurred_at="2026-10-12T01:02:03.000Z")
    body = json.loads(lines[-1].removeprefix("error_event "))
    assert body["error_route"] == "/ventures/{venture_id}/charter"
    assert body["error_status"] == 503
    assert body["error_origin"] == "native_auth_unavailable"
    assert body["error_occurred_at"] == "2026-10-12T01:02:03.000Z"
    for route in ("/auth/session?email=private@company.com", "/" + "x" * 81):
        log_error_event("client", feature="chat", code="server_error", error_route=route)
        assert "error_route" not in json.loads(lines[-1].removeprefix("error_event "))


@pytest.mark.hosted_only
def test_job_failure_row_is_idempotent_closed_and_marks_final(core):
    load_company_os_app()
    from app.analytics.error_events import record_job_failure
    from app.state import db

    job = SimpleNamespace(id="job_abc123", kind="idea.validate", attempts=3, max_attempts=3,
                          workspace_id="ws_1", venture_id="ven_1")
    with db()() as session:
        for _ in range(2):  # a retried delivery of the same attempt does not double count
            record_job_failure(session, job, code="plan invalid: private prompt text", status="failed_dead",
                               person_id="usr_1")
        record_job_failure(session, SimpleNamespace(**{**vars(job), "attempts": 2}), code="handler_failed:KeyError",
                           status="retry_wait", person_id="usr_1")
        session.commit()
    rows = _rows("job.failed")
    assert len(rows) == 2
    final = next(props for _, props in rows if props["final"])
    assert final == {"job_id": "job_abc123", "kind": "idea.validate", "feature": "idea", "error_code": "other",
                     "attempt": 3, "max_attempts": 3, "final": True, "terminal_status": "failed_dead"}
    assert all(row.venture_id == "ven_1" and row.workspace_id == "ws_1" and row.person_id == "usr_1" for row, _ in rows)
    assert "private" not in json.dumps([props for _, props in rows])


@pytest.mark.hosted_only
@pytest.mark.parametrize("venture_id,idea", [
    ("ven_inventory", "Warehouse inventory reconciliation"),
    ("ven_hiking", "Family hiking route discovery"),
])
def test_job_failure_reporting_waits_for_outer_writer_and_runs_once(core, venture_id, idea):
    from app.analytics.error_events import record_job_failure
    from app.db.models import Venture
    from app.db.supymem_models import ErrorReport
    from app.state import db
    from core.clock import utcnow

    job = SimpleNamespace(id="job_" + venture_id, kind="research.deep", attempts=1, max_attempts=3,
                          workspace_id=None, venture_id=venture_id)

    def reports():
        with db()() as session:
            return session.query(ErrorReport).all()

    with db()() as session:
        session.add(Venture(id=venture_id, name=idea, raw_idea=idea, stage="draft",
                            created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        with session.begin_nested():
            record_job_failure(session, job, code="research_interrupted", status="retry_wait")
            session.flush()
            assert reports() == []
        assert reports() == []
        session.commit()
        assert len(reports()) == 1 and reports()[0].count == 1
        job.attempts = 2
        record_job_failure(session, job, code="research_interrupted", status="retry_wait")
        session.flush()
        assert reports()[0].count == 1
        session.rollback()
        assert reports()[0].count == 2
        session.commit()
        assert reports()[0].count == 2
    assert len(_rows("job.failed")) == 1


@pytest.mark.hosted_only
def test_model_rejection_row_has_closed_labels_only(core):
    load_company_os_app()
    from app.analytics.error_events import record_model_rejected
    from app.tenancy import bind

    with bind(workspace_id="ws_1", venture_id="ven_1", actor="usr_7"):
        for _ in range(2):
            record_model_rejected(operation_kind="research.source_review", rule="research_output_truncated",
                                  feature="research", operation_id="op_1", venture_id="ven_1",
                                  workspace_id="ws_1", job_id="job_9")
    (row, props), = _rows("model.output_rejected")
    assert props == {"operation_kind": "research.source_review", "rule": "research_output_truncated",
                     "feature": "research", "job_id": "job_9"}
    assert (row.person_id, row.venture_id) == ("usr_7", "ven_1")


def test_confirm_failure_and_guard_are_recorded_without_content(core, monkeypatch):
    load_company_os_app()
    from app.analytics import chat_usage

    recorded = []
    monkeypatch.setattr(chat_usage, "emit", lambda name, **kw: recorded.append((name, kw["props"])))
    chat_usage.record_confirm_failure("ven_1", "site.publish", "insufficient_credits", 402)  # no actor: silent
    chat_usage.record_guard("ven_1", "refusal")
    assert recorded == []
    with chat_usage.attributed_chat_action("usr_1", "ws_1", "ven_1"):
        chat_usage.record_confirm_failure("ven_1", "site.publish", "insufficient_credits", 402)
        chat_usage.record_confirm_failure("ven_1", "sudo rm -rf", "unknown_action", 400)
        chat_usage.record_confirm_failure("ven_2", "site.publish", "forbidden", 403)  # other venture: silent
        chat_usage.record_guard("ven_1", "claim_replaced")
    assert recorded == [
        ("chat.reply_error", {"source": "chat", "failure_kind": "confirm_failed", "reason": "insufficient_credits",
                              "tool": "site.publish", "feature": "publish", "status": 402}),
        ("chat.reply_error", {"source": "chat", "failure_kind": "confirm_failed", "reason": "unknown_action",
                              "tool": "unknown", "feature": "chat", "status": 400}),
        ("guard.flag", {"source": "chat", "feature": "guards", "kind": "claim_replaced"}),
    ]


@pytest.mark.hosted_only
def test_emit_in_session_is_transactional_and_once(core):
    load_company_os_app()
    from app.analytics.events import emit_in_session
    from app.state import db

    with db()() as session:
        for _ in range(2):
            emit_in_session(session, "build.finished", event_key="build:b1", venture_id="ven_1", props={"a": 1})
        emit_in_session(session, "not.allowed", event_key="k", props={})
        session.rollback()
    assert _rows("build.finished") == []
    with db()() as session:
        for _ in range(2):
            emit_in_session(session, "build.finished", event_key="Build:B1", venture_id="ven_1", props={"a": 1})
        session.commit()
    assert len(_rows("build.finished")) == 1


def test_reference_tables_sync_catalog_and_internal_accounts_from_env(core, monkeypatch):
    load_company_os_app()
    from app.analytics.feature_catalog import FEATURES, reference_rows
    from app.analytics.rollup import sync_reference_tables
    from app.db.models import AnalyticsFeatureCatalog, AnalyticsFeatureMap, AnalyticsInternalAccount, Person
    from app.state import db
    from core.clock import utcnow

    with db()() as session:
        session.add_all([Person(id="usr_team", display_name="T", email="dev@team.example", created_at=utcnow()),
                         Person(id="usr_out", display_name="O", email="x@other.example", created_at=utcnow())])
        session.commit()
    monkeypatch.setenv("SUPYMEM_ANALYTICS_INTERNAL_PERSON_IDS", "usr_named, usr_team")
    monkeypatch.setenv("SUPYMEM_ANALYTICS_INTERNAL_EMAIL_DOMAINS", "@team.example")
    for _ in range(2):
        sync_reference_tables()
    with db()() as session:
        assert {r.slug for r in session.query(AnalyticsFeatureCatalog)} == set(FEATURES)
        assert session.query(AnalyticsFeatureMap).count() == len(reference_rows()[1])
        internal = {r.person_id: r.reason for r in session.query(AnalyticsInternalAccount)}
    assert internal == {"usr_named": "id", "usr_team": "id"}
    monkeypatch.setenv("SUPYMEM_ANALYTICS_INTERNAL_PERSON_IDS", "")
    sync_reference_tables()
    with db()() as session:
        assert {r.person_id: r.reason for r in session.query(AnalyticsInternalAccount)} == {"usr_team": "domain"}
    monkeypatch.delenv("SUPYMEM_ANALYTICS_INTERNAL_EMAIL_DOMAINS")
    sync_reference_tables()
    with db()() as session:
        assert session.query(AnalyticsInternalAccount).count() == 0
