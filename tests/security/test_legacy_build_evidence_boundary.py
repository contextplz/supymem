"""Assembled hosted routes cannot publish synthetic QA or loopback previews."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_core_signed_boundary import headers


def _venture(client):
    result = client.post("/ventures", headers=headers("/ventures", "POST"),
                         json={"name": "Build evidence", "raw_idea": "An order board", "start": False})
    assert result.status_code == 201, result.text
    return result.json()["id"]


@pytest.mark.parametrize("suffix", ["gates", "preview"])
def test_hosted_build_shortcuts_fail_before_executing_or_mutating(client, monkeypatch, suffix):
    from app.api import build
    from app.db.supymem_models import BuildRun, QaRun
    from app.state import MEM, db

    vid = _venture(client)
    path = f"/ventures/{vid}/build/{suffix}"
    def unexpected(*args, **kwargs):
        pytest.fail("legacy fixture execution must not run in hosted mode")
    monkeypatch.setattr(build, "run_all", unexpected)
    monkeypatch.setattr(build, "start_preview", unexpected)
    # Availability of a future real worker must not reactivate these fixtures.
    from app.worker import handlers
    monkeypatch.setattr(handlers, "UNAVAILABLE_HOSTED", {})
    result = client.post(path, headers=headers(path, "POST"))
    assert result.status_code == 503, result.text
    assert result.json()["detail"]["capability"] == "legacy_build_evidence"
    assert f"gates:{vid}" not in MEM and f"preview:{vid}" not in MEM
    with db()() as session:
        assert not session.scalars(select(BuildRun).where(BuildRun.venture_id == vid)).all()
        assert not session.scalars(select(QaRun).where(QaRun.venture_id == vid)).all()


def test_signed_scope_precedes_unavailable_evidence_response(client):
    vid = _venture(client)
    for suffix in ("gates", "preview"):
        path = f"/ventures/{vid}/build/{suffix}"
        assert client.post(path, headers={"X-Subject": "usr_alice"}).status_code == 401
        assert client.post(path, headers=headers(path, "POST", "usr_bob")).status_code == 404


def test_hosted_read_does_not_promote_old_memory_to_execution_evidence(client):
    from app.state import MEM

    vid = _venture(client)
    MEM[f"gates:{vid}"] = {"all_passed": True, "readiness_score": 0.95}
    MEM[f"preview:{vid}"] = {"url": "http://127.0.0.1:5173", "status": "ready"}
    path = f"/ventures/{vid}/build"
    result = client.get(path, headers=headers(path))
    assert result.status_code == 200, result.text
    assert result.json()["gates"] is None and result.json()["preview"] is None
    assert "all_passed" not in result.text and "127.0.0.1" not in result.text


@pytest.mark.parametrize("environment", ["staging", "production", "prod"])
def test_hosted_env_alone_prohibits_legacy_shortcut(client, monkeypatch, environment):
    from app.api.build import _require_legacy_local_execution
    from fastapi import HTTPException

    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    monkeypatch.setenv("APP_ENV", environment)
    with pytest.raises(HTTPException) as exc:
        _require_legacy_local_execution()
    assert exc.value.status_code == 503


def test_private_correction_eligibility_checks_real_legacy_proof_before_exposing_write(client):
    import json

    from app.db.models import Venture
    from app.db.supymem_models import BuildRun, FounderApp, FounderRequest
    from app.state import db
    from core.clock import utcnow

    vid = _venture(client)
    now = utcnow()
    spec = {"artifact_profile": "static_landing_v1", "lifecycle": {"review": {
        "status": "accepted", "source_artifact_id": "artifact_legacy", "report_digest": "a" * 64}}}
    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
        session.add(FounderApp(id="app_legacy_correction", workspace_id=wid, venture_id=vid, slug="legacy-correction",
                              name="Legacy", kind="landing", created_at=now, updated_at=now))
        session.flush()
        session.add(BuildRun(id="bld_legacy_correction", workspace_id=wid, venture_id=vid,
                            app_id="app_legacy_correction", trigger="manual", agent="legacy", state="succeeded",
                            spec=json.dumps(spec), runtime_version=1, closed_at=now, created_at=now))
        session.commit()
    path = "/builds/bld_legacy_correction/corrections/availability"
    response = client.get(path, headers=headers(path))
    assert response.status_code == 200, response.text
    assert response.json() == {"build_id": "bld_legacy_correction", "available": False,
                               "reason": "hosting_build_not_accepted"}
    assert client.get(path, headers=headers(path, subject="usr_bob")).status_code == 404
    write = "/builds/bld_legacy_correction/corrections"
    response = client.post(write, headers=headers(write, "POST"), json={"source_artifact_id": "artifact_legacy",
        "report_digest": "a" * 64, "instruction": "Make the private accessibility correction."})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "hosting_build_not_accepted"
    with db()() as session:
        assert session.query(FounderRequest).filter_by(source="private_build_correction").count() == 0
        assert session.get(BuildRun, "bld_legacy_correction").spec == json.dumps(spec)
