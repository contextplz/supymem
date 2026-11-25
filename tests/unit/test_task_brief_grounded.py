"""Grounded task briefs do not need a model."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

import asyncio
from types import SimpleNamespace

import pytest
from tests.unit.credits.conftest import credits_env


@pytest.mark.parametrize("idea", ["Warehouse delivery handovers", "Family swimming lessons"])
@pytest.mark.parametrize("exhausted", [False, True])
def test_work_brief_uses_scoped_meter_and_never_calls_provider_at_limit(credits_env, monkeypatch, idea, exhausted):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.planning import summarize
    from app.credits import estimate, metered
    from app.credits.ledger import append, verify_chain
    from app.db.models import CreditAccount, CreditReservation
    from app.llm import founder_provider
    from app.state import db
    from app.tenancy import bind
    from llm_gateway import provider_foundry, sole
    from tests.credit_fixtures import ensure_test_wallet

    monkeypatch.setenv("LLM_OFFLINE", "0")
    wallet = ensure_test_wallet("venture")
    workspace_id = "ws_wallet:venture"
    facts = {"venture": {"id": "venture", "workspace_id": workspace_id, "idea": idea},
             "title": "Review the proposal", "task": {"id": "task", "runnable": False}, "phase": "queued"}
    calls, provider_calls, direct_calls = [], [], []
    monkeypatch.setattr(estimate, "pricing_table", lambda: {"gpt-6.1-sol": {
        "input_per_mtok": "2", "output_per_mtok": "10", "cached_input_per_mtok": "0.1"}})
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: SimpleNamespace(source="platform_default"))
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: provider_foundry.ProviderConfig(
        deployment_pro="gpt-6.1-sol", deployment_flash="gpt-6.1-sol"))
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="Your proposal is saved for review. You can read the requirements in Work."))],
        usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
               "prompt_tokens_details": {"cached_tokens": 8}, "completion_tokens_details": {"reasoning_tokens": 0}})

    async def direct(*args, **kwargs):
        direct_calls.append(kwargs)
        return response

    async def provider(*args, **kwargs):
        provider_calls.append(kwargs)
        with db()() as session:
            assert session.get(CreditAccount, wallet.id).reserved_micro > 0
        return response

    original_llm = metered.llm

    async def admitted(*args, **kwargs):
        calls.append(kwargs)
        return await original_llm(*args, **kwargs)

    with db()() as session:
        account = session.get(CreditAccount, wallet.id)
        if exhausted:
            amount = account.granted_micro - account.spent_micro
            account.spent_micro += amount
            append(session, account=account, kind="spend", amount_micro=amount,
                reason="Prior test usage", actor_kind="system", actor_id="test", idempotency_key="spent-wallet")
            session.commit()
        initial_spend = account.spent_micro

    monkeypatch.setattr(sole, "env_chat_completion", direct)
    monkeypatch.setattr(sole, "transmit", provider)
    monkeypatch.setattr(metered, "llm", admitted)
    with bind(workspace_id=workspace_id, venture_id="venture", actor=wallet.owner_person_id):
        written = asyncio.run(summarize._llm_brief(facts))
    assert (written is None) == exhausted
    assert not direct_calls and len(provider_calls) == (0 if exhausted else 1)
    assert len(calls) == 1
    assert calls[0]["venture_id"] == "venture" and calls[0]["workspace_id"] == workspace_id
    assert calls[0]["max_tokens"] == 500
    with db()() as session:
        account = session.get(CreditAccount, wallet.id)
        assert account.reserved_micro == 0
        assert account.spent_micro - initial_spend == (0 if exhausted else 205)
        assert verify_chain(wallet.id, transaction=session)[0]
        assert session.query(CreditReservation).filter_by(account_id=wallet.id, status="active").count() == 0


@pytest.mark.parametrize("idea", ["Warehouse delivery handovers", "Family swimming lessons"])
def test_work_briefs_are_plain_and_give_state_grounded_next_step(monkeypatch, idea):
    load_company_os_app()
    from app.planning import summarize
    from app.state import MEM

    facts = {"venture": {"id": "venture", "idea": idea}, "title": "Review the private proposal",
             "task_id": "task", "task": {"id": "task", "runnable": False}, "phase": "queued",
             "owner_name": "Cole", "owner_role": "build",
             "detail": "The destination is missing. Keep the proposal private.",
             "why_now": "You need a review—nothing is published."}
    MEM.clear()
    monkeypatch.setattr(summarize, "collect_facts", lambda *args, **kwargs: facts)

    async def written(*args):
        return "You need a review—nothing is published. Your private proposal is not ready to run."

    monkeypatch.setattr(summarize, "_llm_brief", written)
    reply = asyncio.run(summarize.summarize_task("venture", task_id="task"))
    assert "—" not in reply["body_md"]
    assert "Next step" in reply["body_md"] and "Work" in reply["body_md"]
    assert "start" not in reply["body_md"].split("Next step")[-1].lower()
    grounded = summarize.render_grounded(facts)
    assert "—" not in grounded and "Next step" in grounded and "(build)" not in grounded
    MEM[summarize._cache_key("venture", "task")] = {**reply,
        "body_md": "You have saved this work.\n\n" + summarize._next_step({**facts, "task": {"runnable": True}})}
    updated = asyncio.run(summarize.summarize_task("venture", task_id="task"))
    assert not updated["cached"]
    assert "choose Run" not in updated["body_md"] and "manual result" in updated["body_md"]


def test_clean_heartbeat_titles():
    load_company_os_app()
    from app.planning.store import clean_task_title

    assert clean_task_title("Just finished Test a paid offer") == "Test a paid offer"
    assert clean_task_title("Working on Recruit design partners") == "Recruit design partners"
    assert clean_task_title("Recruit design partners") == "Recruit design partners"


def test_grounded_queued_brief_explains_the_work():
    load_company_os_app()
    from app.planning.summarize import render_grounded

    text = render_grounded(
        {
            "title": "Recruit build ai-powered form design partners",
            "phase": "queued",
            "owner_name": "Cole",
            "owner_role": "Builder",
            "why_now": "We need people who will actually try the offer.",
            "est_minutes": 60,
            "venture": {"name": "Form Operator", "idea": "AI-powered forms"},
        }
    )
    low = text.lower()
    assert "cole" in low
    assert "saved for your review" in low and "nothing starts automatically" in low
    assert "design partner" in low
    assert "form operator" in low
    assert "why this matters now" in low


def test_grounded_running_brief_says_in_flight():
    load_company_os_app()
    from app.planning.summarize import render_grounded

    text = render_grounded(
        {
            "title": "Instrument the activation funnel",
            "phase": "in_progress",
            "owner_name": "Cole",
            "venture": {"name": "Form Operator", "idea": "AI-powered forms"},
        }
    )
    assert "doing this now" in text.lower()
    assert "Instrument" in text
