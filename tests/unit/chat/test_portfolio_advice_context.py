"""A portfolio comparison keeps supplied facts instead of one-venture status shortcuts."""
from __future__ import annotations

import pytest

from tests.unit.chat.test_assistant_routing import env as env  # noqa: F401
from tests.company_os_harness import local_chat as local_chat, local_owner_clients as local_owner_clients
from tests.unit.test_autonomous_experiments import venture as owner_venture

QUESTION = ("Give a read-only portfolio comparison and prioritize the next experiment using only these supplied facts. "
    "LabHire: a live concept landing and zero new waitlist requests, no working product. "
    "Studio Slots: three invitations sent, zero replies or interviews and unknown solo customer fit. "
    "Provider Planner: one publicly listed adult provider with licensing unknown; anonymized verbal discovery only, "
    "no child or family records. Rank the five by fastest next learning step and distinguish a working product from a page. "
    "This request is advice only, not validated customer demand.")


@pytest.mark.parametrize("audience", ["hotel shift managers", "recreational trail runners"])
@pytest.mark.parametrize("prefix", ["Save the comparison test outcome: ", ""])
def test_comparison_in_report_records_results_only_with_saved_receipt(owner_venture, monkeypatch, audience, prefix):
    from app.chat import assistant, experiments
    from app.credits.policy import set_policy
    from app.experiments import ledger
    from core.clock import utcnow

    client, venture_id, _workspace_id = owner_venture
    set_policy("feature_flags", {"experiments_quality_v1": True, "experiments_autopilot_v1": True}, "test")
    ledger.create_from_tests(venture_id, "chat", [{"hypothesis": f"{audience} will try your offer"}])
    experiment = ledger.list_for_venture(venture_id)[0]
    ledger.start(experiment["id"], venture_id)
    report = (f"{prefix}I spoke with eight {audience}. Five said they would compare your offer "
        "with their usual approach before deciding. I finished the test and kept dated notes today.")
    advice_calls = []

    async def extract(*_args, **_kwargs):
        return {"ran": True, "outcome": "inconclusive", "learning": "You have interest, not a purchase.",
            "sample_size": 8, "provenance": {"source": "Your dated notes", "method": "Interviews",
                "observed_at": utcnow().isoformat()}}

    async def advice(*_args, **_kwargs):
        advice_calls.append(_args)
        return "Your results were recorded. Next: review your test."

    monkeypatch.setattr(experiments, "_llm_json", extract)
    monkeypatch.setattr(assistant, "_complete", advice)
    record_result = ledger.record_result
    monkeypatch.setattr(ledger, "record_result", lambda *_args, **_kwargs: None)
    failed = client.post(f"/ventures/{venture_id}/chat", json={"message": report})
    assert failed.status_code == 200, failed.text
    assert "Your final result is not saved yet" in failed.json()["body_md"]
    assert "Review your test" in failed.json()["body_md"]
    assert ledger.get(experiment["id"], venture_id)["state"] == "running"
    assert not ledger.get(experiment["id"], venture_id).get("result_text")
    monkeypatch.setattr(ledger, "record_result", record_result)
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": report})
    assert response.status_code == 200, response.text
    saved = ledger.get(experiment["id"], venture_id)
    assert saved["state"] == "concluded" and saved["outcome"] == "inconclusive"
    assert saved["result_text"] == report
    assert saved["learning"] == ('You have interest, not a purchase. You have not described what someone has done or committed to yet. '
        'Kind words alone do not show demand. Next time, ask: "What happened the last time you dealt with this problem?" (reported by you)')
    assert "Saved what you reported" in response.json()["body_md"]
    assert not advice_calls
    for reply in (failed.json(), response.json()):
        assert not any(action.get("confirmation") for action in reply["actions"])
        assert "credit" not in reply["body_md"].lower()
    comparison = f"Compare your concepts using only my descriptions. {report[len(prefix):]}"
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": comparison})
    assert response.status_code == 200, response.text
    assert len(advice_calls) == 1
    assert ledger.get(experiment["id"], venture_id) == saved


def test_portfolio_live_page_and_product_context_reaches_advice_without_status_shortcut(env, monkeypatch):
    client, vid = env
    from app.chat import assistant
    from app.state import db
    from app.db.models import Job
    from sqlalchemy import select
    captured = []
    async def complete(messages, **_kwargs):
        captured.append(messages)
        return "Prioritize one consenting adult interview; the supplied pages and invitations establish no working product or validated demand."
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("portfolio advice cannot authorize a search")
    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr("app.research.explore.explore_web", forbidden)
    with db()() as session:
        before = list(session.scalars(select(Job.id)))
    reply = client.post(f"/ventures/{vid}/chat", json={"message": QUESTION})
    assert reply.status_code == 200, reply.text
    assert len(captured) == 1
    prompt = "\n".join(message["content"] for message in captured[0])
    assert QUESTION in prompt
    assert "Other venture records have not been inspected" in prompt
    assert "founder\'s supplied descriptions" in prompt
    assert "Prioritize one consenting adult interview" in reply.json()["body_md"]
    assert reply.json()["actions"] == []
    with db()() as session:
        assert list(session.scalars(select(Job.id))) == before


