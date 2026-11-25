"""Actual migrated PostgreSQL drafts; all coding/model/provider seams closed."""
import asyncio
import json

import pytest
from sqlalchemy import inspect, select

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.unit.autopilot.test_site_effects import _generate


def test_migrated_bound_drafts_do_not_borrow_another_ventures_latest(journey_db, monkeypatch):
    from app.db.models import Venture, SiteVersion
    from app.firstlook import versions
    from core.clock import utcnow
    d = journey_db
    columns = {column["name"]: column for column in inspect(d["engine"]).get_columns("site_versions")}
    assert columns["venture_id"]["nullable"] is True
    first, proof = _generate((None, d["vid"]), monkeypatch)
    assert proof.outcome() == "completed"
    now = utcnow()
    second_vid = d["vid"] + "_second"
    with d["factory"]() as session:
        session.add(Venture(id=second_vid, workspace_id=d["wid"], name="Second owned venture",
            raw_idea="Other synthetic draft", stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
        session.commit()
    second, second_proof = _generate((None, second_vid), monkeypatch)
    assert second_proof.outcome() == "completed"
    assert versions.get_latest(d["wid"]).id == second.id  # legacy workspace API remains compatible
    assert versions.get_latest(d["wid"], venture_id=d["vid"]).id == first.id
    assert versions.get_by_version(d["wid"], second.version, venture_id=d["vid"]) is None
    from app.api.firstlook_api import get_site, get_private_site_preview
    from app.tenancy import bind
    from fastapi import HTTPException
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        assert get_site(d["wid"], venture_id=d["vid"])["id"] == first.id
        preview = json.loads(get_private_site_preview(d["wid"], venture_id=d["vid"]).body)
        assert preview["site_id"] == first.id and preview["venture_id"] == d["vid"]
        for reader in (get_site, get_private_site_preview):
            with pytest.raises(HTTPException) as denied:
                reader(d["wid"], venture_id=second_vid)
            assert denied.value.status_code == 404
    assert get_site(d["wid"])["id"] == second.id  # explicit legacy omitted-query compatibility
    with d["factory"]() as session:
        for row in session.scalars(select(SiteVersion).where(
                SiteVersion.workspace_id == d["wid"], SiteVersion.venture_id == d["vid"])):
            row.venture_id = None  # historical attribution is deliberately unknown
        session.commit()
    assert proof.outcome() == "held"
    assert versions.get_latest(d["wid"], venture_id=d["vid"]) is None
    assert versions.get_latest(d["wid"], venture_id=second_vid).id == second.id
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        with pytest.raises(HTTPException) as missing:
            get_site(d["wid"], venture_id=d["vid"])
        assert missing.value.status_code == 404


def test_actual_pg_writer_rejects_foreign_venture_and_workspace(journey_db, monkeypatch):
    from app.db.models import SiteVersion
    from app.firstlook import service as firstlook
    d = journey_db
    with d["factory"]() as session:
        before = session.query(SiteVersion).count()
    def forbidden(*args, **kwargs):
        pytest.fail("Foreign scope reached writer")
    monkeypatch.setattr(firstlook, "write_site", forbidden)
    for wrong in ({"venture": "foreign"}, {"workspace": "foreign"}):
        with pytest.raises(PermissionError, match="site_scope_mismatch"):
            _generate((None, d["vid"]), monkeypatch, **wrong)
    with d["factory"]() as session:
        assert session.query(SiteVersion).count() == before


def test_actual_pg_receipt_rechecks_snapshot_drift_and_mixed_candidate(journey_db, monkeypatch):
    from app.db.models import SiteVersion
    from app.autopilot import receipts, service
    from app.autopilot.authority import founder_scope
    from app.firstlook import versions
    d = journey_db
    view, proof = _generate((None, d["vid"]), monkeypatch)
    assert proof.outcome() == "completed"
    with founder_scope(d["vid"], service._owner(d["vid"])):
        with receipts.collect(d["vid"]) as mixed:
            versions.record_generated_draft(view.id)
            receipts.saved_document("missing_companion", "draft", "Owned synthetic content")
        assert mixed.outcome() == "held"
    with d["factory"]() as session:
        row = session.get(SiteVersion, view.id)
        copy = json.loads(row.copy_json)
        copy["_render_snapshot"]["html"] += "Changed after generation"
        row.copy_json = json.dumps(copy)
        session.commit()
    assert proof.outcome() == "held"
    with founder_scope(d["vid"], service._owner(d["vid"])):
        with receipts.collect(d["vid"]) as invalid:
            versions.record_generated_draft(view.id)
        assert invalid.outcome() == "held"  # malformed durable bytes cannot produce new proof


def test_actual_pg_viewer_cannot_generate_or_write_a_bound_draft(journey_db, monkeypatch):
    from app.authz.workspace import ensure_subject_person
    from app.db.models import WorkspaceMember, SiteVersion
    from app.firstlook import service as firstlook
    from app.tenancy import bind
    from core.clock import utcnow
    d = journey_db
    viewer = ensure_subject_person(d["actor"] + "_viewer")
    with d["factory"]() as session:
        session.add(WorkspaceMember(id=viewer + "_member", workspace_id=d["wid"], person_id=viewer,
            role="viewer", can_approve=0, created_at=utcnow()))
        session.commit()
        before = session.query(SiteVersion).count()
    def forbidden(*args, **kwargs):
        pytest.fail("Read-only actor crossed writer admission")
    monkeypatch.setattr(firstlook, "write_site", forbidden)
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=viewer):
        with pytest.raises(PermissionError, match="site_write_forbidden"):
            asyncio.run(firstlook.generate_first_look(d["wid"], "Owned synthetic draft", venture_id=d["vid"]))
    with d["factory"]() as session:
        assert session.query(SiteVersion).count() == before
    from app.api.firstlook_api import GenerateBody, generate_site
    from app.tenancy import current_or_none
    from fastapi import HTTPException
    assert current_or_none() is None
    with pytest.raises(HTTPException) as denied:
        asyncio.run(generate_site(d["wid"], GenerateBody(idea_text="Owned test", venture_id=d["vid"]),
                                  x_subject=viewer))
    assert denied.value.status_code == 403
    with d["factory"]() as session:
        assert session.query(SiteVersion).count() == before


def test_explicit_venture_interest_counts_only_its_bound_versions(journey_db, monkeypatch):
    from app.api.firstlook_api import get_site
    from app.db.models import SiteVersion, Venture
    from app.firstlook import interest
    from app.tenancy import bind
    from core.clock import utcnow
    d = journey_db
    first, _ = _generate((None, d["vid"]), monkeypatch)
    second_vid = d["vid"] + "_interest_b"
    now = utcnow()
    with d["factory"]() as session:
        session.add(Venture(id=second_vid, workspace_id=d["wid"], name="Synthetic B",
            raw_idea="Synthetic B", stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
        session.commit()
    second, _ = _generate((None, second_vid), monkeypatch)
    interest.record(workspace_id=d["wid"], site_version=second.version)
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        payload = get_site(d["wid"], venture_id=d["vid"])
    assert payload["id"] == first.id and payload["interest_count"] == 0
    assert payload["interest"]["by_version"] == {}
    # Counts are submissions, never asserted distinct people or real users.
    interest.record(workspace_id=d["wid"], site_version=first.version)
    interest.record(workspace_id=d["wid"], site_version=first.version)
    assert interest.aggregate(d["wid"], venture_id=d["vid"]) == {
        "total": 2, "by_version": {first.version: 2}}
    assert interest.aggregate(d["wid"], venture_id=second_vid) == {
        "total": 1, "by_version": {second.version: 1}}
    assert interest.aggregate(d["wid"], venture_id="foreign_venture") == {"total": 0, "by_version": {}}
    assert interest.aggregate("foreign_workspace", venture_id=d["vid"]) == {"total": 0, "by_version": {}}
    assert get_site(d["wid"])["interest_count"] == 3  # legacy omitted-query totals
    with d["factory"]() as session:
        session.get(SiteVersion, first.id).venture_id = None
        session.commit()
    assert interest.aggregate(d["wid"], venture_id=d["vid"]) == {"total": 0, "by_version": {}}
    assert interest.aggregate(d["wid"])["total"] == 3  # historical signals not rewritten
