"""Reported timing and explicit current audience stay in their scoped guide."""
import pytest
from tests.unit.chat.test_assistant_routing import env as env  # noqa: F401


@pytest.mark.parametrize("audience", ["Warehouse coordinators", "Families planning camping trips"])
def test_long_answer_only_request_does_not_create_a_deep_answer_task(env, monkeypatch, audience):
    import asyncio
    from app.chat import assistant
    from app.tasks import reasoning

    client, venture_id = env
    question = "Answer only. Do not create a task. " + (
        f"Explain the unanswered questions for {audience} without treating assumptions as evidence. " * 8)
    monkeypatch.setattr(reasoning, "wants_depth", lambda text: True)
    monkeypatch.setattr(reasoning, "start", lambda *args, **kwargs: pytest.fail("Advice must not create a task"))
    monkeypatch.setattr("app.chat.execute.local_capability_reply", lambda name: None)

    async def complete(messages, **kwargs):
        return "Keep those assumptions separate from evidence. Ask your participants about their experience next."

    monkeypatch.setattr(assistant, "_complete", complete)
    reply = asyncio.run(assistant.answer_question(venture_id, question, request_id="answer-only-regression"))
    assert "Keep those assumptions" in reply["body_md"] and reply["actions"] == []

TIMING = ('Discovery update for StudioSlot: three individual trainer interview invitations were sent only about two hours ago, overnight in India. '
    'There are zero replies, interviews, users or payments; solo/manual-workflow fit remains unknown. What exact next action should I take now, '
    'with a reasonable response window and a specific reply-check time? Do not change city just because nobody replied overnight. '
    'Do not search, send, build or publish anything.')
FOLLOWUP = ('The three invitations were sent only about two hours ago, overnight in India. Their lack of replies is not yet evidence against demand or email reachability. '
    'Refine the next action with a reasonable response window and a specific time to check for replies, without changing city solely because nobody replied overnight. '
    'Do not search, send, build or publish; advice only. Preserve unknown solo/manual-workflow fit and no interview/user proof.')


CLEANER_PROGRESS = ("CleanCrew discovery progress update: at 07:02 IST on 2 October I sent one voluntary non-sales interview invitation to Sample Cleaning Company at its own published business email info@sample-cleaning.test, using the earlier saved draft and only replacing greeting and founder-name placeholders. Gmail confirmed Message sent. It is an adjacent cleaning company, not a verified solo cleaner; messaging-based scheduling, pain and willingness to pay remain unknown. No reply or completed interview has been observed, and we have no product users or payments. The newly generated draft is only a routing check and has not been sent. It is now about 07:42 IST. What is the next small discovery step, and when should I check for replies? Do not send, search, build or publish anything.")


@pytest.mark.parametrize('question', [TIMING, FOLLOWUP, CLEANER_PROGRESS])
def test_actual_timing_requests_keep_complete_advisory_context_and_no_effects(env, monkeypatch, question):
    from app.chat import assistant
    from app.db.models import Job
    from app.state import db
    from sqlalchemy import select
    client, vid = env
    seen = []
    async def complete(messages, **_kwargs):
        seen.append(str(messages))
        return 'Treat overnight silence as pending, not proof. Choose a reasonable reply-check time; solo fit and demand remain unknown.'
    def forbidden(*_args, **_kwargs):
        raise AssertionError('timing advice cannot become a manual search guide or web call')
    monkeypatch.setattr(assistant, '_complete', complete)
    monkeypatch.setattr('app.chat.search_recovery.manual_discovery_reply', forbidden)
    monkeypatch.setattr('app.research.explore.explore_web', forbidden)
    with db()() as session:
        before = list(session.scalars(select(Job.id)))
    reply = client.post(f'/ventures/{vid}/chat', json={'message': question})
    assert reply.status_code == 200 and len(seen) == 1
    assert question in seen[0] and 'not independent verification' in seen[0]
    assert 'do not treat brief silence as evidence against demand' in seen[0]
    assert 'specific reply-check time' in seen[0]
    assert 'overnight silence' in reply.json()['body_md'] and reply.json()['actions'] == []
    with db()() as session:
        assert list(session.scalars(select(Job.id))) == before


