"""Public conversion transport must not import founder authority or buffer large bodies."""
import importlib
import httpx
import pytest
from starlette.requests import Request
from tests.integration.test_gateway_native_auth import gateway as gateway


def test_public_form_post_preserves_bytes_headers_and_no_identity(gateway, monkeypatch):
    monkeypatch.setenv('RATE_LIMIT_PUBLIC_READ_60', '8')
    _, client = gateway
    proxy = importlib.import_module('app.proxy')
    original = httpx.AsyncClient
    calls=[]
    body=b'email=interested%40business.io&consent=yes&consent_version=early-access-v1'
    async def upstream(request):
        calls.append(request)
        assert request.url.path == '/public/waitlists/ven_test'
        assert request.headers['x-subject']==''
        assert 'authorization' not in request.headers and 'cookie' not in request.headers
        if request.method=='POST':
            assert request.content==body
            assert request.headers['accept']=='text/html'
        return httpx.Response(201 if request.method=='POST' else 200, content=b'<h1>Request recorded</h1>',
            headers={'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store',
                     'Content-Security-Policy':"default-src 'none'; form-action 'self'",'X-Frame-Options':'DENY'})
    monkeypatch.setattr(proxy.httpx,'AsyncClient',lambda **kwargs: original(**kwargs,transport=httpx.MockTransport(upstream)))
    result=client.post('/public/waitlists/ven_test',content=body,headers={'Content-Type':'application/x-www-form-urlencoded',
        'Accept':'text/html','X-Subject':'usr_attacker','Authorization':'Bearer forged'})
    assert result.status_code==201 and result.content==b'<h1>Request recorded</h1>'
    assert result.headers['cache-control']=='no-store' and 'form-action' in result.headers['content-security-policy']
    assert client.post('/public/waitlists/ven_test',content=b'x'*4097,headers={'Content-Type':'application/json'}).status_code==413
    assert client.post('/public/waitlists/ven_test',content=iter([b'x'*2048,b'x'*2049]),headers={'Content-Type':'application/json'}).status_code==413
    assert client.post('/public/waitlists/ven_test',content=b'{}',headers={'Content-Type':'application/json','Content-Encoding':'gzip'}).status_code==415
    assert len(calls)==1
    assert client.put('/public/waitlists/ven_test',json={}).status_code==405
    assert client.get('/public/waitlists/ven_test/leads').status_code==404
    from app.ratelimit import limiter

    limiter.reset()
    results=[client.get('/public/waitlists/ven_test').status_code for _ in range(9)]
    assert 429 in results and len(calls)==9


def test_public_events_post_forwards_text_plain_without_identity(gateway, monkeypatch):
    _, client = gateway
    proxy = importlib.import_module("app.proxy")
    original = httpx.AsyncClient
    calls = []
    body = b'{"key":"pk_app_fixture","event":"landing_view","props":{}}'

    async def upstream(request):
        calls.append(request)
        assert request.url.path == "/e"
        assert request.method == "POST"
        assert request.headers["x-subject"] == ""
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert request.content == body
        assert request.headers["content-type"].startswith("text/plain")
        return httpx.Response(204)

    monkeypatch.setattr(
        proxy.httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(upstream))
    )
    result = client.post(
        "/e",
        content=body,
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "X-Subject": "usr_attacker",
            "Authorization": "Bearer forged",
            "Cookie": "session=private",
        },
    )
    assert result.status_code == 204
    assert len(calls) == 1
    assert client.post("/e", content=b"x" * 8193, headers={"Content-Type": "text/plain"}).status_code == 413
    assert client.get("/e").status_code == 405


def test_rotating_spoofed_forwarded_prefix_cannot_bypass_public_limit(gateway, monkeypatch):
    main, client = gateway
    monkeypatch.setattr(main.settings, 'app_env', 'production')
    monkeypatch.setattr(main.settings, 'trusted_proxy_hops', 1)
    monkeypatch.setenv('RATE_LIMIT_PUBLIC_READ_60', '8')
    proxy = importlib.import_module('app.proxy')
    original = httpx.AsyncClient
    calls = []
    async def upstream(request):
        calls.append(request)
        return httpx.Response(200, text='A public form')
    monkeypatch.setattr(proxy.httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(upstream)))
    # ALB append mode preserves the untrusted prefix and appends the real peer.
    codes = [client.get('/public/waitlists/ven_test', headers={
        'X-Forwarded-For': f'198.51.100.{index}, 203.0.113.9',
    }).status_code for index in range(1, 11)]
    assert codes[:8] == [200] * 8 and codes[8:] == [429, 429]
    assert len(calls) == 8


@pytest.mark.parametrize('env,forwarded,expected', [
    ('local', '198.51.100.1', '127.0.0.1'),
    ('production', '198.51.100.1, 203.0.113.9', '203.0.113.9'),
    ('production', '198.51.100.1, not-an-ip', '127.0.0.1'),
    ('production', '198.51.100.1,', '127.0.0.1'),
    ('production', '', '127.0.0.1'),
    ('staging', '2001:0DB8:0000::1', '2001:db8::1'),
    ('production', '203.0.113.9:4321', '203.0.113.9'),
    ('production', '[2001:db8::1]:4321', '2001:db8::1'),
    ('production', '203.0.113.9:99999', '127.0.0.1'),
])
def test_rate_identity_uses_canonical_trusted_peer(gateway, monkeypatch, env, forwarded, expected):
    main, _ = gateway
    monkeypatch.setattr(main.settings, 'app_env', env)
    monkeypatch.setattr(main.settings, 'trusted_proxy_hops', 1 if env in {'staging', 'production'} else 0)
    request = Request({'type': 'http', 'client': ('127.0.0.1', 1234),
        'headers': [(b'x-forwarded-for', forwarded.encode())]})
    assert main._login_ip(request) == expected
