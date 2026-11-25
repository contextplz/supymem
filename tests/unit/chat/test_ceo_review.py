import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.unit.chat.test_chat_request_status import chat_request
from tests.unit.memory.test_facts import facts


CASES = [
    ("AINest", "Teams buying AI agents for repeated tasks", "An AI agent marketplace that earns a small commission per completed task",
     "Buyers may not trust agents to finish paid tasks", "Complete five paid tasks for two teams in seven days"),
    ("StockWatch", "Warehouse operators", "Inventory alerts that catch missing stock before the daily shipment",
     "Your alerts may arrive too late to help warehouse operators", "Run alerts beside the stock sheet for one warehouse for seven days"),
    ("WalkTogether", "Families looking for nearby walking groups", "A weekend walking group for families who want to meet neighbors",
     "Families may sign up but not come back", "Host two walks for five families over two weekends"),
]


def _save_review_case(chat, case):
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from core.clock import utcnow
    from tests.company_os_harness import append_idea_revision

    chat["case"] = case
    name, customer, offer, _risk, _test = case
    fields = {"customer": customer, "offer": offer, "problem": "Your current workaround takes time",
              "pricing_hypothesis": "A small commission per completed task, including the complete saved price detail",
              "founder_facts": {}}
    with chat["factory"]() as session:
        venture = session.get(Venture, chat["vid"])
        venture.name, venture.raw_idea = name, offer
        row = session.get(IdeaSession, "idea_pressure")
        if row is None:
            row = IdeaSession(id="idea_pressure", workspace_id=chat["wid"], venture_id=chat["vid"],
                state="confirmed", case="know", created_at=utcnow(), updated_at=utcnow())
            session.add(row)
            session.flush()
            session.add(IdeaRevision(id="revision_pressure", session_id=row.id, workspace_id=chat["wid"],
                venture_id=chat["vid"], rev=1, source="founder", summary=offer, structured=json.dumps(fields),
                diff_from_prev="{}", created_at=utcnow()))
            row.current_revision_id = "revision_pressure"
        else:
            append_idea_revision(session, row.current_revision_id, structured=json.dumps(fields), summary=offer)
        session.commit()


@pytest.fixture
def review_env(chat_request, monkeypatch):
    from app.credits.accounts import ensure_account
    from app.credits import metered
    from app.llm import founder_provider
    from llm_gateway import provider_foundry

    chat = chat_request
    config = provider_foundry.ProviderConfig(provider="openai_compatible", base_url="https://offline.invalid/v1",
        deployment_pro="gpt-4o", deployment_flash="gpt-4o")
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: config)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda workspace_id:
        SimpleNamespace(source="platform_default"))
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setenv("LLM_CREDITS_REQUIRE_GATE", "1")
    monkeypatch.setenv("SUPYMEM_CREDITS_ENABLED", "1")
    account = ensure_account(chat["vid"])
    chat.update(calls=[], account_id=account.id, case=CASES[0], error=None, leak=False, finish_reason="stop")

    def save(case):
        _save_review_case(chat, case)

    async def offline(messages, **kwargs):
        chat["calls"].append((messages, kwargs))
        if chat["error"]:
            raise chat["error"]
        _name, customer, _offer, risk, next_test = chat["case"]
        inputs = json.loads(messages[-1]["content"])
        source = inputs["founder_revision_source"]
        report = {"verdict": "proceed_with_test", "summary": "Test the riskiest part before building more.",
            "findings": [{"method": "gstack_ceo_review", "assessment": risk, "source_ids": [source["id"]]}],
            "gaps": [], "questions": [], "strengths": [], "biggest_risk": risk, "forcing_questions": [],
            "risks": [{"risk": risk, "supports": "At least three of five people refuse to use it after trying it.",
                       "disproves": "At least three of five people use it again without a reminder."},
                      {"risk": f"You may spend more reaching {customer} than you earn.",
                       "supports": "You spend more to find each paying customer than that customer pays in a month.",
                       "disproves": "Two customers pay more than the time and money spent reaching them."}],
            "proposed_tests": [{"hypothesis": f"{customer} will use your offer twice.", "action": next_test,
                                "success_measure": "Pass if three of five use it twice; fail if fewer than three do."}]}
        if chat["leak"]:
            from app.gstack.methodology import CEO_PRIME_DIRECTIVES
            report["risks"][0]["supports"] = CEO_PRIME_DIRECTIVES[0][:800]
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=chat["finish_reason"],
            message=SimpleNamespace(content=json.dumps(report)))],
            usage=SimpleNamespace(prompt_tokens=900, completion_tokens=600, total_tokens=1500))

    monkeypatch.setattr(metered, "_transmit", offline)
    chat["save"] = save
    save(CASES[0])
    return chat


