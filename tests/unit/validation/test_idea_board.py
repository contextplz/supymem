import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.unit.chat.test_round3_matrix import venture as matrix_venture


@pytest.fixture
def venture(tmp_path, monkeypatch, request):
    from fastapi.testclient import TestClient

    constructor = TestClient.__init__

    def initialize(instance, *args, **kwargs):
        constructor(instance, *args, **kwargs)
        from app.core.local_mode import is_local
        if is_local():
            from app.core.local_owner import _ensure_proxy_secret
            from core.local_security import _proxy_secret
            _ensure_proxy_secret()
            instance.headers["x-local-proxy-secret"] = _proxy_secret().decode()

    monkeypatch.setattr(TestClient, "__init__", initialize)
    yield from matrix_venture.__wrapped__(tmp_path, monkeypatch, request)


@pytest.mark.parametrize("idea", ["Contract review for procurement teams", "Garden planning for apartment residents"])
def test_local_board_uses_saved_model_without_pricing(monkeypatch, idea):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.validation import board
    from app.credits import estimate
    from app.llm import founder_provider
    from llm_gateway import provider_foundry, routing, sole

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda *_: SimpleNamespace(
        source="founder_override", provider="openai", model="owner-model", key_is_set=True))
    config = SimpleNamespace(provider="openai")
    monkeypatch.setattr(founder_provider, "to_provider_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(provider_foundry, "resolve_live_model", lambda *_args, **_kwargs: "owner-model")
    monkeypatch.setattr(routing, "resolve_purpose_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(routing, "sol_output_cap", lambda *_: board.MAX_TOKENS)
    monkeypatch.setattr(sole, "transport_prompt_tokens", lambda *_args, **_kwargs: 100)
    monkeypatch.setattr(estimate, "estimate_llm_micro", lambda *_args, **_kwargs: pytest.fail("Local review needs no hosted pricing"))
    result = board._quote("local-workspace", {"idea": idea})
    assert result["byok"] and result["ceiling_micro"] == result["call_ceiling_micro"] == 0


@pytest.mark.parametrize("idea", ["Purchase approvals for factory finance teams", "Weekend cycling routes for families"])
def test_board_quote_covers_each_complete_metered_request(monkeypatch, idea):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.validation import board
    from app.credits import estimate
    from app.llm import founder_provider
    from llm_gateway import sole, routing, provider_foundry

    config = SimpleNamespace(provider="azure_openai")
    monkeypatch.setattr(sole, "_resolved_tier", lambda *_: "pro")
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda *_: SimpleNamespace(source="platform"))
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: config)
    monkeypatch.setattr(provider_foundry, "resolve_live_model", lambda *_args, **_kwargs: "stub")
    monkeypatch.setattr(routing, "resolve_purpose_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(routing, "sol_output_cap", lambda *_: 5000)
    calls = []

    def tokens(messages, **kwargs):
        assert idea in messages[1]["content"] and kwargs["purpose"] == "idea.board"
        assert kwargs["response_schema"] == board.Memo.model_json_schema()
        assert kwargs["clock_text"] == " " * 256
        return 10000

    def price(model, *, prompt_tokens, max_tokens):
        calls.append((prompt_tokens, max_tokens))
        return prompt_tokens + max_tokens

    monkeypatch.setattr(sole, "transport_prompt_tokens", tokens)
    monkeypatch.setattr(estimate, "estimate_llm_micro", price)
    quote = board._quote("workspace_fixture", {"idea": idea})
    envelope = len(json.dumps({"response_schema": board.Memo.model_json_schema()}, ensure_ascii=False, allow_nan=False).encode())
    assert calls == [(10000 + envelope, 5000)] * len(board.LENSES)
    assert quote["call_ceiling_micro"] == quote["ceiling_micro"] == sum(prompt + cap for prompt, cap in calls)


@pytest.mark.parametrize("idea", ["A warehouse incident log", "A home sewing repair kit"])
def test_pressure_review_purpose_is_routed_and_requested_work_starts(venture, monkeypatch, idea):
    from llm_gateway.routing import resolve_tier
    from app.chat import execute, router

    assert resolve_tier("idea.board") == "pro"
    calls = []

    async def prepare(*args, **kwargs):
        return {"actions": [{"confirmation": {"action": "idea.pressure_test", "resulting_state": {"review_id": "review"}}}]}

    async def start(*args, **kwargs):
        calls.append(kwargs["actions"])
        return {"body_md": "Your review is ready. Read the findings."}

    monkeypatch.setattr(router, "_prepare_intent", prepare)
    monkeypatch.setattr(execute, "start_requested_work", start)
    reply = asyncio.run(router._dispatch_intent("venture", None, "idea.pressure_test.prepare",
        f"Pressure test my idea: {idea}.", None, "founder"))
    assert calls == [{"idea.pressure_test"}]
    assert "actions" not in reply
    from app.validation import board
    from app.chat import blockers, recovery
    from app.credits.errors import CreditsUnavailable, InsufficientCredits
    from llm_gateway.hosted import HostedCreditsRequired
    from app.tenancy import bind
    _, venture_id, workspace_id = venture

    def failed(*args):
        raise CreditsUnavailable("review unavailable")

    monkeypatch.setattr(board, "prepare", failed)
    with bind(workspace_id=workspace_id, venture_id=venture_id):
        failure = asyncio.run(execute._execute_action(venture_id, "idea.pressure_test.prepare"))
    alternative = next(action for action in failure["actions"] if action["action"] == "recovery.alternative")
    assert alternative["payload"]["step"] == "idea.board"
    assert len(recovery.STEPS["idea.board"]["steps"]) >= 3
    explanation = blockers.explain(venture_id, {"code": "idea_review_failed"})
    assert "background job" not in explanation["body_md"]
    assert "three people" in explanation["body_md"]
    for error in (InsufficientCredits("wallet", 0, 1), HostedCreditsRequired()):
        def limited(*args):
            raise error
        monkeypatch.setattr(board, "prepare", limited)
        with bind(workspace_id=workspace_id, venture_id=venture_id):
            limit = asyncio.run(execute._execute_action(venture_id, "idea.pressure_test.prepare"))
        assert "usage limit" in limit["body_md"]
        assert not any(action.get("action") == "recovery.alternative" for action in limit["actions"])
    from app.llm import founder_provider
    from llm_gateway import provider_foundry
    from app.credits import metered
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    config = provider_foundry.ProviderConfig(deployment_pro="gpt-6.1-sol", deployment_flash="gpt-6.1-sol")
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: SimpleNamespace(source="platform_default"))
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: config)
    quoted = board._quote(workspace_id, {"idea": idea})
    estimates = []

    async def captured(callback, **kwargs):
        estimates.append(kwargs["estimate_micro"])
        return None

    monkeypatch.setattr(metered, "call", captured)
    with metered.bounded_calls(workspace_id, venture_id, lambda *args: None):
        for lens in board.LENSES:
            asyncio.run(metered.llm(board.messages({"idea": idea}, lens), purpose="idea.board",
                workspace_id=workspace_id, venture_id=venture_id, max_tokens=board.MAX_TOKENS,
                response_schema=board.Memo.model_json_schema()))
    assert sum(estimates) == quoted["call_ceiling_micro"]


