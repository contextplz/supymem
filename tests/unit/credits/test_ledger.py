"""Hash chain, idempotency, and available_after_micro."""

from __future__ import annotations
from tests.credit_fixtures import ensure_test_wallet

import pytest
from sqlalchemy import text


def test_hash_chain_verifies(account) -> None:
    from app.credits.ledger import verify_chain

    ok, detail = verify_chain(account.id)
    assert ok, detail


def test_tampered_prev_hash_is_detected(account) -> None:
    from app.credits.ledger import GENESIS_HASH, verify_chain
    from app.state import db

    factory = db()
    with factory() as session:
        max_seq = session.execute(text("SELECT COALESCE(MAX(seq), 0) FROM credit_ledger")).scalar()
        session.execute(
            text(
                "INSERT INTO credit_ledger ("
                " id, seq, account_id, kind, amount_micro, available_after_micro,"
                " reason, actor_kind, idempotency_key, meta_json, prev_hash,"
                " entry_hash, created_at"
                ") VALUES ("
                " 'cle_tamper', :seq, :aid, 'adjust', 0, 0,"
                " 'tamper', 'system', 'tamper:1', '{}', 'not_the_previous_hash',"
                " 'deadbeef', CURRENT_TIMESTAMP)"
            ),
            {"seq": int(max_seq or 0) + 1, "aid": account.id},
        )
        session.commit()

    ok, detail = verify_chain(account.id)
    assert ok is False
    assert detail is not None
    assert "prev_hash" in detail
    assert GENESIS_HASH


def test_duplicate_idempotency_key_raises(account) -> None:
    from app.credits._txn import serialize_writes, session_factory
    from app.credits.errors import DuplicateLedgerEntry
    from app.credits.ledger import append
    from app.db.models import CreditAccount

    factory = session_factory()
    with factory() as session:
        serialize_writes(session, key=account.id)
        row = session.get(CreditAccount, account.id)
        assert row is not None
        append(
            session,
            account=row,
            kind="adjust",
            amount_micro=0,
            reason="once",
            actor_kind="system",
            actor_id="test",
            idempotency_key="dup:ledger",
        )
        with pytest.raises(DuplicateLedgerEntry):
            append(
                session,
                account=row,
                kind="adjust",
                amount_micro=0,
                reason="twice",
                actor_kind="system",
                actor_id="test",
                idempotency_key="dup:ledger",
            )
        session.rollback()


def test_seq_is_strictly_increasing_across_accounts(credits_env) -> None:
    """``seq`` is a global total order, and a collision must never be mistaken
    for a duplicate idempotency key — that would report an unapplied charge as
    already settled and silently drop it."""
    import uuid

    from app.credits.accounts import ensure_account
    from app.credits.grants import grant
    from app.credits.ledger import search

    first = ensure_test_wallet(f"ven_{uuid.uuid4().hex[:12]}")
    second = ensure_test_wallet(f"ven_{uuid.uuid4().hex[:12]}")
    for i, acct in enumerate((first, second, first, second)):
        grant(
            account_id=acct.id,
            amount_micro=10_000,
            reason="seq ordering probe",
            actor_kind="system",
            actor_id="test",
            idempotency_key=f"seq-probe:{i}",
        )

    rows = search(limit=200)["items"]
    seqs = [row.seq for row in rows]
    assert len(seqs) == len(set(seqs)), f"duplicate seq assigned: {seqs}"
    # search() returns newest first, so ascending order means monotonic writes.
    assert seqs == sorted(seqs, reverse=True)


def test_available_after_micro_matches_derived_balance(account) -> None:
    from app.credits.accounts import balance
    from app.credits.ledger import entries_for_account

    page = entries_for_account(account.id, limit=20)
    items = page["items"]
    assert items
    grant_row = next(row for row in items if row.kind == "grant")
    snap = balance(account)
    assert grant_row.available_after_micro == snap["available_micro"]
