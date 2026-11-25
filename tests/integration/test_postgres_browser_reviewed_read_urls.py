"""Reviewed exact read URL authority joins normal Core actor/effect custody on owned PG."""
import json

import pytest
from pydantic import ValidationError

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.integration.test_postgres_browser_bounded_reads_controller import device, marked_request, read_order
from tests.integration.test_postgres_browser_qualification_hooks import signed_client
from tests.unit.product.test_device_browser_ai import response

URLS = ['https://pypi.org/project/requests/', 'https://github.com/psf/requests']


def url_order(d, monkeypatch, urls=URLS):
    from app.build import device_orders as orders
    caller, profile = device(d, monkeypatch)
    manifest = orders.Manifest(task_id='founder_browser_' + d['vid'], title='Reviewed public read URLs',
        instruction='Read the two exact reviewed public source pages. Do not submit or type.', applications=['com.google.Chrome'],
        origins=['https://pypi.org', 'https://github.com'], actions=['browser.read'], credit_ceiling_micro=500_000,
        duration_seconds=900, general_browser_verification={'operation': 'general_goal_v1', 'start_url': 'https://pypi.org/',
            'max_steps': 8, 'allow_type': False, 'fill_values': {}, 'read_urls': list(urls), 'execution_profile': profile.model_dump()})
    order = orders.prepare(d['vid'], caller, manifest)
    args = (d['vid'], caller, order['id'])
    for action in ('approve', 'claim'):
        order = orders.transition(*args, order['review_digest'], action)
    return orders, caller, order, args


def request(order):
    value = marked_request(order)
    value.context.read_urls = list(order['manifest']['general_browser_verification'].get('read_urls') or [])
    return value


def reply(url):
    return response(json.dumps({'actions': [{'op': 'navigate', 'text': url}], 'memory': '', 'subgoal_done': False}))


def test_signed_exact_cross_origin_read_url_has_normal_one_use_effect_custody(journey_db, monkeypatch):
    from app.build import device_browser_ai as ai, device_effects as effects
    d = journey_db
    orders, caller, order, args = url_order(d, monkeypatch)
    value = request(order)
    sent = []
    monkeypatch.setattr(ai, '_provider_call', lambda body: (sent.append(body), reply(URLS[1]))[1])
    client, headers = signed_client(monkeypatch, caller)
    path = f"/ventures/{d['vid']}/device-work-orders/{order['id']}/browser-ai-call"
    result = client.post(path, json=value.model_dump(), headers=headers(path))
    assert result.status_code == 200, result.text
    candidate = result.json()['effect_proposals'][0]
    assert candidate['candidate']['target'].startswith('document_') and result.json()['task_verified'] is False
    child = candidate['invocation_id']
    assert orders.retrieve(*args)['invocations'][child]['read_url'] == URLS[1]
    assert effects.admit(*args, invocation_id=child, observation_digest=value.observation.digest(), effect_digest='e' * 64)
    assert not effects.admit(*args, invocation_id=child, observation_digest=value.observation.digest(), effect_digest='e' * 64)
    effects.receipt(*args, invocation_id=child, effect_digest='e' * 64, outcome='returned')
    schema = sent[0]['text']['format']['schema']['properties']['actions']['items']['anyOf']
    navigate = next(shape for shape in schema if shape['properties']['op']['enum'] == ['navigate'])
    assert navigate['properties']['text']['enum'] == URLS and len(sent) == 1


@pytest.mark.parametrize('forged', [[], list(reversed(URLS)), [URLS[0]], [*URLS, 'https://github.com/unreviewed/repo']])
def test_context_must_match_exact_saved_review_urls_before_provider(journey_db, monkeypatch, forged):
    from app.build import device_browser_ai as ai
    d = journey_db
    orders, _, order, args = url_order(d, monkeypatch)
    value = request(order)
    value.context.read_urls = forged
    monkeypatch.setattr(ai, '_provider_call', lambda _: pytest.fail('Changed review scope reached provider'))
    with pytest.raises(ValueError, match=ai.INVALID):
        ai.run(*args, request=value)
    assert not orders.retrieve(*args).get('invocations')


