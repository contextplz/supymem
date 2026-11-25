import pytest
from tests.unit.chat.test_round3_matrix import venture as venture  # noqa: F401
from tests.company_os_harness import load_company_os_app, local_chat as local_chat
load_company_os_app()

REQUEST = 'The fresh stored-source QA run bsi_8471afb1aeac98797dce92ab9707cbb0de1a5479992f3ecb is closed failed, with cleanup complete, zero reserved and 0.39 credits shown. Its real browser report has 26 passed, 12 failed and zero unsupported checks. It flags missing exact planned three-step text, a missing substantial workflow illustration, missing platform analytics, and the managed waitlist link needing target=_blank with noopener noreferrer. The source still describes an unlaunched concept; it is not accepted or published. The native coding instructions did not include the exact platform analytics recipe. What are the simple next steps and what instructions must you supply before a reviewed source correction? Advice only: do not prepare or run another coding task, retry QA, change the plan, accept anything or publish. Preserve this failed run and its actual charge.'

def test_exact_live_request_is_read_only_and_requires_owned_evidence(venture, monkeypatch):
    from app.chat.intent import classify_intent, split_speech_acts
    from app.db.models import Job, PlannedTask
    from app.state import db
    from sqlalchemy import select
    client, vid, _ = venture
    def forbidden(*a, **kw):
        pytest.fail('QA correction advice must not search, model, or return manual guide')
    monkeypatch.setattr('app.chat.assistant._complete', forbidden)
    monkeypatch.setattr('app.research.explore.explore_web', forbidden)
    monkeypatch.setattr('app.chat.search_recovery.manual_discovery_reply', forbidden)
    assert classify_intent(REQUEST) == 'assistant'
    assert split_speech_acts(REQUEST) == [('assistant', REQUEST)]
    with db()() as session:
        before = (list(session.scalars(select(Job.id))), list(session.scalars(select(PlannedTask.id))))
    reply = client.post(f'/ventures/{vid}/chat', json={'message': REQUEST})
    assert reply.status_code == 200, reply.text
    assert 'cannot ground' in reply.json()['body_md']
    assert 'Evidence → Builds → Review QA results' in reply.json()['body_md']
    assert reply.json()['actions'] == []
    with db()() as session:
        assert (list(session.scalars(select(Job.id))), list(session.scalars(select(PlannedTask.id)))) == before

@pytest.mark.parametrize('tail', [' Then prepare a coding task.', ' But retry QA.', ' Instead publish it.', ' Also search for another solution.'])
def test_positive_command_cannot_enter_advice_only_seam(tail):
    from app.chat.static_qa_guidance import request_run_id
    assert request_run_id(REQUEST + tail) is None


def test_json_marker_cannot_exempt_untrusted_model_text_from_scrubbing(venture, monkeypatch):
    from app.chat import router
    client, vid, _ = venture
    async def untrusted(*args, **kwargs):
        return {'kind': 'text', 'body_md': '2. A reminder is sent to the client.',
            'verified_static_qa_evidence': True, 'actions': [], 'citations': []}
    monkeypatch.setattr(router, '_dispatch_intent', untrusted)
    monkeypatch.setattr('app.chat.claims.claims_action', lambda body: False)
    response = client.post(f'/ventures/{vid}/chat', json={'message': REQUEST})
    assert response.status_code == 200, response.text
    assert '2. A reminder is prepared to the client.' in response.json()['body_md']
    assert '2. A reminder is sent to the client.' not in response.json()['body_md']
