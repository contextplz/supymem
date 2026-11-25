"""Canonical research/publication remain visible without importing working caches."""

from __future__ import annotations

import json
import secrets
from datetime import timedelta

import pytest
from sqlalchemy import event, func, select

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401


@pytest.fixture
def reader(journey_db, monkeypatch):
    from app.credits.accounts import ensure_account
    from app import state
    from app.tenancy import bind
    from core.assertion import sign_assertion
    from fastapi.testclient import TestClient

    d = journey_db
    secret = secrets.token_hex(32)
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", secrets.token_hex(32))
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "projection-admin@example.test")
    monkeypatch.setenv("LLM_CREDITS_REQUIRE_GATE", "1")
    from app.main import create_app
    ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    # These two existing dashboard status readers lazily initialize empty
    # control registries. Seed only their exact empty setup keys, retaining
    # empty venture research/site caches and the strict all-DML assertion.
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        state.MEM["munder_breaker_agents"] = {}
        state.MEM["munder_control"] = {}
    client = TestClient(create_app(), raise_server_exceptions=True)  # No lifespan/worker startup.

    def read(suffix, *, actor=None):
        subject = actor or d["actor"]
        path = f"/ventures/{d['vid']}/{suffix}"
        assertion = sign_assertion(secret, sub=subject, email="", role="user", path=path, method="GET")
        return client.get(path, headers={"X-COS-Assertion": assertion, "X-Subject": subject})

    yield {**d, "read": read}
    client.close()


def _run(d, *, market=None, status="done", workspace_id=None, after=0):
    from app.db.models import OnboardingRun, OnboardingStep
    from core.clock import utcnow
    from core.ids import new_id

    now = utcnow() + timedelta(seconds=after)
    run = OnboardingRun(id=new_id("onb"), venture_id=d["vid"], workspace_id=workspace_id or d["wid"],
        mode="guided", status="completed", phase="validate", started_at=now, updated_at=now)
    with d["factory"]() as session:
        session.add(run)
        session.flush()
        if market is not None:
            session.add(OnboardingStep(id=new_id("step"), run_id=run.id, step_id="understand.research_market",
                phase="understand", status=status, idempotency_key=run.id, output_json=json.dumps(market),
                started_at=now, finished_at=now if status == "done" else None))
        session.commit()
    return run.id


def _market():
    return {"mode": "live", "summary": "Saved source scan, not proof of demand",
            "sources": [{"title": "Actual saved source", "url": "https://source.example.test/market"}],
            "competitors": [{"name": "Saved alternative", "url": "https://alternative.example.test"}],
            "demand_signals": [], "differentiation_score": 0.4}


def _publication(d, *, state="live", workspace_id=None, venture_id=None, target="supymem_static", url=None):
    from app.db.supymem_models import Deployment, FounderApp
    from core.clock import utcnow
    from core.ids import new_id

    wid, vid, now = workspace_id or d["wid"], venture_id or d["vid"], utcnow()
    app_id = new_id("app")
    with d["factory"]() as session:
        session.add(FounderApp(id=app_id, workspace_id=wid, venture_id=vid, slug=app_id,
            name="Saved published product", kind="static", created_at=now, updated_at=now))
        session.flush()
        dep = Deployment(id=new_id("dep"), app_id=app_id, workspace_id=wid, venture_id=vid,
            target=target, state=state, version=1, url=url or "https://product.example.test/exact",
            live_at=now if state == "live" else None)
        session.add(dep)
        session.commit()
    return dep.id


def _read_without_writes(d, suffix):
    from app.db.models import MemoryWorkingEntry

    writes = []
    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append((statement.split(None, 3)[:3], _parameters.get("key") if isinstance(_parameters, dict) else None))
    with d["factory"]() as session:
        before = session.scalar(select(func.count()).select_from(MemoryWorkingEntry))
    event.listen(d["engine"], "before_cursor_execute", capture)
    try:
        response = d["read"](suffix)
    finally:
        event.remove(d["engine"], "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    assert writes == [], writes
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(MemoryWorkingEntry)) == before
    return response.json()


def test_dashboard_reads_exact_saved_research_with_empty_pg_cache(reader):
    d = reader
    _run(d, market=_market())
    tile = _read_without_writes(d, "dashboard")["tiles"]["validation"]
    assert tile["state"] == "ready" and tile["source"] == "persisted_research"
    assert tile["mode"] == "live" and tile["verdict"] == "Preliminary" and tile["source_count"] == 1
    assert tile["confidence"] is None and tile["demand"] == "unknown"
    assert tile["competitors"] == _market()["competitors"]
    assert "cannot establish demand" in tile["recommendation"]
    assert tile["questions_answered"] == 0 and tile["rounds"] == 0


