import asyncio
import logging

import pytest

from tests.unit.chat.test_round3_matrix import venture


IDEAS = [
    ("AINest", "A place where new founders explain a startup idea and get help developing it."),
    ("Stock Ledger", "Inventory alerts for warehouse operators who miss stock shortages."),
    ("Plant Journal", "A watering reminder for home gardeners who forget their plants."),
]


@pytest.fixture(params=IDEAS, ids=[idea[0] for idea in IDEAS])
def fresh_venture(venture, request):
    from app.credits.accounts import ensure_account
    from app.credits.units import credits_to_micro
    from app.db.models import CreditAccount, Venture
    from app.state import db

    account = ensure_account(venture[1])
    with db()() as session:
        saved = session.get(Venture, venture[1])
        saved.name, saved.raw_idea = request.param
        if account is not None:
            wallet = session.get(CreditAccount, account.id)
            wallet.granted_micro = wallet.spent_micro + wallet.reserved_micro + credits_to_micro(40_001)
        session.commit()
    return venture


def test_fresh_venture_waits_for_intake_without_model_or_charge(fresh_venture, monkeypatch):
    for autopilot in (False, True):
        with monkeypatch.context() as patch:
            _assert_pending_intake(fresh_venture, patch, autopilot)


def _assert_pending_intake(fresh_venture, monkeypatch, autopilot):
    from app.chat import market_beliefs
    from app.credits.accounts import balance, ensure_account, get_account_by_id
    from app.credits.policy import set_policy
    from app.experiments import belief_autopilot, beliefs
    from app.ventures.current_idea import intake_pending

    _, venture_id, _ = fresh_venture
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings

    if is_local():
        update_settings("capabilities", {"run_for_me_experiments": autopilot}, {})
    set_policy("feature_flags", {"experiments_autopilot_v1": autopilot}, updated_by="test")
    assert belief_autopilot.enabled() is autopilot
    assert intake_pending(venture_id)
    account = ensure_account(venture_id)
    before = balance(account) if account is not None else None

    async def forbidden(*args, **kwargs):
        pytest.fail("Pending intake must not call the model or prepare paid work")

    monkeypatch.setattr(beliefs.metered, "llm", forbidden)
    with pytest.raises(beliefs.IdeaIntakePending):
        asyncio.run(beliefs.generate(venture_id))
    reply = asyncio.run(market_beliefs.prepare(venture_id, {"day_one": True, "max_emails_per_day": 0}))
    assert "credits" not in reply["body_md"].lower()
    assert "model connection" not in reply["body_md"].lower()
    _assert_intake_step(reply)
    assert beliefs.load(venture_id)["beliefs"] == []
    if account is not None:
        assert balance(get_account_by_id(account.id)) == before
    else:
        assert ensure_account(venture_id) is None


def _assert_intake_step(reply):
    # 1782201d2 sends pending intake to the inline startup review, not an action card.
    if reply["kind"] == "startup_review":
        assert not reply["actions"] and "priorities" in reply["body_md"]
    else:
        assert reply["actions"]
    assert all(not action.get("action", "").startswith("experiment.") for action in reply["actions"])


def test_preparation_failures_are_truthful_logged_and_retryable(fresh_venture, monkeypatch, caplog):
    for error_kind in ("generic", "credits", "model", "policy"):
        caplog.clear()
        with monkeypatch.context() as patch:
            _assert_preparation_failure(fresh_venture, patch, caplog, error_kind)


