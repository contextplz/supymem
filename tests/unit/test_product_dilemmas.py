"""Product-understanding dilemmas come before GStack and write a brief."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.clarify.dilemmas import brief_from_answers, dilemmas_for_idea, is_product_questions


def test_music_marketplace_asks_what_the_object_is():
    qs = dilemmas_for_idea(
        "I need a marketplace where I can get the songs and group multiple songs together and sell my playlist choices."
    )
    assert 5 <= len(qs) <= 7
    assert any(q["slot_key"] == "product_expertise" for q in qs)
    assert any(q["slot_key"] == "product_market" for q in qs)
    assert all(q["process"] == "product_understanding" for q in qs)
    blob = " ".join(q["prompt"] for q in qs).lower()
    assert "playlist" in blob or "song" in blob
    obj = next(q for q in qs if q["slot_key"] == "product_object")
    assert any("playlist" in (o["label"].lower()) for o in obj["options"])


def test_product_questions_are_not_gstack():
    qs = dilemmas_for_idea("a marketplace for restoring vintage synthesizers")
    assert is_product_questions(qs)
    assert not is_product_questions(
        [{"id": "q1", "slot_key": "goal_primary", "process": "gstack_office_hours"}]
    )


def test_brief_is_the_product_not_a_gstack_plan():
    idea = "a marketplace for restoring vintage synthesizers"
    qs = dilemmas_for_idea(idea)
    answers = {q["slot_key"]: "opt_a" for q in qs}
    brief = brief_from_answers(idea=idea, name="Synthfix", answers=answers, questions=qs)
    assert brief["process"] == "product_understanding"
    assert brief["name"] == "Synthfix"
    assert brief["what_it_is"]
    assert brief["what_it_is_not"]
    assert brief["first_job"]
    assert brief["understood"]
    assert brief["expertise"]
    assert brief["market"]
    assert "GStack" not in brief["what_it_is"]


@pytest.mark.asyncio
async def test_office_hours_does_not_infer_from_product_quiz():
    from types import SimpleNamespace

    from app.onboarding.state import Ctx
    from app.onboarding.steps.validate import ask, office_hours_plan
    from app.state import MEM

    ven = SimpleNamespace(
        id="ven_mixup",
        raw_idea="a marketplace for restoring vintage synthesizers",
        name="Synthfix",
        one_liner="",
    )
    run = SimpleNamespace(id="obr_mixup", phase="validate", mode="guided")
    ctx = Ctx(venture=ven, run=run, mode="guided")  # type: ignore[arg-type]
    product = dilemmas_for_idea(ven.raw_idea, name=ven.name)
    MEM[f"questions:{ven.id}"] = product
    MEM[f"product_questions:{ven.id}"] = product
    MEM[f"answers:{ven.id}"] = {q["slot_key"]: "opt_a" for q in product}
    MEM[f"answers_source:{ven.id}"] = "founder"
    asked = await ask(ctx)
    assert asked.pause is not None
    assert asked.pause.reason == "awaiting_clarification"
    assert not is_product_questions(MEM[f"questions:{ven.id}"])
    planned = await office_hours_plan(ctx)
    assert planned.pause is not None
    assert planned.pause.reason == "awaiting_clarification"
    assert MEM.get(f"gstack_plan:{ven.id}") is None


@pytest.mark.asyncio
async def test_product_ask_pauses_before_gstack():
    from types import SimpleNamespace

    from app.onboarding.state import Ctx
    from app.onboarding.steps.understand import product_ask, product_brief
    from app.state import MEM

    ven = SimpleNamespace(
        id="ven_prod",
        raw_idea="songs marketplace to sell playlist choices",
        name="Mixlist",
        one_liner="",
    )
    run = SimpleNamespace(id="obr_prod", phase="understand")
    ctx = Ctx(venture=ven, run=run, mode="guided")  # type: ignore[arg-type]
    asked = await product_ask(ctx)
    assert asked.pause is not None
    assert asked.pause.reason == "awaiting_product_understanding"
    assert asked.output["process"] == "product_understanding"
    MEM[f"product_answers:{ven.id}"] = {
        q["slot_key"]: "opt_a" for q in asked.output["questions"]
    }
    MEM[f"product_questions:{ven.id}"] = asked.output["questions"]
    planned = await product_brief(ctx)
    assert planned.pause is not None
    assert planned.pause.reason == "awaiting_product_brief"
    assert planned.output["what_it_is"]


@pytest.mark.asyncio
async def test_demand_read_reuses_product_research():
    from types import SimpleNamespace

    from app.onboarding.state import Ctx
    from app.onboarding.steps.validate import demand_read
    from app.state import MEM

    ven = SimpleNamespace(id="ven_reuse", raw_idea="vintage synth marketplace", name="Synthfix")
    run = SimpleNamespace(id="obr_reuse", phase="validate", mode="guided")
    ctx = Ctx(venture=ven, run=run, mode="guided")  # type: ignore[arg-type]
    ctx.out["understand.research_market"] = {
        "mode": "live",
        "demand": "mixed",
        "queries": ["vintage synth marketplace competitors"],
        "sources": [{"title": "Synthforum", "url": "https://example.com"}],
        "competitors": [{"name": "Reverb", "website": "https://reverb.com"}],
        "claims": [{"claim": "People restore vintage synths", "source": "https://example.com"}],
        "demand_signals": [{"claim": "People restore vintage synths", "source": "https://example.com"}],
        "provider": "duckduckgo",
    }
    MEM[f"brief:{ven.id}"] = {
        "product": {
            "name": "Synthfix",
            "what_it_is": "Two sides meet here",
            "research_query": "vintage synth marketplace competitors",
        }
    }
    result = await demand_read(ctx)
    assert result.query
    assert result.kind == "search"
    assert result.output["dossier"]["reused"] is True
    assert MEM[f"validation:{ven.id}"]["source_count"] == 1


@pytest.mark.asyncio
async def test_research_market_soft_fails_instead_of_throwing(monkeypatch):
    from types import SimpleNamespace

    from app.onboarding.state import Ctx
    from app.onboarding.steps import understand
    from app.state import MEM

    async def boom(*_a, **_k):
        raise RuntimeError("search down")

    monkeypatch.setattr(understand, "research_recon", boom)
    ven = SimpleNamespace(id="ven_mkt", raw_idea="hardware marketplace", name="Ican")
    run = SimpleNamespace(id="obr_mkt", phase="understand")
    ctx = Ctx(venture=ven, run=run, mode="guided")  # type: ignore[arg-type]
    MEM[f"brief:{ven.id}"] = {"product": {"one_liner": "sell taps and hammers"}}
    result = await understand.research_market(ctx)
    assert result.output["demand"] == "unknown"
    assert result.output.get("reason") == "research_error"


@pytest.mark.asyncio
@pytest.mark.hosted_only
async def test_landing_page_pauses_for_builder_choice(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from types import SimpleNamespace

    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.onboarding.state import Ctx
    from app.onboarding.steps.build import landing_page

    ven = SimpleNamespace(id="ven_build", raw_idea="auto driver community", name="Beable")
    run = SimpleNamespace(id="obr_build", phase="build", mode="guided")
    ctx = Ctx(venture=ven, run=run, mode="guided")  # type: ignore[arg-type]
    ctx.out = {}
    result = await landing_page(ctx)
    assert result.pause is not None
    assert result.pause.reason == "awaiting_builder_choice"


@pytest.mark.asyncio
@pytest.mark.hosted_only
async def test_landing_page_skips_choice_when_page_already_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from types import SimpleNamespace

    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.onboarding.state import Ctx
    from app.onboarding.steps.build import landing_page

    ven = SimpleNamespace(
        id="ven_ready",
        raw_idea="studio hours",
        name="Studiohour",
        workspace_id="wsp_ready",
    )
    run = SimpleNamespace(id="obr_ready", phase="build", mode="guided")
    ctx = Ctx(venture=ven, run=run, mode="guided")  # type: ignore[arg-type]
    ctx.out = {}
    repo = tmp_path / "ventures" / "ven_ready" / "repo"
    repo.mkdir(parents=True)
    (repo / "index.html").write_text(
        "<!DOCTYPE html><html><body>" + ("Studiohour" * 40) + "</body></html>"
    )
    result = await landing_page(ctx)
    assert result.pause is None
    assert "index.html" in (result.output.get("files") or [])


@pytest.mark.asyncio
@pytest.mark.hosted_only
async def test_cloud_choice_does_not_write_a_fake_page(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("BUILDER_HOSTED", raising=False)
    monkeypatch.delenv("BUILDER_SANDBOX", raising=False)
    from types import SimpleNamespace

    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.onboarding.state import Ctx
    from app.onboarding.steps.build import CLOUD_UNAVAILABLE_MSG, landing_page
    from app.state import MEM

    ven = SimpleNamespace(
        id="ven_cloud",
        raw_idea="studio hours",
        name="Studiohour",
        company="Studiohour",
    )
    run = SimpleNamespace(id="obr_cloud", phase="build", mode="guided")
    ctx = Ctx(venture=ven, run=run, mode="guided")  # type: ignore[arg-type]
    ctx.out = {}
    MEM.clear()
    monkeypatch.setattr(
        "app.onboarding.gates.get_run_gate",
        lambda key, *_a, **_k: "cloud" if key == "builder_choice" else None,
    )
    result = await landing_page(ctx)
    assert result.pause is not None
    assert result.pause.reason == "awaiting_builder_choice"
    assert result.output["error"] == CLOUD_UNAVAILABLE_MSG
    repo = tmp_path / "ventures" / "ven_cloud" / "repo"
    assert not (repo / "index.html").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("idea", ["Inventory planning for wholesalers", "Music lessons for families"])
@pytest.mark.parametrize("legacy_page", [False, True])
async def test_local_onboarding_never_creates_or_accepts_an_internal_page(tmp_path, monkeypatch, idea, legacy_page):
    from types import SimpleNamespace
    from tests.company_os_harness import load_company_os_app

    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    load_company_os_app()
    from app.onboarding.state import Ctx
    from app.onboarding.steps.build import landing_page

    venture = SimpleNamespace(id="ven_local_page", workspace_id="wsp_local_page", raw_idea=idea, name=idea)
    context = Ctx(venture=venture, run=SimpleNamespace(id="obr_local_page", phase="build", mode="guided"), mode="guided")
    context.out = {}
    page = tmp_path / "ventures" / venture.id / "repo" / "index.html"
    if legacy_page:
        page.parent.mkdir(parents=True)
        page.write_text("<!doctype html><html><body>" + idea * 40 + "</body></html>")
    previous = page.read_bytes() if legacy_page else None
    from app.builder import ctl

    requested = []
    review_path = f"/v/{venture.id}/device-work-orders/dwo_local"
    monkeypatch.setattr(ctl, "local_tool", lambda *args, **kwargs: "codex")
    def request(venture_id, instruction, **options):
        requested.append(venture_id)
        assert idea in options["brief"] and options["provider"] == "codex" and options["repair"] is True
        return {"id": "dwo_local", "state": "approved", "review_path": review_path}
    monkeypatch.setattr(ctl, "request_local_build", request)
    result = await landing_page(context)
    assert result.pause.reason == "awaiting_builder_session"
    assert result.pause.founder_action == review_path
    assert result.output["device_work_order_id"] == "dwo_local" and requested == [venture.id]
    assert result.output["files"] == [] and result.output["message"]
    assert page.read_bytes() == previous if legacy_page else not page.exists()


def test_cloud_builder_live_only_when_explicitly_hosted(monkeypatch):
    from app.onboarding.steps.build import cloud_builder_live

    monkeypatch.delenv("BUILDER_HOSTED", raising=False)
    monkeypatch.delenv("BUILDER_SANDBOX", raising=False)
    assert cloud_builder_live() is False
    monkeypatch.setenv("BUILDER_SANDBOX", "fake")
    assert cloud_builder_live() is False
    monkeypatch.setenv("BUILDER_SANDBOX", "fargate")
    assert cloud_builder_live() is True
    monkeypatch.delenv("BUILDER_SANDBOX", raising=False)
    monkeypatch.setenv("BUILDER_HOSTED", "1")
    assert cloud_builder_live() is True
from tests.unit.gtm.test_known import known_case, journey_db


@pytest.mark.asyncio
async def test_product_ask_reuses_known_answers_and_preserves_open_questions(known_case, monkeypatch):
    from types import SimpleNamespace
    from app.clarify import dilemmas
    from app.connections import context_docs
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    from app.db.models import Venture
    from app.onboarding.state import Ctx
    from app.onboarding.steps.understand import product_ask
    from app.state import MEM
    from app.ventures import founder_facts, startup_review

    data = known_case
    if is_local():
        update_settings("capabilities", {"connected_apps": True}, {})
        update_settings("composio", {}, {"api_key": "test-connection-key"})
    questions = [{"id": "buyer", "slot_key": "product_buyer", "prompt": "Who buys it?", "options": []},
                 {"id": "market", "slot_key": "product_market", "prompt": "Where do you sell?", "options": []}]
    monkeypatch.setattr(dilemmas, "dilemmas_for_idea", lambda *_args, **_kwargs: questions)
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        ctx = Ctx(venture=venture, run=SimpleNamespace(id="known_run"), mode="guided")
        first = await product_ask(ctx)
        from app.api import clarify
        monkeypatch.setattr(clarify, "MEM", MEM)
        monkeypatch.setattr(clarify, "_pause_reason", lambda _vid: "awaiting_product_understanding")
        assert [question["id"] for question in clarify._live_questions(data["vid"])[0]] == ["market"]
        assert [question["id"] for question in first.output["questions"]] == ["market"]
        assert first.output["assumptions"][0]["values"][0]["value"] == data["audience"]
        assert MEM[f"product_answers:{data['vid']}"]["product_buyer"] == data["audience"]
        assert (await product_ask(ctx)).pause is not None
        run = SimpleNamespace(id="known_run", status="paused", mode="guided", workspace_id=data["wid"], entry_mode="idea")
        monkeypatch.setattr(clarify, "active_run", lambda _vid: run)
        from app.onboarding import spawn

        async def resume(*_args, **_kwargs):
            return run

        monkeypatch.setattr(spawn, "start_onboarding", resume)
        await clarify.post_clarification_answers(data["vid"], clarify.ClarificationAnswers(answers=[
            clarify.AnswerItem(question_id="market", other_text="Your customers buy locally")]))
        assert MEM[f"product_answers:{data['vid']}"]["product_buyer"] == data["audience"]
        MEM[f"product_answers:{data['vid']}"] = {}
        from app.onboarding.gates import set_run_gate
        set_run_gate("product_understanding", data["vid"], run.id, None)
        startup_review._document(session, data["vid"], startup_review.INTERNAL_KIND,
            {"founder_facts": {"audience": founder_facts._fact(data["audience"], "website", .8)}})
        session.commit()
        second = await product_ask(ctx)
        assert second.output["questions"][0]["known_check"]["status"] == "confirm"
        ctx.mode = "auto"
        assert (await product_ask(ctx)).pause is not None
        ctx.mode = "guided"
        monkeypatch.setattr(context_docs, "research_context", lambda *_args: {"connected_context": {"documents": [
            {"title": "Your selected plan", "excerpt": "Audience: Museum visitors"}]}})
        third = await product_ask(ctx)
        assert third.output["questions"][0]["known_check"]["status"] == "conflict"
        assert len(third.output["questions"][0]["options"]) == 2
        from fastapi import HTTPException
        from app.ventures import known
        choice = clarify.AnswerItem(question_id="buyer", option_id="known_0")
        with pytest.raises(HTTPException, match="incomplete_clarification_answers"):
            await clarify.post_clarification_answers(data["vid"], clarify.ClarificationAnswers(answers=[choice]))
        assert known.known(data["vid"], "audience")["status"] == "conflict"
        await clarify.post_clarification_answers(data["vid"], clarify.ClarificationAnswers(answers=[choice,
            clarify.AnswerItem(question_id="market", other_text="Your customers buy locally")]))
        assert known.known(data["vid"], "audience")["status"] == "known"
