"""Pure route spies prove redirects cannot leave the native reviewed-origin gate."""
from types import SimpleNamespace

import pytest

from coding_agents.owned_browser import MAX_PASSIVE_BODY_BYTES, OwnedBrowser


def source_route(*, resource='document', method='GET', status=200, headers=None, body=b'Scoped source', retired=False, fetch_error=False):
    browser = OwnedBrowser.__new__(OwnedBrowser)
    browser.origins = frozenset({'https://owned.example'})
    browser._allow_loopback = False
    browser.read_only = True
    browser._uncertain = False
    browser._page = SimpleNamespace(url='https://owned.example/start')
    browser._route_authority_until = float('inf')
    browser.qualification_grant = None
    browser._reviewed_read_navigation = None
    browser.last_read_refusal = 'old_refusal'
    events = []
    def check():
        events.append(('check', None))
        if retired:
            raise PermissionError('source_retired')
    browser._check = check
    request = SimpleNamespace(method=method, resource_type=resource, url='https://owned.example/resource',
        frame=SimpleNamespace(page=browser._page))
    def read_body():
        events.append(('body', None))
        return body
    response = SimpleNamespace(status=status, headers=headers if headers is not None else {'content-type': 'text/plain'},
        body=read_body, dispose=lambda: events.append(('dispose', None)))
    def fetch(**kwargs):
        events.append(('fetch', kwargs))
        assert kwargs == {'max_redirects': 0, 'max_retries': 0, 'timeout': 10000}
        if fetch_error:
            raise RuntimeError('synthetic_transport_failure')
        return response
    route = SimpleNamespace(request=request, fetch=fetch, fulfill=lambda **kwargs: events.append(('fulfill', kwargs)),
        abort=lambda reason: events.append(('abort', reason)),
        continue_=lambda **_: pytest.fail('Unsafe continue fallback'))
    return browser, route, events


@pytest.mark.parametrize('resource', ['document', 'script', 'stylesheet', 'image', 'font', 'media'])
@pytest.mark.parametrize('status', [301, 302, 303, 304, 307, 308])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_all_ordinary_redirect_classes_are_blocked_without_follow_or_fulfill(resource, status, method):
    browser, route, events = source_route(resource=resource, method=method, status=status,
        headers={'location': 'https://foreign.example/private'})
    browser._route(route)
    assert [kind for kind, _ in events] == ['fetch', 'abort', 'dispose']
    assert browser._uncertain is (resource == 'document')
    assert browser.last_read_refusal == 'browser_read_redirect_unavailable'


@pytest.mark.parametrize('location', ['/same-origin', '//foreign.example/path', 'https://user:password@foreign.example/',
    'http://127.0.0.1/private', '', 'https://owned.example/redirect-loop'])
def test_location_values_never_gain_permission_for_a_second_request(location):
    browser, route, events = source_route(status=302, headers={'location': location})
    browser._route(route)
    assert sum(kind == 'fetch' for kind, _ in events) == 1 and not any(kind == 'fulfill' for kind, _ in events)


@pytest.mark.parametrize('method,status', [('GET', 200), ('HEAD', 200), ('GET', 204), ('GET', 205)])
def test_nonredirect_response_rechecks_authority_preserves_status_and_avoids_head_body(method, status):
    browser, route, events = source_route(method=method, status=status)
    browser._route(route)
    value = next(value for kind, value in events if kind == 'fulfill')
    assert value['status'] == status and value['body'] == (b'Scoped source' if method == 'GET' and status == 200 else b'')
    assert ('check', None) in events and events[-1][0] == 'dispose'
    assert any(kind == 'body' for kind, _ in events) is (method == 'GET' and status == 200)
    assert browser.last_read_refusal is None and not browser._uncertain


@pytest.mark.parametrize('case', ['retired', 'fetch_error', 'large_header', 'large_body', 'bad_length', 'bad_mime', 'bad_status'])
def test_unknown_transport_or_retired_authority_has_no_continue_or_fulfill(case):
    options = {'retired': {'retired': True}, 'fetch_error': {'fetch_error': True},
        'large_header': {'headers': {'content-length': str(MAX_PASSIVE_BODY_BYTES + 1), 'content-type': 'text/plain'}},
        'large_body': {'body': b'x' * (MAX_PASSIVE_BODY_BYTES + 1)}, 'bad_length': {'headers': {'content-length': 'unknown'}},
        'bad_mime': {'headers': {'content-type': 'unknown'}}, 'bad_status': {'status': True}}[case]
    browser, route, events = source_route(**options)
    browser._route(route)
    assert any(kind == 'abort' for kind, _ in events) and not any(kind == 'fulfill' for kind, _ in events)
    assert browser._uncertain
    assert any(kind == 'dispose' for kind, _ in events) is (case != 'fetch_error')


def test_dispose_failure_cannot_restore_authority_or_continue_transport():
    browser, route, events = source_route(status=302)
    response = route.fetch(max_redirects=0, max_retries=0, timeout=10000)
    response.dispose = lambda: (_ for _ in ()).throw(RuntimeError('synthetic_cleanup_failure'))
    route.fetch = lambda **_: response
    browser._route(route)
    assert browser._uncertain and browser.last_read_refusal == 'browser_read_transport_unconfirmed'
    assert not any(kind == 'fulfill' for kind, _ in events)