@pytest.mark.parametrize("idea,audience,offer", [
    ("A warehouse incident log", "warehouse shift supervisors", "Record and hand off safety incidents"),
    ("A home sewing repair kit", "people repairing their own clothes", "Mend a torn garment with guided supplies"),
])
def test_requested_pressure_test_is_metered_independent_and_optional(venture, monkeypatch, idea, audience, offer):
    client, venture_id, workspace_id = venture
    from app.validation import board
    from app.credits.gate import _call_allowance
    from app.credits.errors import CreditsUnavailable
    from app.db.models import HumanTask, Venture
    from app.db.supymem_models import IdeaSession, ValidationRun
    from app.idea.service import append_revision
    from app.state import MEM, db
    from app.tenancy import bind
    from app.validation.verdict import compute_verdict
    from core.clock import utcnow
    from core.ids import new_id

    with db()() as session:
        session.get(Venture, venture_id).raw_idea = idea
        saved = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id))
        if saved is None:
            saved = IdeaSession(id=new_id("ids"), venture_id=venture_id, workspace_id=workspace_id,
                                case="know", state="open", created_at=utcnow(), updated_at=utcnow())
            session.add(saved)
            session.flush()
        revision = append_revision(session, saved, {"problem": idea, "customer": audience, "offer": offer}, source="founder", summary=idea)
        saved.state, saved.confirmed_revision_id = "confirmed", revision.id
        session.commit()
    MEM[f"research_dossier:{venture_id}"] = {"summary": "A saved source mentions a competing approach.", "mode": "live"}
    before = compute_verdict({"sources": []}).model_dump()
    quote = {"provider": "offline_stub", "model": "stub", "byok": False,
             "call_ceiling_micro": 300, "ceiling_micro": 300}
    monkeypatch.setattr(board, "_quote", lambda *_: dict(quote))
    calls = []
    memos = []
    for index, vote in enumerate(["Back", "Pass", "Back if"]):
        memos.append({"score": [8, 3, 6][index], "vote": vote,
            "risks": [{"area": "demand", "risk": f"{audience} may not need {offer}."},
                      {"area": "economics", "risk": f"Delivery cost risk {index}."},
                      {"area": ["offer", "defensibility", "focus"][index], "risk": f"Independent risk {index}."}],
            "questions": [f"What would you measure for question {number}?" for number in range(3)],
            "conditions": ["You observe a repeatable customer outcome.", f"You resolve risk {index}."]})

    async def offline(messages, **kwargs):
        assert kwargs["purpose"] == "idea.board" and kwargs["workspace_id"] == workspace_id
        assert kwargs["venture_id"] == venture_id
        allowance = _call_allowance.get()
        assert allowance[:2] == (workspace_id, venture_id)
        allowance[2]("stub_account", kwargs["request_id"], 100)
        context = json.loads(messages[1]["content"])
        assert context["icp"] == audience and idea in context["idea"] and offer in context["idea"]
        assert "saved source" in context["research"]
        assert "memos" not in context
        assert len(messages) == 2
        calls.append((messages, kwargs))
        memo = memos[len(calls) - 1]
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(memo)))])

    monkeypatch.setattr(board.metered, "llm", offline)
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": "Pressure-test my idea"})
    assert response.status_code == 200, response.text
    assert not any(action.get("confirmation", {}).get("action") == "idea.pressure_test" for action in response.json()["actions"])
    assert "credit" not in response.json()["body_md"].lower()
    review_key = next(key for key, value in MEM.items() if key.startswith(f"idea_board:{venture_id}:") and value.get("state") == "complete")
    review_id = review_key.rsplit(":", 1)[1]
    payload = {"review_id": review_id, "ceiling_micro": quote["ceiling_micro"]}
    assert payload["ceiling_micro"] == quote["ceiling_micro"]
    assert len(calls) == 3
    assert MEM[review_key]["quote"]["ceiling_micro"] == quote["ceiling_micro"]
    reply = response
    assert len(calls) == 3 and len({messages[0]["content"] for messages, _ in calls}) == 3
    assert len({kwargs["request_id"] for _, kwargs in calls}) == 3
    assert "AI review: Back if" in reply.json()["body_md"] and "not evidence" in reply.json()["body_md"]
    result = MEM[board._key(venture_id, payload)]["result"]
    assert all("provider" not in " ".join(action.get("confirmation", {}).get("consequences", [])).lower() for action in reply.json()["actions"])
    assert result["score"] == 5.7 and result["risks"][0]["reviewers"] == list(board.LENSES)
    assert sum(len(group["risks"]) for group in result["risks"]) == 7
    assert result["risks"][0]["risks"][0]["reviewers"] == list(board.LENSES)
    assert len(result["conditions"]) == 4 and result["memos"] == dict(zip(board.LENSES, memos))
    assert [item["vote"] for item in result["disagreements"]] == ["Back", "Pass", "Back if"]
    with bind(workspace_id=workspace_id, venture_id=venture_id):
        repeated = asyncio.run(board.run(venture_id, payload))
        assert "AI review: Back if" in repeated["body_md"] and len(repeated["actions"]) == 4
        assert len(calls) == 3
        with pytest.raises(ValueError, match="confirmation_changed"):
            asyncio.run(board.run(venture_id, {**payload, "ceiling_micro": 301}))
        accepted = reply.json()["actions"][0]["confirmation"]["resulting_state"]
        with db()() as session:
            assert session.query(HumanTask).filter_by(venture_id=venture_id).count() == 0
        board.accept(venture_id, accepted)
        board.accept(venture_id, accepted)
        with bind(workspace_id="another_workspace", venture_id=venture_id):
            with pytest.raises(ValueError, match="condition_not_found"):
                board.accept(venture_id, accepted)
        with db()() as session:
            tasks = session.query(HumanTask).filter_by(venture_id=venture_id).all()
            assert len(tasks) == 1 and tasks[0].category == "idea_review"
            assert session.query(ValidationRun).filter_by(venture_id=venture_id).count() == 0
        fresh = board.prepare(venture_id)["actions"][0]["confirmation"]["resulting_state"]
        original_context = board._context
        context = original_context(venture_id)[1]
        monkeypatch.setattr(board, "_context", lambda *_: (workspace_id, {**context, "idea": "Changed idea"}))
        with pytest.raises(ValueError, match="changed") as changed:
            asyncio.run(board.run(venture_id, fresh))
        assert "credit" not in str(changed.value).lower()
        assert len(calls) == 3
        monkeypatch.setattr(board, "_context", original_context)
        monkeypatch.setenv("SUPYMEM_MODE", "hosted")
        interrupted = board.prepare(venture_id)["actions"][0]["confirmation"]["resulting_state"]
        attempts = []
        async def over_ceiling(_messages, **kwargs):
            attempts.append(kwargs["request_id"])
            _call_allowance.get()[2]("stub_account", kwargs["request_id"], 301)
        monkeypatch.setattr(board.metered, "llm", over_ceiling)
        with pytest.raises(CreditsUnavailable, match="credit_ceiling"):
            asyncio.run(board.run(venture_id, interrupted))
        with pytest.raises(ValueError, match="will not run again"):
            asyncio.run(board.run(venture_id, interrupted))
        assert len(attempts) == 1
    all_back = {lens: {**memo, "vote": "Back"} for lens, memo in zip(board.LENSES, memos)}
    assert board.chair(all_back)["verdict"] == "Back"
    two_pass = copy.deepcopy(all_back)
    for lens in list(board.LENSES)[:2]:
        two_pass[lens]["vote"] = "Pass"
    assert board.chair(two_pass)["verdict"] == "Pass"
    with pytest.raises(ValueError):
        board.Memo.model_validate({**memos[0], "score": 11})
    assert compute_verdict({"sources": []}).model_dump() == before

