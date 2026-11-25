"""Explicit synthetic founder/venture attribution for credit service tests."""
from uuid import uuid4


def ensure_test_wallet(venture_id=None):
    from app.credits._txn import naive_utc, session_factory
    from app.credits.accounts import ensure_account
    from app.db.models import Person, Venture, Workspace
    vid = venture_id or 'ven_' + uuid4().hex[:12]
    pid, wid = 'per_wallet:' + vid, 'ws_wallet:' + vid
    now = naive_utc()
    with session_factory()() as session:
        if session.get(Person, pid) is None:
            session.add(Person(id=pid, display_name='Synthetic founder', created_at=now))
            session.flush()
            session.add(Workspace(id=wid, owner_id=pid, kind='venture', name='Test', entry_mode='idea',
                                  created_at=now, updated_at=now))
            session.flush()
            session.add(Venture(id=vid, workspace_id=wid, name='Test', raw_idea='Test', stage='draft',
                                gtm_order=0, daily_token_budget=10000, created_at=now, updated_at=now))
            session.commit()
    wallet = ensure_account(vid)
    # Detached fixture-only attribution; production wallets have no venture_id.
    wallet.attribution_venture_id = vid
    return wallet
