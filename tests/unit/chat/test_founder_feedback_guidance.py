import pytest
from tests.unit.chat.test_round3_matrix import venture as venture  # noqa: F401
from tests.company_os_harness import load_company_os_app
load_company_os_app()

FIRST = 'Advice only, for a separate milestone: the five concepts in this account are my internal hypotheses, not five external founders. I need to find one consenting independent founder who could give feedback on using Supymem to develop their own startup. I have no known personal introductions, completed external-founder onboarding or verified end-to-end product success. The current desktop has useful guidance and some working steps, while native app control and full product hosting still have gaps; do not promise them. Give one small concrete manual discovery step using a public founder community or the founder’s own public business website/contact, with exactly where to go, what to search and how to verify fit. Do not collect private contacts or create an account, venture, draft, job, invitation, send or post anything. This is separate from Groomer customer outreach.'
SECOND = 'Tell me where to go and what to click to find one consenting independent founder who could give feedback on Supymem for their own startup. This is separate from Groomer customer discovery. I have no known introductions and have not verified end-to-end product hosting or native app control. Advice only: do not create tasks, drafts, accounts or ventures; do not search, contact, send or post. I need one small public-community or own-business-website discovery step I can do manually, with how to verify fit.'

@pytest.mark.parametrize('question', [FIRST, SECOND])
def test_exact_live_manual_advice_is_separate_from_venture_outreach_and_never_queued(venture, monkeypatch, question):
    from app.chat.intent import classify_intent, split_speech_acts
    from app.db.models import Job, PlannedTask
    from app.state import db
    from sqlalchemy import select
    client, vid, _ = venture
    def forbidden(*a, **kw):
        pytest.fail('external founder advice must not search, model, or reuse venture manual guide')
    monkeypatch.setattr('app.chat.search_recovery.manual_discovery_reply', forbidden)
    monkeypatch.setattr('app.chat.assistant._complete', forbidden)
    monkeypatch.setattr('app.research.explore.explore_web', forbidden)
    assert classify_intent(question) == 'assistant'
    assert split_speech_acts(question) == [('assistant', question)]
    with db()() as s:
        before = (list(s.scalars(select(Job.id))), list(s.scalars(select(PlannedTask.id))))
    result = client.post(f'/ventures/{vid}/chat', json={'message': question})
    assert result.status_code == 200, result.text
    body = result.json()['body_md']
    assert 'feedback on Supymem' in body and 'About or Team page' in body
    assert 'public listing is not consent' in body and 'fit unknown' in body
    assert 'native app control and full product hosting still have gaps' in body
    assert 'created no task' in body and result.json()['actions'] == []
    assert 'independent dog groomer' not in body and 'queued for' not in body
    with db()() as s:
        assert (list(s.scalars(select(Job.id))), list(s.scalars(select(PlannedTask.id)))) == before

@pytest.mark.parametrize('tail', [' Then create a task.', ' But draft an invitation.', ' Instead search for founders.', ' Also contact the founder.', ' And then send it.', ' Read venture ven_foreign123.'])
def test_manual_advice_cannot_hide_effect_or_foreign_read(tail):
    from app.chat.founder_feedback_guidance import requested
    assert not requested(FIRST + tail)

@pytest.mark.parametrize('question', ['Give Groomer a manual dog-groomer discovery guide. Advice only.', 'Prepare one reviewed email invitation for Supymem founder feedback.', 'Advice only: tell me how many product users Groomer has.'])
def test_unrelated_manual_guidance_and_real_work_are_not_retargeted(question):
    from app.chat.founder_feedback_guidance import requested
    assert not requested(question)