@pytest.mark.parametrize("case", CASES[1:], ids=[case[0] for case in CASES[1:]])
def test_saved_review_changes_append_with_hosted_history_guard(facts, monkeypatch, case):
    from app.db.append_only import _install_sqlite
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from sqlalchemy import select

    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    chat = {"factory": facts.factory, "wid": "wsp_owned", "vid": "ven_owned"}
    _save_review_case(chat, CASES[0])
    with facts.factory() as session:
        _install_sqlite(session.connection(), "idea_revisions")
        original = session.get(IdeaRevision, "revision_pressure").structured
        session.commit()
    _save_review_case(chat, case)
    with facts.factory() as session:
        idea = session.get(IdeaSession, "idea_pressure")
        current = session.get(IdeaRevision, idea.current_revision_id)
        assert current.id != "revision_pressure" and current.rev == 2
        assert json.loads(current.structured)["customer"] == case[1]
        assert session.get(IdeaRevision, "revision_pressure").structured == original
        assert len(session.scalars(select(IdeaRevision).where(IdeaRevision.session_id == idea.id)).all()) == 2


def prepare(chat):
    from app.chat.execute import _ceo_review
    from app.chat.conversations import bind_conversation
    from app.chat.store import append_message
    from app.tenancy import bind

    with bind(workspace_id=chat["wid"], venture_id=chat["vid"], actor=chat["actor"]):
        with bind_conversation(chat["vid"], chat["cid"]):
            prepared = asyncio.run(_ceo_review(chat["vid"]))
            assert prepared["kind"] == "confirmation", prepared
            return append_message(chat["vid"], role="assistant", **{key: prepared[key] for key in ("body_md", "kind", "actions", "citations")})


def confirm(chat, card, payload=None):
    path = f"/ventures/{chat['vid']}/chat/confirm"
    return chat["client"].post(path, headers=chat["headers"](path, "POST"), json={
        "action": "ceo_review", "message_id": card["id"],
        "conversation_id": card["conversation_id"],
        "payload": payload if payload is not None else card["actions"][0]["confirmation"]["resulting_state"]})


def usage(chat):
    from app.db.models import CreditAccount, CreditLedgerEntry
    with chat["factory"]() as session:
        account = session.get(CreditAccount, chat["account_id"])
        entries = session.query(CreditLedgerEntry).filter_by(account_id=account.id).all()
        return account.granted_micro - account.spent_micro - account.reserved_micro, account.reserved_micro, [
            (row.id, row.kind, row.amount_micro) for row in entries]


@pytest.mark.parametrize("case", CASES[1:], ids=[case[0] for case in CASES[1:]])
def test_next_move_click_runs_requested_review_once_without_a_second_confirmation(review_env, case):
    from app.db.models import CreditLedgerEntry, CreditReservation

    chat = review_env
    chat["save"](case)
    before = usage(chat)
    path = f"/ventures/{chat['vid']}/chat/confirm"
    request = {"action": "ceo_review", "payload": {}, "idempotency_key": "requested-review"}
    response = chat["client"].post(path, headers=chat["headers"](path, "POST"), json=request)
    assert response.status_code == 200, response.text
    reply = response.json()
    assert "**Biggest risks**" in reply["body_md"] and case[3] in reply["body_md"]
    assert reply["kind"] != "confirmation" and "confirm" not in reply["body_md"].lower()
    assert not any(action.get("confirmation") for action in reply["actions"])
    assert len(chat["calls"]) == 1
    after = usage(chat)
    assert after[0] < before[0] and after[1] == before[1] == 0
    with chat["factory"]() as session:
        assert session.query(CreditReservation).filter_by(venture_id=chat["vid"], status="settled").count() == 1
        assert session.query(CreditLedgerEntry).filter_by(account_id=chat["account_id"], kind="spend").count() == 1
    replay = chat["client"].post(path, headers=chat["headers"](path, "POST"), json=request)
    assert replay.status_code == 200 and replay.json() == reply
    assert len(chat["calls"]) == 1 and usage(chat) == after


