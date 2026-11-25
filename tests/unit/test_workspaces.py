"""Workspace model, Desk uniqueness, and venture resolution (35, 46 T10.3)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError

import pytest
from sqlalchemy import event, select

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def cos(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ws.db")
    load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    from app.state import db

    db()
    yield


def test_desk_is_created_once_and_reused(cos) -> None:
    from app.workspaces import resolve

    first = resolve.ensure_desk()
    second = resolve.ensure_desk()
    assert first.id == second.id
    assert first.kind == "desk"


@pytest.mark.parametrize("domain", ["Factory maintenance", "Family hiking"])
def test_local_owner_reads_do_not_wait_for_a_writer(cos, monkeypatch, domain) -> None:
    from sqlalchemy import text

    from app.core.local_owner import ensure_owner
    from app.db.models import WorkspaceMember
    from app.state import db
    from app.workspaces import resolve

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    owner = ensure_owner()
    desk = resolve.ensure_desk()
    engine = db().kw["bind"]

    def short_wait(connection, record, proxy):
        connection.execute("PRAGMA busy_timeout=25")

    event.listen(engine, "checkout", short_wait)
    try:
        with engine.connect() as writer:
            writer.execute(text("BEGIN IMMEDIATE"))
            writer.execute(text("UPDATE workspaces SET name=:name WHERE id=:id"), {"name": domain, "id": desk.id})
            with ThreadPoolExecutor(max_workers=3) as executor:
                requests = [executor.submit(ensure_owner) for _ in range(3)]
                assert [request.result(timeout=2).id for request in requests] == [owner.id] * 3
            assert resolve.ensure_desk().name == desk.name
            writer.rollback()
    finally:
        event.remove(engine, "checkout", short_wait)
    with db()() as session:
        member = session.scalars(select(WorkspaceMember).where(WorkspaceMember.workspace_id == desk.id)).one()
        assert member.person_id == owner.id
        assert member.role == "owner" and member.can_approve == 1


@pytest.mark.parametrize("domain", ["vanta.com", "ouraring.com"])
def test_first_account_requests_create_one_person(cos, domain) -> None:
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Person
    from app.state import db

    factory = db()
    subject = "usr_first_account"
    rendezvous = Barrier(2)

    def overlap_reads(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT people.") and "WHERE people.id =" in statement:
            try:
                rendezvous.wait(timeout=0.3)
            except BrokenBarrierError:
                pass

    engine = factory.kw["bind"]
    event.listen(engine, "after_cursor_execute", overlap_reads)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            requests = [executor.submit(ensure_subject_person, subject, email=f"you@{domain}") for _ in range(2)]
            assert [request.result() for request in requests] == [subject, subject]
    finally:
        event.remove(engine, "after_cursor_execute", overlap_reads)
    with factory() as session:
        people = session.scalars(select(Person).where(Person.id == subject)).all()
        assert len(people) == 1
        assert people[0].email == f"you@{domain}"


@pytest.mark.parametrize("domain", ["vanta.com", "ouraring.com"])
@pytest.mark.parametrize("explicit", [False, True])
def test_first_devices_share_one_desk(cos, domain, explicit) -> None:
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Workspace, WorkspaceMember
    from app.state import db
    from app.workspaces import resolve, service

    subject = ensure_subject_person("usr_first_devices", email=f"you@{domain}")
    factory = db()
    rendezvous = Barrier(2)

    def overlap_reads(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT workspaces.") and "workspaces.kind =" in statement:
            try:
                rendezvous.wait(timeout=0.3)
            except BrokenBarrierError:
                pass

    def open_desk():
        if explicit:
            try:
                return service.create_workspace(kind="desk", owner_id=subject).id
            except service.WorkspaceError as exc:
                assert exc.code == "desk_exists"
        return resolve.ensure_desk(subject).id

    engine = factory.kw["bind"]
    event.listen(engine, "after_cursor_execute", overlap_reads)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(resolve.ensure_desk, subject)
            second = executor.submit(open_desk)
            assert first.result().id == second.result()
    finally:
        event.remove(engine, "after_cursor_execute", overlap_reads)
    with factory() as session:
        desks = session.scalars(select(Workspace).where(Workspace.owner_id == subject, Workspace.kind == "desk")).all()
        assert len(desks) == 1
        members = session.scalars(select(WorkspaceMember).where(WorkspaceMember.workspace_id == desks[0].id)).all()
        assert len(members) == 1
        assert members[0].person_id == subject
        assert members[0].role == "owner"
    assert resolve.ensure_desk(subject).id == desks[0].id


def test_second_desk_is_refused(cos) -> None:
    from app.workspaces import resolve, service

    resolve.ensure_desk()
    with pytest.raises(service.WorkspaceError) as exc:
        service.create_workspace(kind="desk", name="Another")
    assert exc.value.code == "desk_exists"


def test_create_workspace_makes_owner_member(cos) -> None:
    from app.workspaces import service

    ws = service.create_workspace(kind="venture", name="Synths")
    members = service.members(ws.id)
    assert len(members) == 1
    assert members[0]["role"] == "owner"
    assert members[0]["can_approve"] is True
    assert service.role_of(ws.id, members[0]["person_id"]) == "owner"


def test_invalid_kind_and_entry_mode_are_rejected(cos) -> None:
    from app.workspaces import service

    with pytest.raises(service.WorkspaceError):
        service.create_workspace(kind="galaxy")
    with pytest.raises(service.WorkspaceError):
        service.create_workspace(kind="venture", entry_mode="telepathy")


def test_legacy_venture_without_workspace_is_adopted(cos) -> None:
    """A row written before Phase 10 still resolves, exactly once."""
    from app.db.models import Venture
    from app.state import db
    from app.workspaces import resolve
    from core.clock import utcnow
    from core.ids import new_id

    vid = new_id("ven")
    now = utcnow()
    factory = db()
    with factory() as session:
        session.add(
            Venture(
                id=vid,
                name="Legacy",
                raw_idea="an idea from before workspaces existed",
                stage="draft",
                autonomy_level="L2",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    ws = resolve.workspace_for_venture(vid)
    assert ws is not None
    assert ws.kind == "venture"
    assert ws.autonomy_level == "L2"
    # Idempotent: a second resolve returns the same workspace, not a duplicate.
    again = resolve.workspace_for_venture(vid)
    assert again is not None and again.id == ws.id


def test_unknown_venture_resolves_to_none(cos) -> None:
    from app.workspaces import resolve

    assert resolve.workspace_for_venture("ven_doesnotexist") is None


def test_observe_level_descent_blocks_in_flight_work(cos) -> None:
    from app.db.models import Engagement
    from app.state import db
    from app.workspaces import observe, service
    from core.clock import utcnow
    from core.ids import new_id

    ws = service.create_workspace(kind="company", entry_mode="existing_company")
    service.set_observe_level(ws.id, "O4")

    eid = new_id("eng")
    now = utcnow()
    factory = db()
    with factory() as session:
        session.add(
            Engagement(
                id=eid,
                workspace_id=ws.id,
                requested_by="per_local000000",
                size="task",
                title="Update the changelog",
                request_verbatim="update the changelog",
                definition_of_done="changelog updated",
                status="working",
                target_ref="github:acme/repo",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    service.set_observe_level(ws.id, "O1")
    cancelled = observe.cancel_work_above_level(ws.id, "O1")
    assert eid in cancelled or _status(eid) == "blocked"
    assert _status(eid) == "blocked"


def _status(engagement_id: str) -> str:
    from app.db.models import Engagement
    from app.state import db

    factory = db()
    with factory() as session:
        row = session.get(Engagement, engagement_id)
        assert row is not None
        return row.status
