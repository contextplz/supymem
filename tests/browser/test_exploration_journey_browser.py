"""Rendered intake and exploration through signed Core with synthetic provider edges.

Reuse the separately served loopback Console in GTM_CONSOLE_URL. Intake, chat,
rounds, queued experiments and recorded activity are real; model suggestions and
empty search responses are offline fixtures, not evidence of market demand.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from tests.browser.test_gtm_journey_browser import (
    _install_forwarder,
    _screenshot,
    _skip_intro,
    journey_db as journey_db,
    rendered_core as rendered_core,
)
from tests.security.test_core_signed_boundary import headers


pytestmark = pytest.mark.skipif(not os.getenv("GTM_CONSOLE_URL"), reason="isolated Console URL not supplied")


def _run_queued_exploration(receipt, actor, kind):
    from app.chat.conversations import bind_job_conversation
    from app.core.jobs import complete_job, lease
    from app.db.models import Job
    from app.db.session import serialize_writes
    from app.entry import service
    from app.state import db
    from app.worker.handlers import dispatch, install_default_handlers

    install_default_handlers()

    async def run():
        with service.venture_scope(receipt["venture_id"], actor_id=actor, workspace_id=receipt["workspace_id"]):
            with db()() as session:
                expected = list(session.scalars(select(Job.id).where(
                    Job.venture_id == receipt["venture_id"], Job.kind == kind, Job.status == "scheduled")))
                assert expected, f"No queued {kind} work for the confirmed idea"
                jobs = lease(session, "exploration-browser", kinds=[kind], n=len(expected))
                session.commit()
            assert {job.id for job in jobs} == set(expected)

            async def execute(job):
                payload = {**job.payload, "venture_id": job.venture_id, "workspace_id": job.workspace_id,
                           "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
                with bind_job_conversation(payload, venture_id=job.venture_id, workspace_id=job.workspace_id):
                    await dispatch(job.kind, payload)
                with db()() as session:
                    serialize_writes(session, key="market-experiments:" + job.venture_id)
                    complete_job(session, job.id, worker_id=job.worker_id, attempt=job.attempts)
                    session.commit()

            await asyncio.gather(*(execute(job) for job in jobs))
            return jobs

    return asyncio.run(run())


def _experiment_option_ids(experiment_ids):
    from app.db.supymem_models import Experiment
    from app.state import db

    with db()() as session:
        return {json.loads(session.get(Experiment, experiment_id).context)["spec"]["option_id"] for experiment_id in experiment_ids}


@pytest.mark.parametrize("audience,problem,name,width", [
    pytest.param("warehouse teams", "tracking inventory shortages", "Stock Ledger", 1280, id="b2b-inventory"),
    pytest.param("home gardeners", "tracking watering schedules", "Plant Journal", 375, id="consumer-gardening"),
])
@pytest.mark.parametrize("enabled", [True, False], ids=["flag-on", "flag-off"])
def test_confirmed_idea_exploration_journey(rendered_core, monkeypatch, tmp_path, audience, problem, name, width, enabled):
    playwright = pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import expect
    from app.authz.workspace import ensure_subject_person
    from app.credits.accounts import ensure_account
    from app.credits.policy import set_policy
    from app.db.models import CreditAccount, Job
    from app.experiments import autonomous, exploration
    from app.gtm import journey
    from app.signals.research import SearchResults

    base = os.environ["GTM_CONSOLE_URL"].rstrip("/")
    assert urlsplit(base).hostname in {"127.0.0.1", "localhost", "::1"}, "isolated loopback Console required"
    original, client, workers, sends = rendered_core
    actor = "usr_exploration_browser"
    ensure_subject_person(actor)
    fixture = ({**original, "actor": actor}, client, workers, sends)
    set_policy("feature_flags", {"exploration_v1": enabled}, actor)
    monkeypatch.setattr(journey, "_icp_seed", original["stored_seed"])

    def read(core_path):
        response = client.get(core_path, headers=headers(urlsplit(core_path).path, subject=actor))
        assert response.status_code == 200, (core_path, response.status_code, response.text)
        return response.json()

    assert read("/ventures")["items"] == []
    idea = f"A startup tool for {audience} to help with {problem}."
    provider_calls = []

    class Provider:
        name = "firecrawl"

        async def search(self, query, **kwargs):
            provider_calls.append(query)
            await asyncio.sleep(0)
            return SearchResults([], provider_rows=0, rejected_shape=0, rejected_missing_url=0,
                                 usage={"firecrawl_self_hosted": True})

    provider = Provider()
    monkeypatch.setattr(autonomous, "_provider", lambda: provider)

    with playwright.sync_playwright() as runner:
        browser = runner.firefox.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 900})
        requests = _install_forwarder(page, base, fixture)
        page.goto(base + "/new", wait_until="domcontentloaded")
        _skip_intro(page)
        page.get_by_role("textbox", name="What is your idea?", exact=True).fill(idea)
        with page.expect_response(lambda response: response.url.endswith("/entry/chat/preview")
                                  and response.request.method == "POST") as preview:
            page.get_by_role("button", name="Continue", exact=True).click()
        assert preview.value.status == 200
        proposal = preview.value.json()["proposal"]
        assert proposal["route"] == "startup" and proposal["state"] == "pending"
        assert proposal["target_venture_id"] is None
        page.get_by_role("textbox", name="Your venture name", exact=True).fill(name)
        with page.expect_response(lambda response: response.url.endswith("/entry/chat/confirm")
                                  and response.request.method == "POST") as confirmation:
            page.get_by_role("button", name="Continue", exact=True).click()
        assert confirmation.value.status == 200
        receipt = confirmation.value.json()
        assert receipt["status"] == "committed" and receipt["created_new"] is True
        venture_id = receipt["venture_id"]
        saved = read(f"/ventures/{venture_id}")
        assert saved["name"] == name and saved["raw_idea"] == idea
        expect(page.get_by_role("heading", name=name, exact=True)).to_be_visible(timeout=30_000)
        card = page.get_by_role("region", name="Your three ideas", exact=True)
        chat_path = f"/ventures/{venture_id}/chat"

        if not enabled:
            with page.expect_response(lambda response: urlsplit(response.url).path.endswith(chat_path)
                                      and response.request.method == "GET") as history:
                page.reload(wait_until="domcontentloaded")
            assert history.value.status == 200
            assert not [message for message in history.value.json()["items"] if message["kind"] == "exploration_options"]
            expect(card).to_have_count(0)
            expect(page.get_by_role("button", name="Start all three", exact=True)).to_have_count(0)
            with original["factory"]() as session:
                assert not list(session.scalars(select(Job).where(Job.venture_id == venture_id,
                    Job.kind.in_(("experiment.exploration.prepare", autonomous.KIND)))))
            assert not provider_calls and not sends
            browser.close()
            return

        account = ensure_account(venture_id, workspace_id=receipt["workspace_id"], owner_person_id=actor)
        with original["factory"]() as session:
            session.get(CreditAccount, account.id).granted_micro = 10_000_000
            session.commit()

        async def ideas(messages, **kwargs):
            assert kwargs["purpose"] == "experiment.exploration" and kwargs["venture_id"] == venture_id
            inputs = json.loads(messages[1]["content"])
            assert inputs["icp"]["who"] == audience
            drafts = [{"type": "who", "statement": f"You could serve group {index + 1} of {audience} to help with {problem}.",
                       "audience": f"Group {index + 1} of {audience}", "problem": problem, "confidence": 0.2}
                      for index in range(3)]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"beliefs": drafts})))])

        monkeypatch.setattr(exploration.metered, "llm", ideas)
        # Market tests wait until the idea is understood. Entry confirm alone leaves intake open.
        from tests.unit.test_exploration_wiring import complete_startup_intake
        complete_startup_intake(venture_id)
        prepared = workers.submit(_run_queued_exploration, receipt, actor, "experiment.exploration.prepare").result(timeout=60)
        assert len(prepared) == 1
        expect(card).to_be_visible(timeout=30_000)
        expect(card).to_have_count(1)
        messages = [message for message in read(chat_path)["items"] if message["kind"] == "exploration_options"]
        assert len(messages) == 1 and len(messages[0]["options"]) == 3
        round_id = messages[0]["round_id"]
        options = card.get_by_role("list").first.get_by_role("listitem")
        expect(options).to_have_count(3)
        for option in messages[0]["options"]:
            expect(card.get_by_text(option["statement"], exact=True)).to_be_visible()
        with page.expect_response(lambda response: response.url.endswith(chat_path + "/confirm")
                                  and response.request.method == "POST") as started:
            card.get_by_role("button", name="Start all three", exact=True).click()
        assert started.value.status == 200
        assert started.value.request.post_data_json["action"] == "experiment.exploration.start"
        assert started.value.request.post_data_json["payload"]["round_id"] == round_id
        started_round = started.value.json()
        assert started_round["kind"] == "exploration_options" and started_round["round_id"] == round_id
        assert started_round["state"] == "running" and {option["state"] for option in started_round["options"]} == {"running"}
        for index in range(3):
            expect(options.nth(index).get_by_text("Testing", exact=True)).to_be_visible(timeout=30_000)

        jobs = workers.submit(_run_queued_exploration, receipt, actor, autonomous.KIND).result(timeout=60)
        option_ids = workers.submit(_experiment_option_ids, [job.payload["experiment_id"] for job in jobs]).result(timeout=30)
        assert option_ids == {option["id"] for option in messages[0]["options"]}
        assert provider_calls
        activity = read(f"/ventures/{venture_id}/activity?phase=experiments&kind=option_activity")["items"]
        for index in range(3):
            recorded = [item for item in activity if item["detail"]["option_id"] == messages[0]["options"][index]["id"]]
            assert any(item["detail"]["action"] == "search" and item["detail"]["outcome"] == "succeeded" for item in recorded)
            progress = options.nth(index).get_by_role("status")
            expect(progress.get_by_text("Researched", exact=True)).to_be_visible(timeout=30_000)
            expect(progress.locator("time[datetime]")).to_have_count(1)
            displayed_start = progress.locator("time").get_attribute("datetime")
            expected_start = datetime.fromisoformat(min(item["detail"]["started_at"] for item in recorded))
            assert displayed_start == expected_start.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        expect(card).to_have_count(1)
        assert len([message for message in read(chat_path)["items"] if message["kind"] == "exploration_options"]) == 1
        _screenshot(page, tmp_path, f"exploration-chat-{width}.png")

        page.goto(base + f"/v/{venture_id}/market-map", wait_until="domcontentloaded")
        expect(page.get_by_role("heading", name="Experiments", exact=True)).to_be_visible(timeout=30_000)
        rounds = page.get_by_role("region", name="Your rounds", exact=True)
        expect(rounds.locator("details.panel-card")).to_have_count(1)
        expect(rounds.locator("summary").first).to_contain_text("Round 1")
        listed = read(f"/ventures/{venture_id}/experiments/rounds")["rounds"]
        assert len(listed) == 1 and listed[0]["id"] == round_id
        for option in messages[0]["options"]:
            expect(rounds.get_by_role("region", name=option["statement"], exact=True)).to_be_visible()
        assert ("GET", f"/ventures/{venture_id}/experiments/rounds") in requests
        assert not sends
        _screenshot(page, tmp_path, f"exploration-market-map-{width}.png")
        browser.close()