@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
def test_saved_idea_pressure_test_is_useful_metered_and_single_use(review_env, case):
    from app.chat.reply_safety import contains_internal_prompt
    chat = review_env
    chat["save"](case)
    before = usage(chat)
    card = prepare(chat)
    assert not chat["calls"] and usage(chat) == before
    quote = card["actions"][0]["confirmation"]["resulting_state"]
    assert type(quote["ceiling_micro"]) is int and quote["ceiling_micro"] > 0
    response = confirm(chat, card)
    assert response.status_code == 200, response.text
    reply = response.json()
    body = reply["body_md"]
    assert "**Biggest risks**" in body and case[3] in body
    assert "What would support this risk:" in body and "What would disprove it:" in body
    assert "**One concrete next test**" in body and case[4] in body and "Pass or fail:" in body
    assert not contains_internal_prompt(body) and "\u2014" not in body
    assert len(chat["calls"]) == 1
    messages, call = chat["calls"][0]
    assert call["purpose"] == "idea.validate"
    assert call["workspace_id"] == chat["wid"] and call["max_tokens"] >= 2048
    assert case[1] in messages[-1]["content"] and "complete saved price detail" in messages[-1]["content"]
    after = usage(chat)
    assert after[0] < before[0] and after[1] == before[1]
    assert len(after[2]) > len(before[2])
    from app.db.models import CreditLedgerEntry, CreditReservation
    with chat["factory"]() as session:
        reservations = session.query(CreditReservation).filter_by(request_id=quote["review_id"]).all()
        spends = session.query(CreditLedgerEntry).filter_by(request_id=quote["review_id"], kind="spend").all()
        assert len(reservations) == len(spends) == 1
        assert reservations[0].status == "settled" and reservations[0].amount_micro <= quote["ceiling_micro"]
        assert spends[0].purpose == "idea.validate" and spends[0].account_id == chat["account_id"]
        assert reservations[0].venture_id == chat["vid"] and reservations[0].workspace_id == chat["wid"]
        assert spends[0].prompt_tokens == 900 and spends[0].completion_tokens == 600
        assert -spends[0].amount_micro == before[0] - after[0]
    replay = confirm(chat, card)
    assert replay.status_code == 200 and replay.json() == reply
    assert len(chat["calls"]) == 1 and usage(chat) == after


@pytest.mark.parametrize("failure", ["provider", "credits", "leak", "truncated"])
def test_pressure_test_failure_is_truthful_logs_and_offers_retry(review_env, caplog, failure):
    from app.credits.errors import InsufficientCredits
    from app.db.models import CreditAccount
    chat = review_env
    card = prepare(chat)
    if failure == "provider":
        chat["error"] = RuntimeError("private provider problem mentioning insufficient credits")
    elif failure == "credits":
        with chat["factory"]() as session:
            account = session.get(CreditAccount, chat["account_id"])
            account.granted_micro = account.spent_micro + account.reserved_micro
            session.commit()
    elif failure == "leak":
        chat["leak"] = True
    else:
        chat["finish_reason"] = "length"
    response = confirm(chat, card)
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "action_failed" and "ceo_review_failed" in caplog.text
    if failure == "credits":
        from app.credits.errors import LIMIT_MESSAGE
        from app.db.models import CreditLedgerEntry, CreditReservation

        assert reply["body_md"] == LIMIT_MESSAGE and not chat["calls"]
        assert any(action["label"] == "Buy more" for action in reply["actions"])
        with chat["factory"]() as session:
            assert session.query(CreditReservation).filter_by(venture_id=chat["vid"], status="active").count() == 0
            assert session.query(CreditLedgerEntry).filter_by(account_id=chat["account_id"], kind="spend").count() == 0
        assert any(isinstance(record.exc_info[1], InsufficientCredits) for record in caplog.records if record.exc_info)
    else:
        assert any(action["label"] == "Retry" and action["action"] == "ceo_review" and action["payload"] == {}
                   for action in reply["actions"])
        assert "problem on our side" in reply["body_md"]
        assert not any(term in reply["body_md"].lower() for term in ("credit", "budget", "private", "prime directive"))
    assert confirm(chat, card).json() == reply
    assert len(chat["calls"]) <= 1


