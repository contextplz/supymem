"""Map a gateway subject (``usr_…`` or ``founder``) onto a ``Person`` row."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Person, PersonIdentity
from app.state import db
from app.workspaces.resolve import LOCAL_PERSON_ID
from core.clock import utcnow  # type: ignore[import-not-found]
from core.ids import new_id  # type: ignore[import-not-found]


def _session_factory() -> sessionmaker[Session]:
    return db()  # type: ignore[no-untyped-call,no-any-return]


def _norm_email(email: str | None) -> str | None:
    if email is None:
        return None
    stripped = email.strip().lower()
    return stripped or None


def _display_name(email: str | None) -> str:
    if email and "@" in email:
        local = email.split("@", 1)[0].strip()
        if local:
            return local
    return "Founder"


def _identity_by_subject(session: Session, subject: str) -> PersonIdentity | None:
    return session.scalars(
        select(PersonIdentity).where(PersonIdentity.subject == subject)
    ).first()


def _ensure_local_person(session: Session) -> Person:
    person = session.get(Person, LOCAL_PERSON_ID)
    if person is None:
        person = Person(
            id=LOCAL_PERSON_ID,
            display_name="Founder",
            email=None,
            created_at=utcnow(),
        )
        session.add(person)
        session.flush()
    return person


def _apply_email(
    person: Person, identity: PersonIdentity, email: str | None
) -> None:
    if email is None:
        return
    if identity.email != email:
        identity.email = email
    if person.email != email:
        person.email = email


def _load_person(session: Session, identity: PersonIdentity) -> Person:
    person = session.get(Person, identity.person_id)
    if person is None:
        raise RuntimeError(
            f"person_identities {identity.id} points at missing {identity.person_id}"
        )
    return person


def person_for_subject(subject: str, email: str | None = None) -> Person:
    """Return the Person for a gateway subject, creating one if needed.

    ``subject == "founder"`` (local passphrase mode) always maps to
    ``LOCAL_PERSON_ID`` so single-user desktop behaviour is unchanged.
    """
    subject = (subject or "").strip()
    if not subject:
        raise ValueError("subject is required")
    email_norm = _norm_email(email)
    factory = _session_factory()
    with factory() as session:
        person = _person_for_subject(session, subject, email_norm)
        session.commit()
        session.refresh(person)
        session.expunge(person)
        return person


def _person_for_subject(
    session: Session, subject: str, email: str | None
) -> Person:
    identity = _identity_by_subject(session, subject)
    if identity is not None:
        person = _load_person(session, identity)
        _apply_email(person, identity, email or identity.email)
        return person

    if subject == "founder":
        person = _ensure_local_person(session)
        identity = PersonIdentity(
            id=new_id("pid"),
            person_id=person.id,
            subject=subject,
            email=email,
            created_at=utcnow(),
        )
        return _insert_identity(session, person, identity, subject, email)

    person = Person(
        id=new_id("per"),
        display_name=_display_name(email),
        email=email,
        created_at=utcnow(),
    )
    identity = PersonIdentity(
        id=new_id("pid"),
        person_id=person.id,
        subject=subject,
        email=email,
        created_at=utcnow(),
    )
    return _insert_identity(session, person, identity, subject, email)


def _insert_identity(
    session: Session,
    person: Person,
    identity: PersonIdentity,
    subject: str,
    email: str | None,
) -> Person:
    try:
        session.add(person)
        session.add(identity)
        session.flush()
        return person
    except IntegrityError:
        session.rollback()
        existing = _identity_by_subject(session, subject)
        if existing is None:
            raise
        person = _load_person(session, existing)
        _apply_email(person, existing, email)
        return person


def person_for_email(email: str) -> Person | None:
    """Resolve a Person by identity email (case-insensitive). Admin grant path."""
    email_norm = _norm_email(email)
    if email_norm is None:
        return None
    factory = _session_factory()
    with factory() as session:
        identity = session.scalars(
            select(PersonIdentity)
            .where(func.lower(PersonIdentity.email) == email_norm)
            .order_by(PersonIdentity.created_at)
        ).first()
        if identity is None:
            return None
        person = _load_person(session, identity)
        session.expunge(person)
        return person


def identities_for_email(email: str) -> list[PersonIdentity]:
    """Every identity row sharing this email (case-insensitive)."""
    email_norm = _norm_email(email)
    if email_norm is None:
        return []
    factory = _session_factory()
    with factory() as session:
        rows = list(
            session.scalars(
                select(PersonIdentity)
                .where(func.lower(PersonIdentity.email) == email_norm)
                .order_by(PersonIdentity.created_at)
            ).all()
        )
        for row in rows:
            session.expunge(row)
        return rows
