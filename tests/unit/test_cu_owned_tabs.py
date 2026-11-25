"""General owned tab custody and versioned proposal contract; no GUI/providers."""
from types import SimpleNamespace

import pytest

from coding_agents import cu_ai_loop as loop
from desktop_runtime.cu_ai_browser import BrowserMac
from desktop_runtime.cu_browser.backend import BrowserBackend, SafetyRefusal


def test_tab_contract_preserves_legacy_schemas_and_adds_version_three():
    for version in (1, 2):
        schema = loop.response_format('step', action_contract_version=version)['schema']
        assert 'new_tab' not in schema['properties']['actions']['items']['properties']['op']['enum']
    schema = loop.response_format('step', action_contract_version=3)['schema']
    assert {'new_tab', 'switch_tab'}.issubset(schema['properties']['actions']['items']['properties']['op']['enum'])
    assert loop.parse_action({'op': 'switch_tab', 'element_id': 't' + 'a' * 32}).op == 'switch_tab'
    with pytest.raises(loop.ActionError):
        loop.parse_action({'op': 'switch_tab', 'element_id': 't0'})


def test_new_tab_uses_same_url_guard_before_creation():
    b = object.__new__(BrowserBackend)
    for url in ('file:///tmp/private', 'javascript:alert(1)', 'data:text/html,x'):
        with pytest.raises(SafetyRefusal):
            b.check_safety({'action': 'new_tab', 'url': url})


def test_browser_routes_fresh_tab_token_without_integer_conversion():
    calls = []
    b = SimpleNamespace(snapshot_id='fresh', act=lambda payload: calls.append(payload) or {'status': 'ok', 'seconds': 0})
    mac = BrowserMac(b)
    obs = SimpleNamespace(browser_snapshot_id='fresh', elements=[])
    mac.last = obs
    action = loop.Action('switch_tab', element_id='t' + 'a' * 32)
    mac.act(action, obs)
    assert calls == [{'action': 'switch_tab', 'tab_id': action.element_id}]
    assert mac.last is None
    with pytest.raises(RuntimeError, match='fresh_browser_observation_required'):
        mac.act(action, obs)


def tab_backend():
    class Page:
        url = 'https://synthetic.example/'
        closed = False
        def is_closed(self):
            return self.closed
        def evaluate(self, *args):
            return True
        def title(self):
            return 'Synthetic'
    b = object.__new__(BrowserBackend)
    b._owned_tabs = True
    b.page = Page()
    b._indexed_page, b._indexed_url = b.page, b.page.url
    b._indexed_generation = 0
    b._document_handle = object()
    b._document_generations = {}
    b._tab_bindings, b._tab_indices = {}, {}
    b.context = SimpleNamespace(pages=[b.page, Page()])
    return b


def test_observed_legacy_index_binds_exact_page_not_shifted_array():
    b = tab_backend()
    rows = b._observe_tabs(b.page)
    target = b.context.pages[1]
    b.context.pages.reverse()  # index1 now points to another page; binding does not
    assert b._tab_target({'tab': 1}) is target
    assert b._tab_target({'tab_id': rows[1]['id']}) is target
    with pytest.raises(RuntimeError, match='stale_browser_tab'):
        b._tab_target({'tab': 1, 'tab_id': rows[1]['id']})


@pytest.mark.parametrize('change', ['closed', 'replaced', 'navigated', 'generation', 'scheme'])
def test_closed_replaced_or_changed_target_never_falls_back(change):
    b = tab_backend()
    target = b.context.pages[1]
    token = b._observe_tabs(b.page)[1]['id']
    if change == 'closed':
        target.closed = True
    elif change == 'replaced':
        b.context.pages[1] = tab_backend().page
    elif change == 'navigated':
        target.url = 'https://synthetic.example/other'
    elif change == 'generation':
        b._document_generations[id(target)] = 1
    else:
        target.url = 'file:///tmp/private'
        b._tab_bindings[token] = (target, 0, target.url)
    with pytest.raises((RuntimeError, SafetyRefusal)):
        b._tab_target({'tab_id': token})
    assert b.page is b._indexed_page


def test_borrowed_tab_inventory_has_no_capability_and_refuses():
    b = tab_backend()
    b._owned_tabs = False
    assert all(row['id'] is None for row in b._observe_tabs(b.page))
    with pytest.raises(SafetyRefusal, match='owned_browser_tabs_required'):
        b._tab_target({'tab': 0})


def test_old_model_contracts_remain_byte_identical_and_v3_does_not_reveal_urls():
    import json
    import hashlib
    # Digests of actual pre-tab v1/v2 schema/prompt at 9693a74e; no Git history
    # dependency in CI/shallow clones and no fabricated legacy implementation.
    frozen = {1: ('da2dfc57b78233f03de7c92f2f0f7255bb231cbc134bb9990e51488cdd7a362a', 'e7266f9492a3c901f13781f6b9da87911e6ceae54e1af23e1f9c134b2f4c1c4c'), 2: ('c00cdc0a4097335ece16a05f278b6ce4819ba4ed6cc0d516e798b2a22f956fda', '02fa126bfae8fe8ec4072e44a06fc5b155f845513bfc8fe64c658261c6ca6e60')}
    for version in (1, 2):
        assert hashlib.sha256(json.dumps(loop.response_format('step', action_contract_version=version), sort_keys=True).encode()).hexdigest() == frozen[version][0]
        assert hashlib.sha256(loop.system_prompt(version).encode()).hexdigest() == frozen[version][1]
    assert 'new_tab' in loop.system_prompt(3)
    assert 'private.example' not in loop.Action('new_tab', text='https://private.example/token').brief()


def test_owned_tab_progress_uses_stable_page_scope_not_new_observation_ids():
    from desktop_runtime.cu_ai_retries import semantic_state
    obs = SimpleNamespace(elements=[], frontmost='Chrome', pid=None, notes='PAGE URL: https://synthetic.example/',
                          browser_tab_state=(1, 'a' * 32))
    baseline = semantic_state(obs)
    obs.snapshot_id = 'new-observation-token'
    assert semantic_state(obs) == baseline
    obs.browser_tab_state = (2, 'a' * 32)
    assert semantic_state(obs) != baseline
    two = semantic_state(obs)
    obs.browser_tab_state = (2, 'b' * 32)
    assert semantic_state(obs) != two  # same URL but exact other recipient is a real tab switch


def test_observation_progress_digest_changes_only_for_real_owned_tab_state():
    from desktop_runtime.cu_ai_browser import BrowserObservation
    obs = BrowserObservation(b'', [], 'Chrome', 'Same page', '', False, None)
    obs.browser_tab_state = (2, 'a' * 32)
    before = obs.digest()
    obs.browser_snapshot_id = 'refreshed-token'
    assert obs.digest() == before
    obs.browser_tab_state = (2, 'b' * 32)
    assert obs.digest() != before


@pytest.mark.parametrize('version', [1, 2])
def test_negotiated_reply_checks_even_discarded_extra_batch_operations(version):
    with pytest.raises(loop.ActionError, match='action_op_invalid'):
        loop.parse_reply({'actions': [{'op': 'wait'}] * 4 + [{'op': 'new_tab', 'text': 'https://synthetic.example/'}]},
                         action_contract_version=version)