def test_changed_or_foreign_confirmation_cannot_spend(review_env):
    from app.chat.execute import _ceo_review
    chat = review_env
    card = prepare(chat)
    payload = card["actions"][0]["confirmation"]["resulting_state"]
    before = usage(chat)
    for changed in ({**payload, "ceiling_micro": 1}, {**payload, "ceiling_micro": True},
                    {**payload, "review_id": "pressure_foreign"}):
        reply = asyncio.run(_ceo_review(chat["vid"], changed))
        assert reply["kind"] == "action_failed"
    chat["save"](CASES[1])
    reply = asyncio.run(_ceo_review(chat["vid"], payload))
    assert reply["kind"] == "action_failed"
    assert not chat["calls"] and usage(chat) == before


def test_intake_pending_cannot_start_pressure_test(review_env):
    from app.db.supymem_models import IdeaSession
    chat = review_env
    with chat["factory"]() as session:
        session.get(IdeaSession, "idea_pressure").state = "clarifying"
        session.commit()
    path = f"/ventures/{chat['vid']}/chat/confirm"
    response = chat["client"].post(path, headers=chat["headers"](path, "POST"), json={"action": "ceo_review"})
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "needs_you"
    assert response.json()["actions"], response.json()
    assert response.json()["actions"][0]["action"] == "idea.review"
    assert not chat["calls"]


def test_intake_pending_next_moves_never_offer_pressure_test(review_env, monkeypatch):
    from app.chat import next_moves
    from app.ventures import stage
    from tests.unit.chat.test_next_moves import _saved_state

    state = _saved_state(CASES[0][2])
    monkeypatch.setattr(next_moves, "_snapshot", lambda _venture_id: state)
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _venture_id: None)
    monkeypatch.setattr(stage, "get_stage", lambda _venture_id: {"stage": "idea", "label": "Shaping your idea"})
    for idea_state in ("open", "recon", "clarifying", "validating", "proposing", "reviewing"):
        state["idea_state"] = idea_state
        reply = next_moves.compose(review_env["vid"])
        assert not any(action["action"] == "ceo_review" for action in reply["actions"])


@pytest.mark.parametrize("case", CASES[1:], ids=[case[0] for case in CASES[1:]])
def test_missing_model_is_not_reported_as_missing_credits(review_env, monkeypatch, case):
    from app.chat import execute
    from llm_gateway import provider_foundry

    chat = review_env
    chat["save"](case)
    attempts = []
    original = execute._ceo_review

    async def once(*args, **kwargs):
        attempts.append(args)
        assert len(attempts) == 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(execute, "_ceo_review", once)
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: None)
    before = usage(chat)
    path = f"/ventures/{chat['vid']}/chat/confirm"
    response = chat["client"].post(path, headers=chat["headers"](path, "POST"), json={"action": "ceo_review"})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert len(attempts) == 1 and usage(chat) == before
    assert reply["kind"] == "action_failed" and "problem on our side" in reply["body_md"]
    assert not any(word in reply["body_md"].lower() for word in ("credit", "budget", "usage limit"))
    assert reply["actions"][0]["label"] == "Retry" and not chat["calls"]


@pytest.mark.parametrize("case", CASES[1:], ids=[case[0] for case in CASES[1:]])
def test_text_command_runs_the_same_saved_idea_review_without_confirmation(review_env, case):
    from app.chat.execute import execute_action
    from app.chat.conversations import bind_conversation
    from app.tenancy import bind

    chat = review_env
    chat["save"](case)
    with bind(workspace_id=chat["wid"], venture_id=chat["vid"], actor=chat["actor"]):
        with bind_conversation(chat["vid"], chat["cid"]):
            reply = asyncio.run(execute_action(chat["vid"], "ceo_review", payload={"query": "ceo review this plan"},
                actor_subject=chat["actor"]))
    assert reply["kind"] == "text" and "**Biggest risks**" in reply["body_md"] and len(chat["calls"]) == 1
    assert not any(action.get("confirmation") for action in reply["actions"])
