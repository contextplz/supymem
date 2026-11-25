"""Ordinary supplied interview progress remains advice, not a count lookup."""
import pytest
from tests.unit.chat.test_assistant_routing import env as env  # noqa: F401
from tests.company_os_harness import local_chat as local_chat

REQUEST = "I sent the reviewed general-workflow interview invitation to Hulk Fitness team at its own publicly listed business address fitness@consumer.example, using the latest saved draft with only the greeting and Sai Vishwas signature filled. Gmail confirmed Message sent at about 09:47 IST on 2 October 2026. Gyms now has one invitation sent, zero observed replies, zero interviews and zero users. Ownership, member count, manual process and pain are still unknown. This is an adjacent gym discovery invitation, not a qualified or validated customer. Nothing else was sent or scheduled. What is the next appropriate step using only these supplied facts?"


@pytest.mark.parametrize('question', [REQUEST, REQUEST+' Do not search, send, build or publish anything.',
    REQUEST.replace('Hulk Fitness team', 'family walking group').replace('Gyms now', 'Walking groups now')
    .replace('adjacent gym discovery', 'family recreation discovery').replace('member count', 'participant count')])
def test_exact_live_founder_report_uses_full_advice_context_without_effect_or_metrics_fallback(env, monkeypatch, question):
    from app.chat import assistant
    from app.chat.intent import discovery_progress_question, split_speech_acts
    from app.db.models import Job, PlannedTask
    from app.state import db
    from sqlalchemy import select
    client, vid = env
    seen = []
    async def complete(messages, **_kw):
        seen.append(messages)
        return 'Keep unknown fit. Wait two to three business days before checking for a voluntary reply; no interview or user is established.'
    def forbidden(*_a, **_kw):
        pytest.fail('progress advice must not become saved metrics, manual guide or web research')
    monkeypatch.setattr(assistant, '_complete', complete)
    monkeypatch.setattr(assistant, '_venture_metrics_answer', forbidden)
    monkeypatch.setattr('app.chat.search_recovery.manual_discovery_reply', forbidden)
    monkeypatch.setattr('app.research.explore.explore_web', forbidden)
    assert discovery_progress_question(question)
    assert split_speech_acts(question) == [('assistant', question)]
    with db()() as session:
        jobs = list(session.scalars(select(Job.id)))
        tasks = list(session.scalars(select(PlannedTask.id)))
    response = client.post(f'/ventures/{vid}/chat', json={'message': question})
    assert response.status_code == 200, response.text
    assert len(seen) == 1, response.json()['body_md']
    context = '\n'.join(x['content'] for x in seen[0])
    assert question in context and 'founder-reported' in context
    assert 'not independent verification' in context
    assert response.json()['actions'] == [] and 'two to three' in response.json()['body_md']
    with db()() as session:
        assert list(session.scalars(select(Job.id))) == jobs
        assert list(session.scalars(select(PlannedTask.id))) == tasks


@pytest.mark.parametrize('extra', [' Then search for gyms.', ' Send more invitations now.', ' But publish a page.', ' Then contact a new person.', ' Then find more prospects.'])
def test_reported_progress_never_swallows_positive_operations(extra):
    from app.chat.intent import discovery_progress_question
    assert not discovery_progress_question(REQUEST+extra)


@pytest.mark.parametrize('text', [
    REQUEST.rsplit('What is',1)[0]+'How many verified active product users do we have?',
    'I sent money to a vendor. What is the next step?',
    'I sent an interview invitation. Search for another contact.',
])
def test_other_metrics_payment_and_effect_requests_do_not_get_progress_override(text):
    from app.chat.intent import discovery_progress_question
    assert not discovery_progress_question(text)
