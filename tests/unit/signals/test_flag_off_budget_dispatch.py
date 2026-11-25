import asyncio
import json

import httpx
import pytest
from sqlalchemy import select

from tests.credit_fixtures import ensure_test_wallet
from tests.unit.chat.test_chat_request_status import chat_request  # noqa: F401


@pytest.fixture(params=[
    ("b2b", "Supplier compliance reviews for procurement teams"),
    ("consumer", "Family music lessons for beginner piano players"),
])
def budget_dispatch(chat_request, monkeypatch, request):
    from app.credits import accounts, metered, policy
    from app.db.models import CreditAccount, CreditReservation, Venture

    domain, query = request.param
    venture_id = f"ven_budget_dispatch_{domain}"
    wallet = ensure_test_wallet(venture_id)
    workspace_id, person_id = f"ws_wallet:{venture_id}", f"per_wallet:{venture_id}"
    policy.set_policy("feature_flags", {}, updated_by="test")
    policy.set_policy("enforcement_enabled", True, updated_by="test")
    monkeypatch.setenv("LLM_ADMISSION_V1", "0")
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_URL", "https://firecrawl.invalid")
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_TOKEN", "offline-token")
    monkeypatch.setenv("WEB_SEARCH_OPENAI_URL", "https://search.invalid/openai/v1")
    monkeypatch.setenv("WEB_SEARCH_OPENAI_KEY", "offline-key")
    with chat_request["factory"]() as session:
        venture = session.get(Venture, venture_id)
        venture.name = query
        venture.raw_idea = query
        session.commit()

    calls = []
    result_url = f"https://{domain}.example/evidence"

    def respond(request):
        with chat_request["factory"]() as session:
            account = session.get(CreditAccount, wallet.id)
            assert account.reserved_micro > 0
            reservation = session.scalar(select(CreditReservation).where(
                CreditReservation.account_id == wallet.id, CreditReservation.status == "reconciling"))
            assert reservation is not None
            assert reservation.workspace_id == workspace_id
            assert reservation.venture_id == venture_id
        payload = json.loads(request.content)
        calls.append(request)
        row = {"url": result_url, "title": query, "snippet": "Public customer evidence"}
        if request.url == "https://search.invalid/openai/v1/responses":
            assert payload["input"] == query
            assert payload["max_tool_calls"] == 1
            return httpx.Response(200, json={"output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps([row])}]}]})
        assert request.url == "https://api.exa.ai/search"
        assert payload["query"] == query
        return httpx.Response(200, json={"results": [row]})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs:
        original_client(transport=httpx.MockTransport(respond), **kwargs))
    return {"wallet": wallet, "workspace_id": workspace_id, "venture_id": venture_id,
            "person_id": person_id, "query": query, "url": result_url, "calls": calls,
            "factory": chat_request["factory"], "quote": metered.price_micro("firecrawl_search"),
            "before_spent": accounts.get_account(venture_id).spent_micro}


@pytest.mark.parametrize("flag", [None, False, "true", 1])
def test_flag_off_bounded_search_dispatches_and_settles_existing_meter(budget_dispatch, flag):
    from app.credits import accounts, gate, metered, policy
    from app.credits.errors import CreditsUnavailable
    from app.db.models import CreditReservation
    from app.signals.research import HttpResearchProvider

    env = budget_dispatch
    policy.set_policy("feature_flags", {"reader_v2": flag, "voice_parity_v1": flag}, updated_by="test")
    admitted, settled = [], []

    def admit(account_id, request_id, estimate_micro):
        if sum(row[2] for row in admitted) + estimate_micro > env["quote"]:
            raise CreditsUnavailable("existing bounded search ceiling")
        admitted.append((account_id, request_id, estimate_micro))
        return settled.append

    async def run():
        with gate.bounded_calls(env["workspace_id"], env["venture_id"], admit):
            rows = await metered.search(HttpResearchProvider("firecrawl", ""), env["query"],
                workspace_id=env["workspace_id"], venture_id=env["venture_id"], request_id="first-search")
            assert rows[0]["url"] == env["url"]
            assert rows.usage["firecrawl_self_hosted"] is True
            with pytest.raises(CreditsUnavailable, match="existing bounded search ceiling"):
                await metered.search(HttpResearchProvider("firecrawl", ""), env["query"],
                    workspace_id=env["workspace_id"], venture_id=env["venture_id"], request_id="second-search")

    asyncio.run(run())
    assert len(env["calls"]) == 1
    assert admitted == [(env["wallet"].id, "first-search", env["quote"])]
    assert len(settled) == 1 and settled[0].recorded
    assert settled[0].actual_micro == 1 and settled[0].settle_result is not None
    account = accounts.get_account(env["venture_id"])
    assert account.spent_micro == env["before_spent"] + 1
    assert account.reserved_micro == 0
    with env["factory"]() as session:
        reservations = session.scalars(select(CreditReservation).where(
            CreditReservation.account_id == account.id)).all()
        assert len(reservations) == 1
        assert reservations[0].amount_micro == env["quote"]
        assert reservations[0].settled_micro == 1
        assert reservations[0].status == "settled"
    assert gate._call_allowance.get() is None


@pytest.mark.parametrize("opt_in", ["reader_v2", "voice_parity_v1", "LLM_ADMISSION_V1"])
def test_opt_in_search_still_refuses_before_provider_dispatch(budget_dispatch, monkeypatch, opt_in):
    from app.credits import gate, metered, policy
    from app.credits.errors import CreditsUnavailable
    from app.signals.research import HttpResearchProvider

    env = budget_dispatch
    if opt_in == "LLM_ADMISSION_V1":
        monkeypatch.setenv(opt_in, "1")
    else:
        policy.set_policy("feature_flags", {opt_in: True}, updated_by="test")
    admitted = []

    async def run():
        with gate.bounded_calls(env["workspace_id"], env["venture_id"], lambda *args: admitted.append(args)):
            with pytest.raises(CreditsUnavailable, match="bounded price before dispatch"):
                await metered.search(HttpResearchProvider("firecrawl", ""), env["query"],
                    workspace_id=env["workspace_id"], venture_id=env["venture_id"], request_id="opt-in-search")

    asyncio.run(run())
    assert admitted == [(env["wallet"].id, "opt-in-search", env["quote"])]
    assert env["calls"] == []


def test_flag_off_search_does_not_bypass_allowance_scope(budget_dispatch):
    from app.credits import gate, metered
    from app.credits.errors import CreditsUnavailable
    from app.signals.research import HttpResearchProvider

    env = budget_dispatch

    async def run():
        with gate.bounded_calls("another-workspace", env["venture_id"], lambda *args:
                                pytest.fail("A mismatched scope must not reach budget admission")):
            with pytest.raises(CreditsUnavailable, match="credit allowance scope mismatch"):
                await metered.search(HttpResearchProvider("firecrawl", ""), env["query"],
                    workspace_id=env["workspace_id"], venture_id=env["venture_id"])

    asyncio.run(run())
    assert env["calls"] == []


@pytest.mark.parametrize("flag", [None, False, "true", 1])
def test_flag_off_voice_budget_dispatches_and_meters_like_baseline(budget_dispatch, monkeypatch, flag):
    from app import tools
    from app.api import voice_api
    from app.chat.agent import TURN_BUDGET_MICRO
    from app.credits import accounts, policy
    from app.credits.units import credits_to_micro
    from app.db.models import AnalyticsEvent
    from app.signals.research import HttpResearchProvider
    from app.tenancy import bind
    from core.clock import utcnow

    env = budget_dispatch
    policy.set_policy("feature_flags", {"voice_parity_v1": flag}, updated_by="test")
    monkeypatch.delenv("FIRECRAWL_SELF_HOSTED_URL")
    monkeypatch.setattr("app.signals.research._provider_from_env", lambda:
                        HttpResearchProvider("exa", "offline-key"))

    async def no_free_results(*args):
        return []

    monkeypatch.setattr(tools, "_free_search", no_free_results)
    now = utcnow()
    monkeypatch.setattr(voice_api, "utcnow", lambda: now)
    receipt_id = "ane_budget_dispatch"
    with env["factory"]() as session:
        session.add(AnalyticsEvent(id=receipt_id, person_id=env["person_id"],
            workspace_id=env["workspace_id"], venture_id=env["venture_id"],
            event_name="voice.session_started", occurred_at=now))
        session.commit()
    args = {"query": env["query"]}
    body = voice_api.VoiceToolCall(session_event_id=receipt_id, turn_id="turn:1",
        call_id="call:1", arguments=json.dumps(args))
    key, remaining, budget, saved = voice_api._reserve_tool(body, "web_search", args,
        env["person_id"], env["venture_id"], env["workspace_id"])
    assert remaining == voice_api.VOICE_TOOL_WALL_S and saved is None
    ctx = tools.ToolContext(venture_id=env["venture_id"], workspace_id=env["workspace_id"], budget_micro=budget)
    with bind(workspace_id=env["workspace_id"], venture_id=env["venture_id"], actor=env["person_id"]):
        result = asyncio.run(tools.execute_tool("web_search", args, ctx))
    assert env["url"] in result
    assert budget == TURN_BUDGET_MICRO
    cost = credits_to_micro(policy.web_search_credits())
    assert ctx.spent_micro == cost and not ctx.paid_search_outcome_unknown
    assert len(env["calls"]) == 1
    assert accounts.get_account(env["venture_id"]).spent_micro == env["before_spent"] + cost
    assert accounts.get_account(env["venture_id"]).reserved_micro == 0
    reply = {"body_md": result, "partial": False}
    voice_api._save_tool_result(key, reply, ctx.spent_micro, ctx.paid_search_outcome_unknown)
    assert voice_api._reserve_tool(body, "web_search", args,
        env["person_id"], env["venture_id"], env["workspace_id"])[3] == reply
    _, _, next_budget, saved = voice_api._reserve_tool(body.model_copy(update={"call_id": "call:2"}),
        "web_search", args, env["person_id"], env["venture_id"], env["workspace_id"])
    assert saved is None and next_budget == max(0, TURN_BUDGET_MICRO - cost)
