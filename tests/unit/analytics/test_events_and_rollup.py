"""Product analytics emit + idempotent daily rollup."""

from __future__ import annotations


import pytest

pytestmark = pytest.mark.hosted_only


def test_emit_and_rollup(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ana.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app import state

    state.reset_db_factory()
    from app.analytics.events import emit
    from app.analytics.rollup import latest_product_metrics, rollup_today

    assert emit("venture.created", person_id="per_a", venture_id="ven_a")
    assert emit("credits.topup", person_id="per_a", venture_id="ven_a")
    assert emit("not_a_real_event") is None
    row = rollup_today()
    assert row.first_ventures >= 1
    again = rollup_today()
    assert again.day == row.day
    metrics = latest_product_metrics()
    assert metrics["first_ventures"] >= 1


def test_first_occurrence_ordered_funnel_and_background_billing_are_not_activity(tmp_path, monkeypatch):
    from datetime import timedelta
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/cohorts.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app import state
    state.reset_db_factory()
    from app.analytics.events import emit
    from app.analytics.rollup import activation_funnel, rollup_today
    from app.db.models import AnalyticsEvent
    from core.clock import utcnow
    # An older founder creates another venture and tops up again today.
    with state.db()() as session:
        for name in ("venture.created", "credits.topup"):
            session.add(AnalyticsEvent(id="historic-" + name, person_id="old", event_name=name, props_json="{}", occurred_at=utcnow() - timedelta(days=40)))
        session.commit()
    emit("venture.created", person_id="old")
    emit("credits.topup", person_id="old")
    emit("signup", person_id="new")
    emit("venture.created", person_id="new")
    emit("venture.created", person_id="new")
    emit("credits.topup", person_id="new")
    emit("credits.topup", person_id="new")
    emit("billing.started", person_id="stub", props={"provider": "stub"})
    emit("credits.topup", person_id="background")
    row = rollup_today()
    assert row.first_ventures == 1  # one new founder, not four creation events
    assert row.first_paid == 2  # first observed top-ups; not the stub or old repeat
    assert row.dau == 2  # background billing alone does not establish activity
    assert activation_funnel() == {"signup": 1, "first_venture": 1, "first_paid": 1}
