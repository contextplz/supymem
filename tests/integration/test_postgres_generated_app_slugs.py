"""Generated public addresses remain unique across concurrent, scoped founders."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock, get_ident

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401


def _tenants(data, count, *, name="Bakery Inventory"):
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Venture
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    from core.ids import new_id

    tenants = []
    for _ in range(count):
        actor = new_id("usr")
        ensure_subject_person(actor)
        workspace = create_workspace(kind="company", name="Generated address QA", owner_id=actor)
        vid, now = new_id("ven"), utcnow()
        with data["factory"]() as session:
            session.add(Venture(id=vid, workspace_id=workspace.id, name=name, raw_idea=name,
                                stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
            session.commit()
        tenants.append({"actor": actor, "wid": workspace.id, "vid": vid})
    return tenants


def test_twenty_same_name_founders_allocate_in_one_session_each(journey_db, monkeypatch):
    from app.db.supymem_models import FounderApp
    from app.hosting import firstlook_publish as publish

    data = journey_db
    tenants = _tenants(data, 20)
    barrier, lock = Barrier(20), Lock()
    first_reads, connections, conflict_count, peak = set(), set(), 0, 0
    original_slug, original_conflict = publish.slug_for, publish._generated_slug_conflict

    def simultaneous_first_choice(name, *, session):
        chosen = original_slug(name, session=session)
        with lock:
            first = get_ident() not in first_reads
            first_reads.add(get_ident())
        if first:
            barrier.wait(timeout=20)
        return chosen

    def counted_conflict(exc):
        nonlocal conflict_count
        result = original_conflict(exc)
        if result:
            with lock:
                conflict_count += 1
        return result

    def checkout(connection, _record, _proxy):
        nonlocal peak
        with lock:
            connections.add(id(connection))
            peak = max(peak, len(connections))

    def checkin(connection, _record):
        with lock:
            connections.discard(id(connection))

    monkeypatch.setattr(publish, "slug_for", simultaneous_first_choice)
    monkeypatch.setattr(publish, "_generated_slug_conflict", counted_conflict)
    event.listen(data["engine"], "checkout", checkout)
    event.listen(data["engine"], "checkin", checkin)
    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            ids = list(pool.map(lambda t: publish._ensure_app(t["vid"], t["wid"], t["actor"]), tenants))
    finally:
        event.remove(data["engine"], "checkout", checkout)
        event.remove(data["engine"], "checkin", checkin)
    assert len(set(ids)) == 20 and conflict_count >= 19
    assert peak == 20  # No nested slug-availability connection checkout.
    with data["factory"]() as session:
        rows = list(session.scalars(select(FounderApp).where(FounderApp.id.in_(ids))))
        assert len({row.slug for row in rows}) == 20
        assert {row.slug for row in rows} == {"bakery-inventory", *[f"bakery-inventory-{n}" for n in range(2, 21)]}
        assert {(row.workspace_id, row.venture_id) for row in rows} == {(t["wid"], t["vid"]) for t in tenants}
        assert all(row.state == "draft" and row.build_mode == "firstlook" for row in rows)
    for tenant, app_id in zip(tenants, ids, strict=True):
        assert publish._ensure_app(tenant["vid"], tenant["wid"], tenant["actor"]) == app_id


def test_same_venture_concurrency_reuses_one_generated_app(journey_db):
    from app.db.supymem_models import FounderApp
    from app.hosting.firstlook_publish import _ensure_app

    data = journey_db
    tenant = _tenants(data, 1)[0]
    barrier = Barrier(8)

    def create(_):
        barrier.wait(timeout=15)
        return _ensure_app(tenant["vid"], tenant["wid"], tenant["actor"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(create, range(8)))
    assert len(set(ids)) == 1
    with data["factory"]() as session:
        rows = list(session.scalars(select(FounderApp).where(FounderApp.venture_id == tenant["vid"])))
        assert len(rows) == 1 and rows[0].id == ids[0]


def test_explicit_requested_slug_is_not_silently_renamed(journey_db):
    from app.db.supymem_models import FounderApp
    from app.infra.apps import create_draft

    data = journey_db
    first, second = _tenants(data, 2)
    options = {"name": "Named address", "slug": "exact-requested-address", "idempotency_key": "explicit-address"}
    created = create_draft(workspace_id=first["wid"], venture_id=first["vid"], actor=first["actor"], **options)
    with pytest.raises(ValueError, match="^slug_taken$"):
        create_draft(workspace_id=second["wid"], venture_id=second["vid"], actor=second["actor"], **options)
    assert create_draft(workspace_id=first["wid"], venture_id=first["vid"], actor=first["actor"], **options)["id"] == created["id"]
    with data["factory"]() as session:
        assert list(session.scalars(select(FounderApp).where(FounderApp.venture_id == second["vid"]))) == []


@pytest.mark.parametrize("refusal", ["foreign_actor", "foreign_workspace", "archived"])
def test_generated_address_preserves_scope_and_archive_refusals(journey_db, refusal):
    from app.db.models import Venture
    from app.db.supymem_models import FounderApp
    from app.hosting.firstlook_publish import _ensure_app
    from app.hosting.service import HostingError
    from core.clock import utcnow

    data = journey_db
    tenant, foreign = _tenants(data, 2)
    actor, wid = tenant["actor"], tenant["wid"]
    if refusal == "foreign_actor":
        actor = foreign["actor"]
    elif refusal == "foreign_workspace":
        wid = foreign["wid"]
    else:
        with data["factory"]() as session:
            session.get(Venture, tenant["vid"]).archived_at = utcnow()
            session.commit()
    with pytest.raises(HostingError, match="^hosting_app_not_found$"):
        _ensure_app(tenant["vid"], wid, actor)
    with data["factory"]() as session:
        assert list(session.scalars(select(FounderApp).where(FounderApp.venture_id == tenant["vid"]))) == []


def test_unrelated_insert_constraint_is_not_retried(journey_db, monkeypatch):
    from app.hosting import firstlook_publish as publish

    data = journey_db
    first, second = _tenants(data, 2, name="Unrelated constraint")
    existing_id = publish._ensure_app(first["vid"], first["wid"], first["actor"])
    calls = []

    def same_id(_kind):
        calls.append(_kind)
        return existing_id

    monkeypatch.setattr(publish, "new_id", same_id)
    with pytest.raises(IntegrityError) as error:
        publish._ensure_app(second["vid"], second["wid"], second["actor"])
    assert error.value.orig.diag.constraint_name == "apps_pkey" and calls.count("app") == 1


def test_sqlite_generated_insert_keeps_one_outer_write_transaction(journey_db, tmp_path):
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base
    from app.db.session import make_engine
    from app.hosting.firstlook_publish import _ensure_app
    from app.state import set_factory

    engine = make_engine("sqlite:///" + str(tmp_path / "generated-address.db"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    set_factory(factory)
    try:
        first, second = _tenants({"factory": factory}, 2)
        first_id = _ensure_app(first["vid"], first["wid"], first["actor"])
        assert _ensure_app(first["vid"], first["wid"], first["actor"]) == first_id
        assert _ensure_app(second["vid"], second["wid"], second["actor"]) != first_id
    finally:
        set_factory(journey_db["factory"])
        engine.dispose()
