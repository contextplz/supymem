"""Reconciliation invariants and drift reporting."""

from __future__ import annotations

from sqlalchemy import text


def test_healthy_account_reconciles(account) -> None:
    from app.credits.reconcile import reconcile

    report = reconcile(account.id)
    assert report.ok
    assert report.ledger_sum.passed
    assert report.chain.passed
    assert report.reservations.passed
    assert report.ledger_sum.drift == 0
    assert report.reservations.drift == 0


def test_drifted_account_reports_drift(account) -> None:
    from app.credits.reconcile import reconcile
    from app.state import db

    factory = db()
    with factory() as session:
        session.execute(
            text(
                "UPDATE credit_accounts"
                " SET reserved_micro = reserved_micro + 1"
                " WHERE id = :id"
            ),
            {"id": account.id},
        )
        session.commit()

    report = reconcile(account.id)
    assert report.ok is False
    assert report.reservations.passed is False
    assert abs(report.reservations.drift) == 1
    assert report.reservations.expected == 1
    assert report.reservations.actual == 0
    assert report.ledger_sum.passed
    assert report.chain.passed
