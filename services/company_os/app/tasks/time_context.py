"""Read timezone only from the authenticated principal's person record."""
from app.authz.workspace import subject_person_id
from app.db.models import Person
from app.state import db


def founder_timezone(subject: str | None) -> str | None:
    if not subject or subject in {"anonymous", "system"}:
        return None
    with db()() as session:
        person = session.get(Person, subject_person_id(subject))
        # The console saves the browser's zone on sign-in; until then say the zone is unconfirmed (UTC), never guess.
        return person.timezone if person else None
