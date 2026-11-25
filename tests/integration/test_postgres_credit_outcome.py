"""Real PostgreSQL: protection serializes against expiry and settlement replay."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


def test_protection_and_expiry_cannot_both_win(journey_db):
    from app.credits.accounts import ensure_account, get_account_by_id
    from app.credits.errors import CreditError
    from app.credits.reconcile import reconcile
    from app.credits.reserve import protect_outcome, reserve, settle, sweep_expired
    from core.clock import utcnow
    d = journey_db
    account = ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    hold = reserve(account_id=account.id, amount_micro=5000, purpose="pg.outcome")
    barrier = Barrier(2)
    def protect():
        barrier.wait(timeout=10)
        try:
            protect_outcome(hold)
            return True
        except CreditError:
            return False
    def expire():
        barrier.wait(timeout=10)
        return sweep_expired(utcnow() + timedelta(days=2))
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(protect)
        other = pool.submit(expire)
        protected, expired = future.result(timeout=30), other.result(timeout=30)
    assert (protected, expired) in {(True, 0), (False, 1)}
    assert get_account_by_id(account.id).reserved_micro == (5000 if protected else 0)
    if protected:
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(lambda _: settle(hold, actual_micro=1250), range(4)))
        assert len({r.ledger_id for r in receipts}) == 1
        assert get_account_by_id(account.id).spent_micro == account.spent_micro + 1250
    assert reconcile(account.id).ok
