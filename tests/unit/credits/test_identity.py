"""person_for_subject / person_for_email against a real SQLite DB."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def identity_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/identity.db")
    load_company_os_app()
    from app import state

    state.reset_db_factory()


def test_founder_maps_to_local_person(identity_env) -> None:
    from app.identity.resolve import person_for_subject
    from app.workspaces.resolve import LOCAL_PERSON_ID

    person = person_for_subject("founder")
    assert person.id == LOCAL_PERSON_ID
    again = person_for_subject(" founder ")
    assert again.id == LOCAL_PERSON_ID


def test_new_subject_creates_one_stable_person(identity_env) -> None:
    from app.db.models import Person, PersonIdentity
    from app.identity.resolve import person_for_subject
    from app.state import db

    first = person_for_subject("usr_alice", email="Alice@Example.com")
    second = person_for_subject("usr_alice")
    assert first.id == second.id
    assert first.id.startswith("per_")
    assert first.email == "alice@example.com"
    assert first.display_name == "alice"

    with db()() as session:
        n_ident = session.scalar(
            select(func.count())
            .select_from(PersonIdentity)
            .where(PersonIdentity.subject == "usr_alice")
        )
        n_people = session.scalar(
            select(func.count()).select_from(Person).where(Person.id == first.id)
        )
    assert n_ident == 1
    assert n_people == 1


def test_person_for_email_is_case_insensitive(identity_env) -> None:
    from app.identity.resolve import person_for_email, person_for_subject

    created = person_for_subject("usr_bob", email="Bob@Host.com")
    found = person_for_email("BOB@host.com")
    assert found is not None
    assert found.id == created.id
    assert person_for_email("nobody@host.com") is None


def test_known_identity_email_repairs_an_empty_person_on_next_resolution(identity_env):
    from app.db.models import Person
    from app.identity.resolve import person_for_subject
    from app.state import db

    person = person_for_subject("usr_cog_repair", email="repair@example.test")
    with db()() as session:
        session.get(Person, person.id).email = None
        session.commit()
    repaired = person_for_subject("usr_cog_repair")
    assert repaired.id == person.id
    assert repaired.email == "repair@example.test"


@pytest.mark.parametrize("existing", [False, True])
def test_request_email_repair_only_fills_missing_account_emails(identity_env, existing):
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Person, PersonIdentity
    from app.state import db
    from core.clock import utcnow

    subject = "usr_request_email"
    with db()() as session:
        session.add(Person(id=subject, display_name="Direct", created_at=utcnow(),
            email="captured@example.test" if existing else None))
        session.add(Person(id="per_request_email", display_name="Linked", created_at=utcnow(),
            email="contact@example.test" if existing else None))
        session.add(PersonIdentity(id="pid_request_email", person_id="per_request_email", subject=subject,
            email="account@example.test" if existing else None, created_at=utcnow()))
        session.commit()

    assert ensure_subject_person(subject, email="ASSERTION@example.test") == subject
    ensure_subject_person(subject, email="different@example.test")
    with db()() as session:
        assert session.get(Person, subject).email == ("captured@example.test" if existing else "assertion@example.test")
        assert session.get(Person, "per_request_email").email == ("contact@example.test" if existing else "assertion@example.test")
        assert session.get(PersonIdentity, "pid_request_email").email == ("account@example.test" if existing else "assertion@example.test")
