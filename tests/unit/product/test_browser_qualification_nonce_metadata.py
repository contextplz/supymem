"""Pure scope/parser proof: sealed fixture nonces are metadata; actual PAN surfaces remain blocked."""
from dataclasses import replace
import json

import pytest

from tests.unit.product.test_browser_qualification_actor_capabilities import capability_source, action_reply


@pytest.fixture(autouse=True)
def company_os():
    from tests.company_os_harness import load_company_os_app
    load_company_os_app(reset=False)


def collision_source(monkeypatch):
    from app.build import device_orders as orders
    ai, grant, saved, request, profile = capability_source(monkeypatch, recovery=True)
    nonce = 'a' * 17 + '1234567890123' + 'b' * 18
    old, new = '/public/browser-qualification/' + grant.nonce, '/public/browser-qualification/' + nonce
    grant = replace(grant, nonce=nonce, rules=tuple(replace(rule,
        path=rule.path.replace(old, new), document_path=rule.document_path.replace(old, new)) for rule in grant.rules))
    request.observation.location = request.observation.location.replace(old, new)
    request.current_load.location = request.observation.location
    request.current_load.response.url = request.observation.location
    request.current_load.observation_digest = request.observation.digest()
    request.context.qualification.grant_digest = grant.grant_digest
    request.context.qualification.tabs = []
    general = {'operation': 'general_goal_v1', 'start_url': request.observation.location, 'max_steps': 32,
        'allow_type': False, 'fill_values': {}, 'execution_profile': profile.model_dump()}
    saved['browser_qualification_grant'] = grant.to_dict()
    saved['manifest'] = orders.Manifest(task_id='founder_browser_v_one', title='Synthetic source',
        instruction=request.observation.goal, applications=['com.google.Chrome'], origins=[grant.origin],
        actions=['browser.read'], credit_ceiling_micro=500000, duration_seconds=900,
        general_browser_verification=general).model_dump(exclude_none=True)
    current = {key: getattr(request.current_load, key) for key in (
        'session_id', 'page_id', 'document_id', 'sequence', 'location', 'observation_digest')}
    current['source'] = {'facts': ['503 Service unavailable'], 'fields': [], 'reads': [], 'reads_omitted': 0,
        'omitted': {'facts': 0, 'fields': 0, 'values': 0}}
    request.context.notes = json.dumps({'schema_version': 2, 'scope': 'source_main_document_inputs_not_acceptance',
        'current': current, 'prior_pages': [], 'input_history': [], 'omitted': {'pages': 0, 'inputs': 0}})
    return ai, grant, saved, request


def test_validated_error_scope_with_card_shaped_nonce_reaches_post_usage_reply_rejection(monkeypatch):
    from coding_agents import cu_ai_loop as loop
    ai, grant, saved, request = collision_source(monkeypatch)
    assert loop.CARD.search(request.observation.location)
    assert not loop.CARD.search(ai._card_location(request.observation.location, grant))
    ai.scoped(saved, request)  # No provider/admission/DB; this is the former pre-I/O false rejection.
    with pytest.raises(ValueError, match='device_browser_ai_reply_invalid'):
        ai._candidate(action_reply({'op': 'done', 'summary': 'Success'}), request.observation,
            saved['manifest']['general_browser_verification'], False, read_only=True, fixture=grant,
            qualification=request.context.qualification, current_load=request.current_load.model_dump())


def test_original_bound_opaque_source_metadata_remains_allowed(monkeypatch):
    ai, _, saved, request = collision_source(monkeypatch)
    identifier = 'a' * 9 + '1234567890123' + 'b' * 10
    request.current_load.session_id = identifier
    notes = json.loads(request.context.notes)
    notes['current']['session_id'] = identifier
    request.context.notes = json.dumps(notes)
    assert ai.loop.CARD.search(identifier)
    ai.scoped(saved, request)


@pytest.mark.parametrize('surface', ['context', 'memory', 'fact', 'field', 'foreign_url', 'unknown_url', 'secret'])
def test_actual_pan_or_secret_surfaces_are_not_nonce_metadata(monkeypatch, surface):
    ai, grant, saved, request = collision_source(monkeypatch)
    pan = '4111111111111111'  # Synthetic card-shaped test data, never a credential.
    notes = json.loads(request.context.notes)
    if surface == 'context':
        request.observation.context = 'Card number: ' + pan
        request.current_load.observation_digest = request.observation.digest()
        notes['current']['observation_digest'] = request.observation.digest()
    elif surface == 'memory':
        request.context.memory = pan
    elif surface == 'fact':
        notes['current']['source']['facts'] = [pan]
    elif surface == 'field':
        notes['current']['source']['fields'] = [{'label': pan, 'type': 'text', 'populated': False, 'reviewed_value_index': None}]
    elif surface in {'foreign_url', 'unknown_url'}:
        previous = json.loads(json.dumps(notes['current']))
        previous['location'] = (request.observation.location.replace(grant.nonce, 'e' * 17 + pan + 'f' * 15)
            if surface == 'foreign_url' else request.observation.location + '/unknown')
        notes['prior_pages'] = [previous]
    else:
        notes['current']['source']['facts'] = ['sk-' + 'a' * 30]
    request.context.notes = json.dumps(notes)
    with pytest.raises(ValueError, match=ai.INVALID):
        ai.scoped(saved, request)


def test_mask_is_exact_current_get_nonce_only_and_preserves_query_card_data(monkeypatch):
    from coding_agents import cu_ai_loop as loop
    ai, grant, _, request = collision_source(monkeypatch)
    location = request.observation.location
    assert ai._card_location(location, None) == location
    assert ai._card_location(location + '?unreviewed=1', grant) == location + '?unreviewed=1'
    assert ai._card_location(location.replace(grant.origin, 'https://foreign.example'), grant).startswith('https://foreign.example')
    first = grant.rules[0]
    query = 'id=4111111111111111'
    queried = replace(grant, rules=(replace(first, query=query), *grant.rules[1:]))
    assert loop.CARD.search(ai._card_location(location + '?' + query, queried))


@pytest.mark.parametrize('surface', ['fact', 'field'])
def test_json_escaped_secret_prefix_is_checked_after_source_notes_decode(monkeypatch, surface):
    ai, _, saved, request = collision_source(monkeypatch)
    notes = json.loads(request.context.notes)
    secret = 'sk-' + 'a' * 30  # Synthetic shape only; no secret is printed or sent.
    if surface == 'fact':
        notes['current']['source']['facts'] = [secret]
    else:
        notes['current']['source']['fields'] = [{'label': secret, 'type': 'text', 'populated': False,
                                                'reviewed_value_index': None}]
    request.context.notes = json.dumps(notes).replace('sk-', r'\u0073\u006b-')
    assert not ai.loop.SECRETISH.search(request.context.notes)
    decoded = ai.source_notes_card_surfaces(request.context.notes, request, saved, [])
    assert any(ai.loop.SECRETISH.search(text) for text in decoded)
    with pytest.raises(ValueError, match=ai.INVALID):
        ai.scoped(saved, request)