def test_saved_unavailable_scan_is_reported_as_degraded_not_missing_or_success(reader):
    d = reader
    _run(d, market={"mode": "unavailable", "reason": "owned_test_no_scan", "sources": []})
    tile = _read_without_writes(d, "dashboard")["tiles"]["validation"]
    assert tile["state"] == "degraded" and tile["verdict"] == "Unavailable"
    assert tile["source_count"] == 0 and tile["demand"] == "unknown"
    assert "Live web research did not run" in tile["recommendation"]


@pytest.mark.parametrize("fault", ["new_run", "unfinished", "foreign_workspace"])
def test_dashboard_does_not_reuse_old_unfinished_or_foreign_research(reader, fault):
    d = reader
    if fault == "new_run":
        _run(d, market=_market())
        _run(d, after=2)
    elif fault == "unfinished":
        _run(d, market=_market(), status="running")
    else:
        _run(d, market=_market(), workspace_id="wsp_foreign")
    tile = _read_without_writes(d, "dashboard")["tiles"]["validation"]
    assert tile["state"] == "empty" and "source" not in tile


def test_export_reads_current_publication_with_empty_pg_cache(reader):
    d = reader
    _publication(d)
    website = _read_without_writes(d, "export")["website"]
    assert website == {"url": "https://product.example.test/exact", "stage": "live", "published": True}


@pytest.mark.parametrize("ambiguous", [False, True])
def test_legacy_workspace_publication_requires_one_exact_venture(reader, ambiguous):
    from app.db.models import SiteVersion, Venture
    from core.clock import utcnow
    from core.ids import new_id

    d, now = reader, utcnow()
    with d["factory"]() as session:
        session.add(SiteVersion(id=new_id("site"), workspace_id=d["wid"], venture_id=None,
            version=1, stage="first_look", template="standard", archetype="product",
            brandkit_hash="0" * 64, copy_hash="1" * 64, path="owned-fixture.html",
            url="https://legacy-product.example.test", sha256="2" * 64, published=1, created_at=now))
        if ambiguous:
            session.add(Venture(id=new_id("ven"), workspace_id=d["wid"], name="Other site owner",
                raw_idea="Other", stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
        session.commit()
    website = _read_without_writes(d, "export")["website"]
    assert website["published"] is (not ambiguous)
    assert website["url"] == (None if ambiguous else "https://legacy-product.example.test")


def test_newest_legacy_site_with_foreign_venture_binding_cannot_promote_older_site(reader):
    from app.db.models import SiteVersion
    from core.clock import utcnow
    from core.ids import new_id

    d, now = reader, utcnow()
    with d["factory"]() as session:
        for version, vid in ((1, None), (2, "ven_foreign_site_owner")):
            session.add(SiteVersion(id=new_id("site"), workspace_id=d["wid"], venture_id=vid,
                version=version, stage="first_look", template="standard", archetype="product",
                brandkit_hash="0" * 64, copy_hash="1" * 64, path="owned-fixture.html",
                url=f"https://legacy-product.example.test/version-{version}", sha256="2" * 64,
                published=1, created_at=now))
        session.commit()
    website = _read_without_writes(d, "export")["website"]
    assert website["published"] is False and website["url"] is None


@pytest.mark.parametrize("fault", ["foreign_workspace", "rolled_back", "wrong_target", "private_url"])
def test_export_does_not_promote_foreign_retired_nonstatic_or_private_sources(reader, fault):
    from app import state
    from app.tenancy import bind

    d = reader
    kwargs = {"foreign_workspace": {"workspace_id": "wsp_foreign"},
              "rolled_back": {"state": "rolled_back"}, "wrong_target": {"target": "ecs"},
              "private_url": {"url": "http://127.0.0.1:9999"}}[fault]
    _publication(d, **kwargs)
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        state.MEM[f"website:{d['vid']}"] = {"url": "https://stale-cache.example.test", "published": True, "stage": "live"}
    website = _read_without_writes(d, "export")["website"]
    assert website["published"] is False
    assert website["stage"] is None
    assert website["url"] != "https://product.example.test/exact"


def test_scoped_readers_refuse_foreign_tenancy_and_signed_nonmember(reader):
    from app.api.dashboard import get_dashboard
    from app.authz.workspace import ensure_subject_person
    from app.tenancy import bind
    from app.ventures.founder_control import export_pack
    from fastapi import Request, Response

    d = reader
    _run(d, market=_market())
    _publication(d)
    with bind(workspace_id="wsp_foreign", venture_id=d["vid"], actor=d["actor"]):
        with pytest.raises(PermissionError, match="working_memory_scope_mismatch"):
            get_dashboard(d["vid"], Request({"type": "http", "headers": []}), Response())
        with pytest.raises(PermissionError, match="working_memory_scope_mismatch"):
            export_pack(d["vid"])
    outsider = "usr_projection_" + secrets.token_hex(6)
    ensure_subject_person(outsider)
    assert d["read"]("dashboard", actor=outsider).status_code == 404
    assert d["read"]("export", actor=outsider).status_code == 404
