"""Rehearse or commit the founder wallet transfer; apply requires drained writers."""
import argparse
import json

from sqlalchemy import select

from app.credits._txn import session_factory
from app.credits.errors import CreditError
from app.credits.migration import migrate_wallets
from app.credits.reconcile import reconcile
from app.db.models import CreditAccount


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    report = {"mode": "apply" if args.apply else "dry-run", "committed": False, "ok": False,
              "wallets": 0, "transferred_micro": 0, "expired_holds": 0, "deferred_accounts": [], "accounts": []}
    try:
        from app.state import get_factory, set_factory
        if get_factory() is None:
            from app.config import settings
            from app.db.session import make_engine, make_session_factory, normalize_db_url, resolve_database_url
            set_factory(make_session_factory(make_engine(normalize_db_url(resolve_database_url() or settings.db_url))))
        with session_factory()() as session:
            report.update(migrate_wallets(session))
            checks = []
            for account_id in session.scalars(select(CreditAccount.id).order_by(CreditAccount.id)):
                check = reconcile(account_id, transaction=session)
                checks.append({"account_id": account_id, "verify_chain": check.chain.passed,
                               "reconcile": check.ok})
            report["accounts"] = checks
            report["ok"] = all(check["verify_chain"] and check["reconcile"] for check in checks)
            if args.apply and report["ok"]:
                session.commit()
                report["committed"] = True
            else:
                session.rollback()
    except Exception as exc:
        report["ok"] = False
        code = str(exc) if isinstance(exc, CreditError) else ""
        report["error"] = code if code.startswith("wallet_migration_") and code.isidentifier() else "wallet_cutover_failed"
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
