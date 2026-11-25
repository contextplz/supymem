"""Legacy canonical charters remain scoped context after the PG cache upgrade."""

from __future__ import annotations

import json
import secrets

import pytest
from sqlalchemy import func, select

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401


def _locked(d, *, version="1.0.0", content=None, locked=1, venture_id=None):
    from app.db.models import Charter
    from core.canonical import digest
    from core.clock import utcnow
    from core.ids import new_id

    content = content if content is not None else {
        "one_liner": "Existing founder charter Ω",
        "wedge": "Exact current canonical wedge",
        "mission": "Keep the complete original statement [[people/canonical]]",
        "explicit_non_goals": ["Do not send messages", "Do not publish without review"],
        "long_founder_constraint": "Complete Ω constraint " * 300,
    }
    now = utcnow()
    return Charter(id=new_id("chr"), venture_id=venture_id or d["vid"],
                   version=version, parent_version=None, locked=locked,
                   content_json=json.dumps(content, ensure_ascii=False, indent=2),
                   checksum=digest(content), slots_snapshot_json='{"founder": "exact Ω"}',
                   dissent_json='[{"statement": "Preserve dissent"}]',
                   created_at=now, locked_at=now if locked else None)


@pytest.fixture
def canonical(journey_db):
    from app.db.models import Venture
    d = journey_db
    row = _locked(d)
    with d["factory"]() as session:
        session.add(row)
        session.flush()
        session.get(Venture, d["vid"]).active_charter_id = row.id
        session.commit()
    return {**d, "charter": row}


def _bound(d, **override):
    from app.tenancy import bind
    return bind(workspace_id=override.get("workspace_id", d["wid"]),
                venture_id=override.get("venture_id", d["vid"]), actor=d["actor"])


def _request(d, *, actor=None):
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from pytest import MonkeyPatch

    # No lifespan/scheduler startup; the real signed scope middleware and route run.
    path = f"/ventures/{d['vid']}/charter"
    with MonkeyPatch.context() as env:
        secret = secrets.token_hex(32)
        env.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
        env.setenv("COMPANY_OS_VAULT_SECRET", secrets.token_hex(32))
        env.setenv("ADMIN_BOOTSTRAP_EMAILS", "charter-admin@example.test")
        env.setenv("LLM_CREDITS_REQUIRE_GATE", "1")
        from app.main import create_app
        subject = actor or d["actor"]
        assertion = sign_assertion(secret, sub=subject, email="", role="user", path=path, method="GET")
        client = TestClient(create_app(), raise_server_exceptions=True)
        try:
            return client.get(path, headers={"X-COS-Assertion": assertion, "X-Subject": subject})
        finally:
            client.close()


def test_legacy_canonical_charter_projects_without_cache_import(canonical, monkeypatch):
    from app import state
    from app.chat.context import assemble
    from app.db.models import Job, MemoryWorkingEntry
    from app.gbrain import cycle
    from app.ventures.founder_control import export_pack

    d, row = canonical, canonical["charter"]
    expected = json.loads(row.content_json)
    with d["factory"]() as session:
        before_jobs = session.scalar(select(func.count(Job.id)))
        assert session.get(MemoryWorkingEntry, ("venture:" + d["vid"], "charter:" + d["vid"])) is None
    ingested = []
    monkeypatch.setattr(cycle, "ingest_markdown", lambda **kw: ingested.append(kw) or 1)
    monkeypatch.setattr(cycle, "_doc_paths", lambda _: [])
    with _bound(d):
        found = state.MEM["charter:" + d["vid"]]
        assert found["content"] == expected and found["content_json"] == row.content_json
        assert found["id"] == row.id and found["venture_id"] == d["vid"] and found["locked"] == 1
        assert found["checksum"] == row.checksum and found["version"] == row.version
        assert found["slots_snapshot_json"] == row.slots_snapshot_json
        assert found["dissent_json"] == row.dissent_json
        assert found["created_at"] == row.created_at.isoformat()
        assert found["locked_at"] == row.locked_at.isoformat()
        assert assemble(d["vid"]).charter == expected
        assert export_pack(d["vid"])["charter"] == {key: expected[key] for key in ("one_liner", "wedge", "mission")}
        assert cycle.cycle_one(d["vid"]) == 1
        assert ingested == [{"venture_id": d["vid"], "slug": "venture/charter", "content": str(expected), "source": "charter"}]
        assert state.MEM.get("charter:" + d["vid"] + ":history") is None
    result = _request(d)
    assert result.status_code == 200
    assert result.json() == {"charter_id": row.id, "version": row.version, "checksum": row.checksum,
                             "content": expected, "locked": True}
    with d["factory"]() as session:
        assert session.get(MemoryWorkingEntry, ("venture:" + d["vid"], "charter:" + d["vid"])) is None
        assert session.scalar(select(func.count(Job.id))) == before_jobs


