"""Fresh-start person wallets; apply requires the same writer drain as cutover."""

import argparse
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.credits._txn import naive_utc, serialize_writes, session_factory
from app.credits.accounts import _credits_number, exempt_person_ids
from app.credits.errors import CreditError
from app.credits.ledger import append, find_by_idempotency_key
from app.credits.policy import WALLET_PHASE_KEY, person_wallet_enabled
from app.credits.reconcile import reconcile
from app.credits.units import credits_to_micro
from app.db.models import CreditAccount

RESET_PREFIX = "reset:2026-10-09:"


def reset_account(session: Session, account: CreditAccount, *, key: str | None = None,
                  target_micro: int | None = None) -> dict[str, object]:
    """Caller owns the transaction and account lock; holds never move."""
    key = key or RESET_PREFIX + account.id
    target = target_micro if target_micro is not None else credits_to_micro(
        50_000 if account.owner_person_id in exempt_person_ids() else 5_000)
    before = account.granted_micro - account.spent_micro - account.reserved_micro
    skipped = "frozen" if account.status == "frozen" else (
        "already_reset" if find_by_idempotency_key(key, session) else None)
    delta = 0 if skipped else target - before
    if not skipped:
        account.granted_micro += delta
        account.version += 1
        account.updated_at = naive_utc()
        append(session, account=account, kind="adjust", amount_micro=delta,
               reason="fresh start reset", purpose="admin.reset", actor_kind="system",
               actor_id="system", idempotency_key=key, meta={"target_micro": target})
    return {"account_id": account.id, "before_available": _credits_number(before),
            "target": _credits_number(target), "delta": _credits_number(delta), "skipped": skipped}


def reset_balances(session: Session) -> dict[str, object]:
    serialize_writes(session, key="founder-wallet-migration")
    serialize_writes(session, key=WALLET_PHASE_KEY)
    if not person_wallet_enabled(session):
        raise CreditError("wallet_reset_requires_person_phase")
    rows = []
    for account_id in session.scalars(select(CreditAccount.id).where(
            CreditAccount.scope == "person").order_by(CreditAccount.id)):
        serialize_writes(session, key=account_id)
        account = session.scalar(select(CreditAccount).where(CreditAccount.id == account_id)
            .with_for_update().execution_options(populate_existing=True))
        row = reset_account(session, account)
        if not row["skipped"]:
            check = reconcile(account_id, transaction=session)
            row.update(verify_chain=check.chain.passed, reconcile=check.ok)
        rows.append(row)
    touched = [row for row in rows if not row["skipped"]]
    return {"accounts": rows, "skipped": [row for row in rows if row["skipped"]],
            "totals": {"touched": len(touched), "skipped": len(rows) - len(touched),
                       "before_available": sum(row["before_available"] for row in touched),
                       "target": sum(row["target"] for row in touched),
                       "delta": sum(row["delta"] for row in touched)},
            "ok": all(row["verify_chain"] and row["reconcile"] for row in touched)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    report = {"mode": "apply" if args.apply else "dry-run", "committed": False, "ok": False}
    try:
        from app.state import get_factory, set_factory
        if get_factory() is None:
            from app.config import settings
            from app.db.session import make_engine, make_session_factory, normalize_db_url, resolve_database_url
            set_factory(make_session_factory(make_engine(normalize_db_url(resolve_database_url() or settings.db_url))))
        with session_factory()() as session:
            report.update(reset_balances(session))
            if args.apply and report["ok"]:
                session.commit()
                report["committed"] = True
            else:
                session.rollback()
    except Exception as exc:
        report["ok"] = False
        report["error"] = "wallet_reset_requires_person_phase" if (
            isinstance(exc, CreditError) and str(exc) == "wallet_reset_requires_person_phase"
        ) else "wallet_reset_failed"
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