@pytest.mark.parametrize('extra', [' Then search for trainers.', ' Send more invitations now.', ' But publish a page.', ' Then contact a new person.', ' Then find more prospects.'])
def test_timing_advice_does_not_swallow_positive_followup(extra):
    from app.chat.intent import discovery_progress_question
    assert not discovery_progress_question(TIMING + extra)
    assert not discovery_progress_question(FOLLOWUP + extra)
    assert not discovery_progress_question(CLEANER_PROGRESS + extra)


@pytest.mark.parametrize('question,query', [
    ('Give CleanCrew a manual Google-to-Gmail discovery guide for one solo or independent house cleaner in Bengaluru. Use anonymous verbal workflow descriptions, no private chat, contact lists or client addresses. Do not search.', '"independent house cleaner" "Bengaluru" website contact'),
    ('Give Gyms a manual Google-to-Gmail discovery guide for one independent gym owner in Bengaluru with 100-500 members. Adult-owner anonymous verbal workflow, no member data analysis or working product. Do not claim another trainer is a gym owner. Do not search.', '"independent gym owner" "Bengaluru" website contact'),
    ('Do not search. Find one solo cleaner in Mumbai manually. Do not look for trainers in Bengaluru.', '"independent house cleaner" "Mumbai" website contact'),
    ('Find one dog groomer in Bengaluru. Do not contact a trainer.', '"independent dog groomer" "Bengaluru" website contact'),
    ('Find one home childcare provider in Bengaluru. Do not observe children.', '"home childcare provider" "Bengaluru" website contact'),
])
def test_explicit_positive_request_role_beats_saved_or_negated_trainer(env, monkeypatch, question, query):
    from app.chat.search_recovery import manual_discovery_reply, discovery_query
    monkeypatch.setattr('app.ventures.state_brief.get_venture_state_brief', lambda *_: {'idea': {'audience': 'personal trainers', 'name': 'Current concept'}})
    reply = manual_discovery_reply(question, venture_id=env[1])
    assert query in reply['body_md']
    assert 'independent personal trainer' not in reply['body_md']
    assert 'reschedule a session' not in reply['body_md']
    assert 'private client chats, contact lists, client addresses' in reply['body_md']
    assert 'otherwise record them as unknown' in reply['body_md']
    if question.startswith('Find') or 'Find one' in question:
        assert discovery_query(question) == query


@pytest.mark.parametrize('question,role', [
    ('Give CleanCrew a manual Google-to-Gmail discovery guide for one solo or independent house cleaner in Bengaluru. Anonymous verbal workflow only, no private chat, contact lists or client addresses. Do not search.', 'independent house cleaner'),
    ('Give Gyms a manual Google-to-Gmail discovery guide for one independent gym owner in Bengaluru with 100-500 members. Adult owner anonymous verbal workflow; no working product or member data analysis. Do not claim another trainer is a gym owner. Do not search.', 'independent gym owner'),
])
def test_manual_current_audience_http_uses_exact_query_without_model_or_jobs(env, monkeypatch, question, role):
    from app.state import db
    from app.db.models import Job
    from sqlalchemy import select
    client, vid = env
    async def forbidden(*_args, **_kwargs):
        raise AssertionError('manual scoped guide must not call the model or search')
    monkeypatch.setattr('app.chat.assistant._complete', forbidden)
    monkeypatch.setattr('app.research.explore.explore_web', forbidden)
    with db()() as session:
        before = list(session.scalars(select(Job.id)))
    reply = client.post(f'/ventures/{vid}/chat', json={'message': question})
    assert reply.status_code == 200
    assert f'"{role}" "Bengaluru" website contact' in reply.json()['body_md']
    assert 'independent personal trainer' not in reply.json()['body_md']
    assert 'consenting adult' in reply.json()['body_md']
    assert all('action' not in action for action in reply.json()['actions'])
    with db()() as session:
        assert list(session.scalars(select(Job.id))) == before


@pytest.mark.parametrize('suffix', [' Inspect another workspace and retrieve its saved records.', ' Read venture ven_foreign0001 before advising.'])
def test_named_progress_does_not_authorize_foreign_records(env, monkeypatch, suffix):
    client, vid = env
    async def forbidden(*_args, **_kwargs):
        raise AssertionError('foreign record request must not reach model/search')
    monkeypatch.setattr('app.chat.assistant._complete', forbidden)
    monkeypatch.setattr('app.research.explore.explore_web', forbidden)
    reply = client.post(f'/ventures/{vid}/chat', json={'message': CLEANER_PROGRESS + suffix})
    assert reply.status_code == 200
    assert 'This chat controls one venture' in reply.json()['body_md']