def test_current_pointer_outranks_stale_pg_payload(canonical):
    from app import state
    from app.db.models import MemoryWorkingEntry, Venture
    d = canonical
    key = "charter:" + d["vid"]
    with _bound(d):
        old = state.MEM[key]
        state.MEM[key] = old
    new = _locked(d, version="2.0.0", content={"one_liner": "Current amendment", "complete": "new Ω source"})
    new.parent_version = old["version"]
    with d["factory"]() as session:
        session.add(new)
        session.flush()
        session.get(Venture, d["vid"]).active_charter_id = new.id
        session.commit()
    with _bound(d):
        assert state.MEM[key]["id"] == new.id
        assert state.MEM[key]["content"] == json.loads(new.content_json)
    assert _request(d).json()["charter_id"] == new.id
    with d["factory"]() as session:
        stored = session.get(MemoryWorkingEntry, ("venture:" + d["vid"], key))
        assert json.loads(stored.payload_json) == old and stored.version == 1


@pytest.mark.parametrize("kind", ["foreign_pointer", "draft", "missing_pointer", "array_content", "malformed_content"])
def test_invalid_current_pointer_cannot_recover_old_payload_or_claim_locked(canonical, kind):
    from app import state
    from app.db.models import Venture
    from core.clock import utcnow
    from core.ids import new_id
    d = canonical
    key = "charter:" + d["vid"]
    with _bound(d):
        state.MEM[key] = state.MEM[key]
    with d["factory"]() as session:
        venture = session.get(Venture, d["vid"])
        if kind == "missing_pointer":
            venture.active_charter_id = new_id("chr")
        else:
            vid = d["vid"]
            if kind == "foreign_pointer":
                vid = new_id("ven")
                now = utcnow()
                session.add(Venture(id=vid, workspace_id=d["wid"], name="Foreign charter owner", raw_idea="Foreign source",
                                    stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
                session.flush()
            candidate = _locked(d, version="2.0.0", venture_id=vid, locked=0 if kind == "draft" else 1)
            if kind == "array_content":
                candidate.content_json = '[]'
            elif kind == "malformed_content":
                candidate.content_json = '{'
            session.add(candidate)
            session.flush()
            venture.active_charter_id = candidate.id
        session.commit()
    with _bound(d):
        assert state.MEM.get(key) is None
        with pytest.raises(KeyError):
            state.MEM[key]
    response = _request(d)
    assert response.status_code == 404 and response.json()["detail"] == "no_charter"


def test_no_active_pointer_does_not_select_retired_row_or_claim_cached_draft_locked(canonical):
    from app import state
    from app.db.models import Venture
    d = canonical
    key = "charter:" + d["vid"]
    with d["factory"]() as session:
        session.get(Venture, d["vid"]).active_charter_id = None
        session.commit()
    with _bound(d):
        assert state.MEM.get(key) is None  # An older locked row is not current.
        working_only = {"id": "draft-only", "locked": 0, "content": {"one_liner": "Working draft context"}}
        state.MEM[key] = working_only
        assert state.MEM[key] == working_only  # Existing internal context compatibility.
    response = _request(d)
    assert response.status_code == 404 and response.json()["detail"] == "no_charter"


def test_canonical_fallback_keeps_scope_and_signed_membership_guards(canonical):
    from app import state
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Venture
    from app.tenancy import ScopeRequired
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    from core.ids import new_id

    d = canonical
    outsider = "usr_charter_" + secrets.token_hex(6)
    ensure_subject_person(outsider)
    foreign = create_workspace(kind="company", name="Foreign scope", owner_id=outsider)
    other = new_id("ven")
    now = utcnow()
    with d["factory"]() as session:
        session.add(Venture(id=other, workspace_id=d["wid"], name="Another venture", raw_idea="Other",
                            stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
        session.commit()
    with pytest.raises(ScopeRequired):
        state.MEM.get("charter:" + d["vid"])
    for overrides in ({"workspace_id": foreign.id}, {"venture_id": other}):
        with _bound(d, **overrides), pytest.raises(PermissionError, match="working_memory_scope_mismatch"):
            state.MEM.get("charter:" + d["vid"])
    assert _request(d, actor=outsider).status_code == 404
