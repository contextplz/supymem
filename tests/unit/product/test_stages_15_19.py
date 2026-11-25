"""Stages 15–18: debug, iterate, import, analytics, admin, AI proxy."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app

ROOT = Path(__file__).resolve().parents[3]


def _boot(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/s15.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IDEA_SYNC", "0")
    monkeypatch.delenv("BUILDER_SANDBOX", raising=False)
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    main = load_company_os_app()
    from app import state
    from app.worker.catalog import load_catalogue, load_trigger_policy

    load_catalogue.cache_clear()
    load_trigger_policy.cache_clear()
    state.reset_db_factory()
    return TestClient(main.create_app())


def _venture_app(client: TestClient):
    idea = "Waitlist for neighbourhood bakers taking custom cake orders via a landing page"
    ven = client.post("/ventures", json={"raw_idea": idea, "start": False}).json()
    created = client.post(
        f"/ventures/{ven['id']}/apps",
        json={"name": "Bakery", "slug": f"bakery{ven['id'][-6:]}"},
    )
    assert created.status_code == 201, created.text
    return ven, created.json()


def test_debug_iterate_import_and_block(tmp_path, monkeypatch) -> None:
    client = _boot(tmp_path, monkeypatch)
    ven, app = _venture_app(client)
    app_id = app["id"]
    assert app["state"] == "draft"

    opened = client.post(
        f"/apps/{app_id}/incidents",
        json={
            "summary": "5xx burst on /checkout",
            "kind": "5xx",
            "log_line": "Traceback: Internal Server Error",
        },
    )
    assert opened.status_code == 201, opened.text
    listed = client.get(f"/apps/{app_id}/incidents")
    assert listed.json()["items"]
    detail = listed.json()["items"][0]
    assert detail["state"] == "open"
    assert detail["debug_run_id"] is None

    low = client.post(
        f"/apps/{app_id}/iterate",
        json={"text": "Make the landing hero mention same-day pickup"},
    )
    assert low.status_code == 200, low.text
    assert low.json()["interpret"]["risk"] == "low"

    from app.db.supymem_models import FounderRequest
    from app.state import db

    factory = db()
    with factory() as session:
        req = session.get(FounderRequest, low.json()["request_id"])
        assert req is not None
        assert req.outcome == "pending"

    risky = client.post(
        f"/apps/{app_id}/iterate",
        json={"text": "Delete every user password and refund all payments from admin"},
    )
    assert risky.status_code == 200
    assert risky.json()["interpret"]["risk"] == "high"

    scoped = client.post(f"/apps/{app_id}/iterate", json={"text": "Ship a native iOS app"})
    assert scoped.json()["interpret"]["scope"]["classification"] == "out"

    # Hosted execution must refuse legacy adapters that could synthesize a
    # successful build or debugging result without a deployed application.
    from app.core.jobs import FatalError
    from app.worker.run_sync import run_kind

    monkeypatch.setenv("APP_ENV", "production")
    for kind in ("app.debug", "app.iterate", "deploy.run"):
        with pytest.raises(FatalError, match="capability_unavailable:" + kind):
            run_kind(kind, {})
    assert client.get(f"/apps/{app_id}").json()["state"] == "draft"

    # Fixture imports and host-local presigns were retired in favor of the
    # authenticated app-scoped source-intake API.
    imported = client.post(
        f"/ventures/{ven['id']}/imports",
        json={"source": "fixture", "fixture": "nextjs-app"},
    )
    assert imported.status_code == 404
    presign = client.post("/uploads/presign", json={"filename": "bundle.zip", "size_bytes": 12})
    assert presign.status_code == 404

    from app.workspaces.block import set_blocked

    set_blocked(ven["workspace_id"], True, actor="adm_test")
    blocked = client.get(f"/ventures/{ven['id']}/apps")
    assert blocked.status_code == 423
    set_blocked(ven["workspace_id"], False, actor="adm_test")
    assert client.get(f"/ventures/{ven['id']}/apps").status_code == 200


def test_founder_analytics_and_internal_events(tmp_path, monkeypatch) -> None:
    client = _boot(tmp_path, monkeypatch)
    ven, app = _venture_app(client)
    _ = ven
    key = client.post(f"/apps/{app['id']}/ingest-key")
    assert key.status_code == 201, key.text
    pk = key.json()["secret"]
    assert pk.startswith("pk_app_")

    unknown = client.post("/e", json={"key": "pk_app_nope", "event": "signup"})
    assert unknown.status_code == 204

    first = client.post("/e", json={"key": pk, "event": "signup", "props": {"plan": "free"}})
    assert first.status_code == 200
    client.post("/e", json={"key": pk, "event": "page_view", "props": {}})
    notices = []
    monkeypatch.setattr("app.product_analytics.service.notice", lambda **kwargs: notices.append(kwargs))
    for props in ({"amount_minor": 19900}, {"amount": "not money", "currency": "USD"}, {"amount_minor": -100}):
        assert client.post("/e", json={"key": pk, "event": "payment", "props": props}).status_code == 200
    assert notices == []  # Public events cannot announce a first payment.
    assert client.post("/e", json={"key": pk, "event": "page_view", "props": {"oversize": "x" * 5000}}).status_code == 204

    overview = client.get(f"/apps/{app['id']}/analytics/overview")
    assert overview.status_code == 200, overview.text
    assert overview.json()["status"] == "unverified"
    assert overview.json()["today"]["visits"] >= 1 and overview.json()["today"]["signups"] == 1
    # Reading the dashboard must not write a rollup row.
    from app.db.supymem_models import AppMetricDaily
    from app.state import db
    with db()() as session:
        assert session.query(AppMetricDaily).filter_by(app_id=app["id"]).count() == 0
    # Apps list oldest first so the page and the beacon agree on "the" app.
    second = client.post(f"/ventures/{ven['id']}/apps", json={"name": "Second", "slug": f"second{ven['id'][-6:]}"})
    assert second.status_code == 201, second.text
    assert [row["id"] for row in client.get(f"/ventures/{ven['id']}/apps").json()["items"]][0] == app["id"]
    assert overview.json()["achieved_on"] is None
    assert overview.json()["today"]["revenue_minor"] is None
    assert overview.json()["unique_visitors"] is None
    assert overview.json()["active_users"] is None
    funnel = client.get(f"/apps/{app['id']}/analytics/funnel")
    assert "steps" in funnel.json()
    assert funnel.json()["measurement_basis"] == "independent_browser_event_counts_not_ordered_people"
    assert funnel.json()["counts"]["payment"] == 3
    events = client.get(f"/apps/{app['id']}/analytics/events")
    assert events.json()["items"]
    revenue = client.get(f"/apps/{app['id']}/analytics/revenue").json()
    assert revenue["status"] == "unavailable" and revenue["amount_minor"] is None and revenue["currency"] is None
    assert client.get(f"/apps/{app['id']}/analytics/health").status_code == 200
    assert client.get(f"/apps/{app['id']}/analytics/ai").status_code == 200
    retention = client.get(f"/apps/{app['id']}/analytics/retention").json()
    assert retention["status"] == "unavailable" and retention["weekly_active"] is None

    tel = client.post("/telemetry", json={"event": "feature.used", "props": {"name": "iterate"}})
    assert tel.status_code == 422  # Legacy arbitrary telemetry payloads are rejected.
    rejected = client.post("/telemetry", json={"event": "not_a_real_event", "props": {}})
    assert rejected.status_code == 422


@pytest.mark.skipif(
    not (ROOT / "services/company_os/app/api/openai_api.py").is_file(),
    reason="Hosted OpenAI proxy is not in the public local release",
)
def test_ai_proxy_v1_with_key(tmp_path, monkeypatch) -> None:
    client = _boot(tmp_path, monkeypatch)
    _ven, app = _venture_app(client)
    created = client.post(
        f"/apps/{app['id']}/api-keys",
        json={"scopes": ["chat"], "monthly_cap_credits": 50},
    )
    assert created.status_code == 201, created.text
    secret = created.json()["secret"]
    assert secret.startswith("sk_app_")
    listed = client.get(f"/apps/{app['id']}/api-keys")
    assert listed.json()["items"]
    assert "secret" not in listed.json()["items"][0]

    headers = {"Authorization": f"Bearer {secret}"}
    # Exercise the mounted Core proxy with real key lookup and credit holds.
    ok = client.post(
        "/openai/v1/chat/completions",
        json={"model": "supymem-flash", "messages": [{"role": "user", "content": "hello"}]},
        headers=headers,
    )
    assert ok.status_code == 409, ok.text
    assert ok.json()["error"]["code"] == "app_not_live"

    # This isolates proxy authorization/metering from deployment lifecycle.
    # Production app creation must stay draft until a verified deployment.
    from app.db.supymem_models import FounderApp
    from app.state import db

    with db()() as session:
        row = session.get(FounderApp, app["id"])
        row.state = "live"
        session.commit()
    from tests.unit.product.test_hermes_platform_credits import _supported_proxy
    calls = _supported_proxy(monkeypatch)
    ok = client.post(
        "/openai/v1/chat/completions",
        json={"model": "supymem-flash", "messages": [{"role": "user", "content": "hello"}]},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    assert "captured provider completion" in ok.json()["choices"][0]["message"]["content"]
    assert ok.headers["x-supymem-credits-used"] == "22"

    models = client.get("/openai/v1/models", headers=headers)
    assert models.status_code == 200
    ids = {m["id"] for m in models.json()["data"]}
    assert "supymem-pro" in ids

    stream = client.post(
        "/openai/v1/chat/completions",
        json={
            "model": "supymem-flash",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers=headers,
    )
    assert stream.status_code == 200
    assert "data:" in stream.text
    assert "data: [DONE]" in stream.text
    assert stream.headers["x-supymem-credits-used"] == "22" and len(calls) == 2

    capped = client.post(
        f"/apps/{app['id']}/api-keys",
        json={"scopes": ["chat"], "monthly_cap_credits": 0},
    )
    zero = capped.json()["secret"]
    starved = client.post(
        "/openai/v1/chat/completions",
        json={"model": "supymem-flash", "messages": [{"role": "user", "content": "x"}]},
        headers={"Authorization": f"Bearer {zero}"},
    )
    assert starved.status_code == 402
    assert starved.json()["error"]["code"] == "insufficient_credits"
    assert len(calls) == 2

    # The retired standalone service must refuse every real request. Its old
    # embedding/moderation and per-tool behavior are not Core capabilities.
    spec = importlib.util.spec_from_file_location(
        "ai_proxy_stage16", ROOT / "services" / "ai_proxy" / "app" / "main.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    retired = TestClient(mod.app)
    ingest = client.post(f"/apps/{app['id']}/ingest-key")
    assert ingest.status_code == 201, ingest.text
    public_headers = {"Authorization": f"Bearer {ingest.json()['secret']}"}
    chat_body = {"model": "supymem-flash", "messages": [{"role": "user", "content": "hello"}]}
    for method, path, body in [
        ("POST", "/v1/chat/completions", chat_body),
        ("POST", "/v1/chat/completions", {**chat_body, "tools": [{"type": "function", "function": {"name": "delete_account"}}]}),
        ("POST", "/v1/chat/completions", {**chat_body, "stream": True}),
        ("GET", "/v1/models", None),
        ("POST", "/v1/moderations", {"input": "hello"}),
        ("POST", "/v1/embeddings", {"input": "hello", "model": "supymem-embed"}),
        ("GET", "/v1/usage", None),
    ]:
        blocked = retired.request(method, path, json=body, headers=headers)
        assert blocked.status_code == 503, blocked.text
        assert blocked.json()["detail"]["code"] == "legacy_proxy_unavailable"
        public = retired.request(method, path, json=body, headers=public_headers)
        assert public.status_code == 401, public.text
        assert public.json()["error"]["code"] == "invalid_api_key"


def test_learning_and_experiments(tmp_path, monkeypatch) -> None:
    _boot(tmp_path, monkeypatch)
    from app.experiments.service import create_experiment, list_experiments, run_evaluate
    from app.learning.cycle import list_proposals, run_cycle

    created = create_experiment(kind="copy", hypothesis="Shorter CTA lifts signup", baseline=0.1)
    assert created["state"] == "running"
    for _ in range(8):
        run_evaluate({})
    assert list_experiments()
    run_cycle({})
    proposals = list_proposals()
    assert proposals
    assert proposals[0]["patch"]["before"]["note"] == "no live mutation"
    assert proposals[0]["state"] == "proposed"