LIVE_FULL = ("Compare our five distinct startup concepts and choose the next action that can most quickly produce honest user evidence. "
"Founder-reported portfolio: Kennel Scheduler and Club Attendance have public early landing pages but no verified working products or product users; "
"Groomer page replacement is under native review and its managed waitlist has zero consented requests. Studio Slots, for solo trainers, has three public-business interview invitations sent, "
"zero replies/interviews/users, unknown solo/manual-workflow fit. Provider Planner has one invitation sent to Maple Room Childcare, zero replies/interviews/users, "
"unknown licensed/home-provider size fit, and adult-only anonymous workflow discovery with no childcare observation or private records. Home Crew Calendar, for solo cleaners, "
"has no outreach, interview, product or users. Every problem and willingness-to-pay claim remains unvalidated. Rank the next acquisition action across these five and explain what would change the priority. "
"Use only these supplied descriptions and saved current-venture evidence; do not search, build, publish, send anything or pretend you inspected other ventures.")
LIVE_SIMPLE = ("Compare these five unvalidated ideas using only my descriptions below and choose one next acquisition action. Kennel Scheduler: appointment reminders for dog groomers, public landing page, "
"zero waitlist requests and zero product users. Club Attendance: gym retention concept with a public landing page and zero product users. Studio Slots: solo-trainer scheduling concept; "
"three interview invitations sent, zero replies, unknown solo/manual fit. Home Crew Calendar: solo-cleaner scheduling concept; zero invitations or interviews. Provider Planner: childcare-provider admin concept; "
"one interview invitation sent, no reply or interview, unknown provider fit; only adult anonymous general workflow conversations permitted. No working product, validated pain, payment or product user for any idea. "
"Rank the next outreach action and give the exact first step. Advice only; no search, build, publishing or sending.")


LIVE_FACTS_SUPPLIED = 'Compare and rank these five startup concepts using only the facts supplied in this message. KennelScheduler: independent dog groomers, booking and reminder concept; an unlaunched static landing source exists, but has not passed source QA or been published; one voluntary interview invitation sent, no replies or users. Studio Slots: solo personal trainers, rescheduling concept; three interview invitations sent to publicly listed adjacent prospects, no replies, completed interviews, users or payments. Home Crew Calendar: solo house cleaners who book by messages, rescheduling concept; one adjacent cleaning-company interview invitation sent, no reply or verified solo prospect. Provider Planner: licensed home childcare providers with four to six children, adult administrative workflow concept; one invitation sent, licensing and home-provider fit unknown, no reply or product. Club Attendance: independent gym owners with 100-500 members, declining attendance concept; no owner invitation or product adoption yet. All five are unvalidated, with zero completed interviews, paying customers or measured savings. Rank the first small discovery experiment for each by speed to evidence and specify one next action per concept. This is a supplied-facts comparison; do not inspect other ventures, search, send, build or publish anything.'


@pytest.mark.parametrize('question', [LIVE_FULL, LIVE_SIMPLE, LIVE_FACTS_SUPPLIED])
def test_exact_supplied_five_concept_comparison_precedes_manual_guide_and_foreign_inventory(env, monkeypatch, question):
    client, vid = env
    from app.chat import assistant
    from app.state import db
    from app.db.models import Job
    from sqlalchemy import select
    captured = []
    async def complete(messages, **_kwargs):
        captured.append(str(messages))
        return 'Use the supplied descriptions to prioritize one anonymous adult interview; invitations and pages are not verified users.'
    def forbidden(*_args, **_kwargs):
        raise AssertionError('supplied-only comparison must not read other venture state or return a lookup guide')
    monkeypatch.setattr(assistant, '_complete', complete)
    monkeypatch.setattr('app.chat.other_venture.other_venture_reply', forbidden)
    monkeypatch.setattr('app.chat.search_recovery.manual_discovery_reply', forbidden)
    monkeypatch.setattr('app.research.explore.explore_web', forbidden)
    with db()() as session:
        before = list(session.scalars(select(Job.id)))
    reply = client.post(f'/ventures/{vid}/chat', json={'message': question})
    assert reply.status_code == 200 and len(captured) == 1
    assert question in captured[0] and 'Other venture records have not been inspected' in captured[0]
    assert 'invitations sent versus prepared' in captured[0]
    assert 'Do not replace distinct counts with a shared summary' in captured[0]
    assert 'supplied descriptions' in reply.json()['body_md'] and reply.json()['actions'] == []
    with db()() as session:
        assert list(session.scalars(select(Job.id))) == before