@pytest.mark.parametrize('url', ['https://github.com/unreviewed/repo', 'https://github.com/psf/requests?view=1',
                                 'https://github.com/psf/requests#readme', 'https://github.com/psf/requests/'])
def test_actor_cannot_invent_or_normalize_an_unreviewed_direct_url(journey_db, monkeypatch, url):
    from app.build import device_browser_ai as ai
    d = journey_db
    orders, _, order, args = url_order(d, monkeypatch)
    value = request(order)
    monkeypatch.setattr(ai, '_provider_call', lambda _: reply(url))
    with pytest.raises(ValueError, match='device_browser_ai_target_invalid'):
        ai.run(*args, request=value)
    assert list(orders.retrieve(*args)['invocations']) == [value.invocation_id]


@pytest.mark.parametrize('changed', ['read_url', 'document', 'reviewed_list'])
def test_saved_direct_effect_rechecks_literal_url_and_current_document(journey_db, monkeypatch, changed):
    from app.build import device_browser_ai as ai, device_effects as effects, device_invocations as calls
    d = journey_db
    orders, _, order, args = url_order(d, monkeypatch)
    value = request(order)
    monkeypatch.setattr(ai, '_provider_call', lambda _: reply(URLS[1]))
    child = ai.run(*args, request=value)['effect_proposals'][0]['invocation_id']
    with d['factory']() as session:
        row, body, _ = calls._locked(session, *args)
        if changed == 'read_url':
            body['invocations'][child]['read_url'] = 'https://github.com/foreign/repo'
        elif changed == 'document':
            body['browser_ai_source']['document_id'] = 'e' * 32
        else:
            body['manifest']['general_browser_verification']['read_urls'] = [URLS[0]]
            body['review_digest'] = orders._digest(body)
        row.provenance_json = json.dumps(body)
        session.commit()
    with pytest.raises(ValueError, match='device_effect_proposal_unavailable'):
        effects.admit(*args, invocation_id=child, observation_digest=value.observation.digest(), effect_digest='e' * 64)


def test_l01_producer_pins_six_literal_get_urls_without_changing_task_or_budget_binding(journey_db, monkeypatch):
    from core.browser_qualification import QualificationTrialBinding
    from core.browser_qualification_catalog import PUBLIC_RESEARCH_READ_URLS, catalog_task
    d = journey_db
    _, _, order, _ = read_order(d, monkeypatch, trial_bound=True)
    general = order['manifest']['general_browser_verification']
    assert tuple(general['read_urls']) == PUBLIC_RESEARCH_READ_URLS and len(PUBLIC_RESEARCH_READ_URLS) == 6
    assert general['start_url'] == 'https://pypi.org/'
    assert order['manifest']['instruction'] == catalog_task('l01_research_synthesis').goal + ' (Start at https://pypi.org/.)'
    assert order['manifest']['actions'] == ['browser.read'] and general['allow_type'] is False and general['fill_values'] == {}
    assert 'browser_qualification_grant' not in order
    QualificationTrialBinding.from_dict(order['browser_qualification_trial_binding'])


def test_empty_default_omits_new_field_from_existing_manifest_wire(journey_db, monkeypatch):
    d = journey_db
    _, _, order, _ = url_order(d, monkeypatch, [])
    assert 'read_urls' not in order['manifest']['general_browser_verification']


@pytest.mark.parametrize('urls', [[URLS[0], URLS[0]], ['http://pypi.org/project/requests/'],
    ['https://user:password@pypi.org/project/requests/'], [URLS[0] + '?q=1'], [URLS[0] + '#section'],
    ['https://unreviewed.example/'], [URLS[0] + '?'], [URLS[0] + '#'], [URLS[0] + '\\path'],
    ['https://pypi.org/project/' + str(index) + '/' for index in range(7)]])
def test_reviewed_urls_reject_duplicates_unsafe_urls_and_unreviewed_origin(journey_db, monkeypatch, urls):
    with pytest.raises((ValueError, ValidationError)):
        url_order(journey_db, monkeypatch, urls)
