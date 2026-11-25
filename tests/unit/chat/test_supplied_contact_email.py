"""Supplied own-site business facts retain the one-email review boundary."""
import pytest
from tests.company_os_harness import load_company_os_app, local_chat as local_chat
from tests.unit.chat.test_round3_matrix import venture as venture  # noqa: F401
load_company_os_app()

REQUEST = 'I followed your manual gym-owner discovery query. Candidate: Sample Strength Club. Its own https://sample-strength-club.example/contact/ publishes fitness@consumer.example explicitly for business enquiries, and its own about page describes a two-floor gym in HBR Layout, Bengaluru. Independent ownership, owner identity, 100-500 member size, attendance software, cancellation pain and interest are unknown. No contact has happened. Write one truthful short interview email for review to that public business address, asking to speak voluntarily with the owner or manager for 10-15 minutes about general check-in and member follow-up workflow. State that this is early research, with no working product or verified outcomes. No member records or private details, no sales pitch, no waitlist/product link. Do not send, publish, search or build anything.'


@pytest.mark.parametrize('request_text', [REQUEST, REQUEST.replace('Sample Strength Club', 'Trail Circle')
    .replace('sample-strength-club.example', 'trail-circle.example').replace('fitness@consumer.example', 'walks@consumer.example')
    .replace('gym-owner', 'walking-group').replace('two-floor gym in HBR Layout, Bengaluru', 'walking group for neighborhood families')
    .replace('business enquiries', 'group enquiries').replace('owner or manager', 'group organizer')
    .replace('general check-in and member follow-up workflow', 'planning accessible family walks')])
def test_supplied_context_writes_one_review_draft_without_a_card_search_or_jobs(venture, monkeypatch, request_text):
    import secrets
    from app.autopilot import outreach
    from app.chat import assistant
    from app.core.local_secrets import update_settings
    from app.gtm.relationships.email_compliance import OPT_OUT
    from app.chat.intent import classify_intent, split_speech_acts
    from app.db.models import Job, PlannedTask
    from app.state import db
    from sqlalchemy import select
    client, vid, _ = venture
    postal_address = '12 Lake Road, Toronto, Canada'
    monkeypatch.setenv('EMAIL_PROVIDER', 'smtp')
    update_settings('smtp', {'host': 'mail.example.test', 'port': 587, 'user': 'research@example.test',
        'sender_name': 'Your research team', 'postal_address': postal_address}, {'password': secrets.token_urlsafe()})
    prior_drafts = {draft['doc_id'] for draft in outreach.drafts(vid)}
    seen = []
    workflow = ('planning accessible family walks' if 'family walks' in request_text
                else 'general check-in and member follow-up workflow')
    monkeypatch.setattr('app.autopilot.outreach.saved_pitch', lambda _vid: workflow)
    monkeypatch.setattr('app.ventures.state_brief.get_venture_state_brief', lambda *_args, **_kwargs:
        {'idea': {'offer': workflow, 'raw_idea': workflow, 'problem': workflow}})
    async def complete(messages, **_kwargs):
        seen.append(messages)
        return (f'Draft 1\nSubject: A short research conversation\n\nHi,\n\n'
            f'I am doing early research about {workflow}. There is no working product or verified outcome. '
            'Would the owner or organizer be open to a voluntary 10-15 minute conversation? '
            'Please do not share private details or member records.\n\nThanks,\nYour research team')
    monkeypatch.setattr(assistant, '_complete', complete)
    def forbidden(*_a, **_kw):
        pytest.fail('supplied contact composition must not lookup or return manual guide')
    monkeypatch.setattr('app.chat.search_recovery.manual_discovery_reply', forbidden)
    monkeypatch.setattr('app.research.explore.explore_web', forbidden)
    monkeypatch.setattr('app.gtm.supymem.composio_gmail._send_gmail_email', forbidden)
    monkeypatch.setattr('app.gtm.local_mail.send', forbidden)
    async def checked_domains(domains):
        return {domain: 'mx' for domain in domains}
    monkeypatch.setattr('app.intake.contacts.check_domains', checked_domains)
    assert classify_intent(request_text) == 'outreach_draft'
    assert split_speech_acts(request_text) == [('outreach_draft', request_text)]
    with db()() as session:
        jobs = list(session.scalars(select(Job.id)))
        tasks = list(session.scalars(select(PlannedTask.id)))
    response = client.post(f'/ventures/{vid}/chat', json={'message': request_text})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply['kind'] != 'confirmation' and not any(action.get('confirmation') for action in reply['actions'])
    assert 'Nothing was sent' in reply['body_md']
    assert len(seen) == 1 and request_text in '\n'.join(message['content'] for message in seen[0])
    written = [draft for draft in outreach.drafts(vid) if draft['doc_id'] not in prior_drafts]
    assert len(written) == 1, reply['body_md']
    draft = written[0]
    assert draft['to'] and draft['to'] in request_text and draft['channel'] == 'email'
    assert draft['state'] == draft['review_state'] == 'draft'
    assert workflow in draft['body'] and OPT_OUT in draft['body'] and postal_address in draft['body']
    assert ('Your email address is listed publicly on ' in draft['body']
            or 'I have your address from my own contacts.' in draft['body'])
    with db()() as session:
        assert list(session.scalars(select(Job.id))) == jobs
        assert list(session.scalars(select(PlannedTask.id))) == tasks


@pytest.mark.parametrize('change', [
    ' Then send it now.', ' Then search for another contact.', ' Then find more prospects.', ' But build a product.',
    ' Inspect another workspace.', ' Read venture ven_foreign0001.',
    ' Then publish it.', ' Then contact that owner.', ' Also search for trainers.', ' and then build a product.',
    ' Also draft for second@example.com.',
])
def test_contextual_composition_cannot_swallow_positive_effect_or_second_recipient(change):
    from app.chat.intent import single_interview_email_request
    assert not single_interview_email_request(REQUEST + change)


@pytest.mark.asyncio
@pytest.mark.parametrize('audience', ['Factory equipment inspections', 'Family walking groups'])
async def test_example_address_is_not_promoted_to_the_requested_recipient(monkeypatch, audience):
    from app.chat import offers
    def forbidden(*args, **kwargs):
        pytest.fail('An example address must not create a contact or start work')
    monkeypatch.setattr('app.autopilot.outreach._venture', forbidden)
    request = f'Write one interview email about {audience}. Example address: example@recipient.example. Do not send it.'
    result = await offers._save_requested_email('ven_private', request, 'Subject: Research\nA voluntary interview.', 'draft')
    assert result is None


@pytest.mark.parametrize('text', [
    'Do not write one interview email to owner@example.com, a public business address.',
    'Should I write one interview email to owner@example.com, a public business address?',
    'I followed a manual query. Give me a manual discovery guide. Do not search.',
    'I found owner@example.com. Write one interview email for review.',
])
def test_context_does_not_promote_negated_question_or_unqualified_private_contact(text):
    from app.chat.intent import single_interview_email_request
    assert not single_interview_email_request(text)