def _assert_preparation_failure(fresh_venture, monkeypatch, caplog, error_kind):
    from app.chat import market_beliefs
    from app.chat.actions import get_action
    from app.credits.errors import CreditsUnavailable, InsufficientCredits, LIMIT_MESSAGE
    from app.experiments import belief_autopilot, beliefs
    from app.ventures import current_idea
    from llm_gateway.errors import ProviderCallError

    _, venture_id, _ = fresh_venture
    errors = {"generic": RuntimeError("private idea text and secret-key"),
        "credits": InsufficientCredits("account", 0, 10),
        "model": ProviderCallError("provider_down", "secret-key"),
        "policy": CreditsUnavailable("experiment plan disabled")}
    monkeypatch.setattr(current_idea, "intake_pending", lambda *args, **kwargs: False)
    monkeypatch.setattr(belief_autopilot, "enabled", lambda: False)

    async def fail(*args, **kwargs):
        raise errors[error_kind]

    monkeypatch.setattr(beliefs, "generate", fail)
    with caplog.at_level(logging.ERROR):
        # Nobody asked for the automatic card, so its failure is logged and never shown.
        assert asyncio.run(market_beliefs.prepare(venture_id, {"day_one": True, "max_emails_per_day": 0})) is None
    assert any(record.getMessage() == "founder_action_failed" for record in caplog.records)
    caplog.clear()
    payload = {"max_emails_per_day": 0}
    with caplog.at_level(logging.ERROR):
        reply = asyncio.run(market_beliefs.prepare(venture_id, payload))
    body = reply["body_md"].lower()
    assert "credits" not in body
    assert ("model" in body) is (error_kind == "model")
    if error_kind == "credits":
        assert reply["body_md"] == LIMIT_MESSAGE
        assert reply["actions"] == [{"label": "Buy more", "href": f"/v/{venture_id}/settings#settings-credits", "style": "primary", "kind": "recovery_link"}]
        assert any(record.venture_id == venture_id and (record.exc_info or record.exc_text)
            for record in caplog.records if record.getMessage() == "founder_action_failed")
        assert "secret-key" not in caplog.text and "private idea text" not in caplog.text
        return
    assert "nothing was launched" in body and "idea is saved" in body
    assert "\u2014" not in body and "secret-key" not in body
    retry = next(action for action in reply["actions"] if action["label"] == "Retry")
    assert get_action(retry["action"]) is not None
    assert retry["payload"] == payload
    assert any(record.venture_id == venture_id and record.action == retry["action"] and (record.exc_info or record.exc_text)
        for record in caplog.records if record.getMessage() == "founder_action_failed")
    assert "secret-key" not in caplog.text and "private idea text" not in caplog.text
    monkeypatch.setattr(beliefs, "generate", forbidden_generation)
    monkeypatch.setattr(current_idea, "intake_pending", lambda *args, **kwargs: True)
    retried = asyncio.run(market_beliefs.execute(venture_id, retry["action"], retry["payload"]))
    assert "credits" not in retried["body_md"].lower()
    _assert_intake_step(retried)


async def forbidden_generation(*args, **kwargs):
    pytest.fail("Retry must honor pending intake")


def test_stale_day_one_job_does_not_auto_prepare_pending_intake(fresh_venture, monkeypatch):
    monkeypatch.setenv("SUPYMEM_DAY_ONE_PLAN_CARD", "1")  # production keeps this card off
    for autopilot in (False, True):
        with monkeypatch.context() as patch:
            _assert_stale_day_one_job(fresh_venture, patch, autopilot)
    from app.chat import market_beliefs
    from app.chat.store import list_messages
    from app.db.models import Workspace
    from app.entry import chat_start
    from app.experiments import belief_autopilot
    from app.state import db
    from app.ventures import current_idea

    _, venture_id, workspace_id = fresh_venture
    with db()() as session:
        actor = session.get(Workspace, workspace_id).owner_id
    monkeypatch.setattr(current_idea, "intake_pending", lambda *args, **kwargs: False)
    monkeypatch.setattr(belief_autopilot, "enabled", lambda: True)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("private worker failure")

    monkeypatch.setattr(market_beliefs, "prepare", unavailable)
    with pytest.raises(RuntimeError, match="private worker failure"):
        asyncio.run(chat_start.run_exploration({"venture_id": venture_id, "workspace_id": workspace_id,
            "actor_id": actor, "proposal_id": "failed-day-one"}))
    failed = next(message for message in list_messages(venture_id) if message["kind"] == "experiment_failed")
    assert any(action.get("label") == "Retry" and action.get("action") == "status" for action in failed["actions"])
    assert not any(word in failed["body_md"].lower() for word in ("credits", "model connection", "private worker failure"))


def _assert_stale_day_one_job(fresh_venture, monkeypatch, autopilot):
    monkeypatch.setenv("SUPYMEM_DAY_ONE_PLAN_CARD", "1")  # production keeps this card off
    from app.chat import market_beliefs, next_moves
    from app.credits.policy import set_policy
    from app.db.models import Workspace
    from app.entry import chat_start
    from app.experiments import exploration
    from app.state import db

    _, venture_id, workspace_id = fresh_venture
    set_policy("feature_flags", {"experiments_autopilot_v1": autopilot, "exploration_v1": True}, updated_by="test")
    with db()() as session:
        actor = session.get(Workspace, workspace_id).owner_id
    monkeypatch.setattr(market_beliefs, "prepare", forbidden_generation)
    monkeypatch.setattr(exploration, "act", forbidden_generation)
    payload = {"venture_id": venture_id, "workspace_id": workspace_id,
        "actor_id": actor, "proposal_id": "stale-day-one"}
    asyncio.run(chat_start.run_exploration(payload))
    assert next_moves.experiment_learning(venture_id) is None
    card = next_moves.compose(venture_id)
    assert card["actions"] and not any(action.get("action", "").startswith("experiment.") for action in card["actions"])