@pytest.mark.parametrize('suffix', [
    'Read other founders ventures and compare their private records.',
    'Inspect another workspace and retrieve its saved records.',
    'Read venture ven_foreign0001 before ranking.',
    'Do not search, but then inspect other ventures.',
])
def test_supplied_comparison_does_not_exempt_positive_foreign_inspection(env, monkeypatch, suffix):
    from app.chat import assistant
    from app.chat.other_venture import supplied_portfolio_context
    client, vid = env
    question = 'Compare five ideas using only my descriptions. ' + suffix
    assert not supplied_portfolio_context(question)
    async def forbidden(*_args, **_kwargs):
        raise AssertionError('foreign record request must not reach the model')
    monkeypatch.setattr(assistant, '_complete', forbidden)
    reply = client.post(f'/ventures/{vid}/chat', json={'message': question})
    assert reply.status_code == 200
    assert 'This chat controls one venture' in reply.json()['body_md']


@pytest.mark.parametrize('suffix', ['Then publish Studio Slots.', 'Send an email now.', 'Build a product for Provider Planner.', 'Search competitors now.', 'No working product, build one now.'])
def test_supplied_comparison_does_not_swallow_positive_effects(suffix):
    from app.chat.other_venture import supplied_portfolio_context
    assert not supplied_portfolio_context('Compare five ideas using only my descriptions. ' + suffix)


@pytest.mark.parametrize("suffix", ["Read another workspace.", "Do not inspect other ventures, search, send, build or publish anything. Then read other ventures.", "Do not inspect ven_foreign0001."])
def test_reversed_supplied_facts_still_rejects_foreign_authority(suffix):
    from app.chat.other_venture import supplied_portfolio_context
    assert not supplied_portfolio_context("Compare ideas using only the facts supplied in this message. " + suffix)


@pytest.mark.parametrize("constraint", ["", " Do not use saved current-venture evidence.", " Without saved current-venture evidence.", " Saved current-venture evidence must not be used.", " Use saved current-venture evidence. Do not use saved current-venture evidence.", " Use saved current-venture evidence. Saved current-venture evidence must never be included.", " Use saved current-venture evidence. Saved current-venture evidence should never be included.", " Use saved current-venture evidence. Saved current-venture evidence is never permitted."])
def test_supplied_only_model_context_excludes_saved_brief_and_recent_history(env, monkeypatch, constraint):
    from app.chat import assistant
    client, vid = env
    captured = []
    def no_brief(*_a, **_k):
        raise AssertionError('supplied-only advice must not fetch saved venture evidence')
    monkeypatch.setattr(assistant, 'get_venture_state_brief', no_brief)
    def no_history(*_a, **_k):
        raise AssertionError('supplied-only advice must exclude saved chat history')
    monkeypatch.setattr(assistant, 'list_messages', no_history)
    async def complete(messages, **_kwargs):
        captured.append(str(messages))
        return 'Studio Slots has three invitations sent; their reply window is pending. No known warm contacts were supplied.'
    monkeypatch.setattr(assistant, '_complete', complete)
    reply = client.post(f'/ventures/{vid}/chat', json={'message': LIVE_FACTS_SUPPLIED + constraint})
    assert reply.status_code == 200 and len(captured) == 1
    assert LIVE_FACTS_SUPPLIED in captured[0]
    assert 'PRIVATE_CURRENT_EXPERIMENT' not in captured[0] and 'PRIVATE_CTA' not in captured[0]
    assert 'Do not invent known contacts' in captured[0]
    assert 'Recent invitations with no replies do not justify an immediate follow-up' in captured[0]
    assert reply.json()['actions'] == []


def test_explicit_saved_current_venture_evidence_still_includes_current_brief(env, monkeypatch):
    from app.chat import assistant
    client, vid = env
    captured = []
    monkeypatch.setattr(assistant, 'get_venture_state_brief', lambda *_a, **_k: {
        'workspace_id': 'wsp_test', '_rendered': 'EXPLICITLY_REQUESTED_CURRENT_EVIDENCE',
    })
    monkeypatch.setattr(assistant, 'list_messages', lambda *_a, **_k: [])
    async def complete(messages, **_kwargs):
        captured.append(str(messages))
        return 'Compare the supplied descriptions using the explicitly requested current evidence.'
    monkeypatch.setattr(assistant, '_complete', complete)
    reply = client.post(f'/ventures/{vid}/chat', json={'message': LIVE_FULL})
    assert reply.status_code == 200 and 'EXPLICITLY_REQUESTED_CURRENT_EVIDENCE' in captured[0]


def test_advice_calendar_has_actual_weekday_dates_and_relative_fallback(env, monkeypatch):
    from datetime import datetime, timezone
    import datetime as calendar_module
    from app.chat import assistant
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 2, 3, 45, tzinfo=timezone.utc)
    monkeypatch.setattr(calendar_module, 'datetime', FixedDatetime)
    guidance = assistant._advice_calendar_context()
    assert 'Friday 2026-10-02' in guidance and 'Monday 2026-10-05' in guidance
    assert 'Tuesday 2026-10-06' in guidance and 'Monday 2026-10-06' not in guidance
    assert 'not an inferred founder timezone' in guidance and '2-3 business-day' in guidance
