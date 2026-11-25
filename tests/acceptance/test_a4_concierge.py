"""A4 — Concierge Stripe task."""

from __future__ import annotations

import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.concierge.planner import clear_tasks, create_task, list_open
from app.concierge.verify import store_in_vault_handle, validate_stripe_key


@pytest.mark.acceptance
def test_a4_stripe_task_and_vault():
    clear_tasks()
    task = create_task("stripe", venture_id="ven_a4")
    assert task["steps"]
    for step in task["steps"]:
        assert step["url"] and "stripe.com" in step["url"]
        assert step["ui_locator"] and step["expect_to_see"]
    assert validate_stripe_key("sk_live_abc", kind="secret").ok is False
    ok = validate_stripe_key("sk_test_abc123", kind="secret")
    assert ok.ok and ok.vault_handle
    stored = store_in_vault_handle("sk_test_abc123", ok.vault_handle)
    # Secrets never land in human_tasks.secret_fields (only vault handles)
    assert task.get("secret_fields") == []
    assert "sk_test_abc123" not in str(task)
    assert stored["handle"].startswith("vault:")
    assert len(list_open("ven_a4")) <= 5
