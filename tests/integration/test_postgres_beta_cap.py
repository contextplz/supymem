"""Beta seat cap holds under concurrent admissions on real PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from sqlalchemy.orm import Session
from tests.integration.test_postgres_auth_security import pg_database as pg_database, service_modules


def test_beta_cap_holds_under_concurrent_admissions(pg_database, monkeypatch):
    _, engine = pg_database
    monkeypatch.setenv("SUPYMEM_BETA_MAX_FOUNDERS", "2")
    with service_modules("company_os"):
        from app.consumer.referrals import admit, ReferralDenied, seats_taken
        from app.db.supymem_models import BetaCode
        from app.db.session import serialize_writes
        with Session(engine) as session:
            session.add(BetaCode(code="PGCAP222", owner_person_id="pg_cap_owner", label="cap", active=True,
                max_uses=None, created_at=datetime.now(timezone.utc)))
            session.commit()
        barrier = Barrier(5)

        def attempt(number):
            barrier.wait(timeout=10)
            person = f"pg_cap_{number}"
            with Session(engine) as session:
                serialize_writes(session, key=f"referral-person:{person}")
                try:
                    admit(session, person_id=person, user_id=person, invite_code="PGCAP222", required=True)
                    session.commit()
                    return "admitted"
                except ReferralDenied as exc:
                    session.rollback()
                    return str(exc)
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(attempt, range(5)))
        assert results.count("admitted") == 2 and results.count("beta_full") == 3
        with Session(engine) as session:
            assert seats_taken(session) == 2
