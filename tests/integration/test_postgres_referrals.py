"""Actual PostgreSQL last-slot race, with an isolated migrated local database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from sqlalchemy.orm import Session
from tests.integration.test_postgres_auth_security import pg_database as pg_database, service_modules


def test_last_slot_admits_exactly_one_distinct_person(pg_database):
    _, engine = pg_database
    with service_modules("company_os"):
        from app.consumer.referrals import admit, ReferralDenied, used
        from app.db.supymem_models import BetaCode, ReferralAdmission
        from app.db.session import serialize_writes
        with Session(engine) as session:
            session.add(BetaCode(code="PGRACE22", owner_person_id="pg_owner", label="race", active=True,
                max_uses=1, created_at=datetime.now(timezone.utc)))
            session.commit()
        barrier = Barrier(4)
        def attempt(number):
            barrier.wait(timeout=10)
            person = f"pg_referral_{number}"
            with Session(engine) as session:
                serialize_writes(session, key=f"referral-person:{person}")
                try:
                    admit(session, person_id=person, user_id=person, invite_code="PGRACE22", required=True)
                    session.commit()
                    return "admitted"
                except ReferralDenied:
                    session.rollback()
                    return "exhausted"
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt, range(4)))
        assert results.count("admitted") == 1
        assert results.count("exhausted") == 3
        with Session(engine) as session:
            assert used(session, "PGRACE22") == 1
            assert session.query(ReferralAdmission).filter(ReferralAdmission.person_id.like("pg_referral_%")).count() == 1
