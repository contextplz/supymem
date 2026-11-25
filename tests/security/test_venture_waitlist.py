"""Public conversions are scoped, consented and separate from QA traffic."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import event, func, select
from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import request, venture


def setup_waitlist(client):
    vid, wid, _ = venture(client)
    app_path = f"/ventures/{vid}/apps"
    created = request(client, "POST", app_path, json={"name": "Bakery", "slug": f"bakery{vid[-6:]}"})
    assert created.status_code == 201, created.text
    path = f"/ventures/{vid}/waitlist"
    assert request(client, "PUT", path, json={"enabled": True}).status_code == 200
    from app.conversion import waitlist
    detail = waitlist.public_details(vid)
    payload = {"email": "founder@sample-business.example", "consent": "yes", "consent_version": waitlist.VERSION,
               "consent_digest": hashlib.sha256(detail["consent"].encode()).hexdigest()}
    return vid, wid, path, payload


def signup_event_count(vid):
    from app.db.supymem_models import AppEvent, FounderApp
    from app.state import db
    with db()() as session:
        app_id = session.scalar(select(FounderApp.id).where(FounderApp.venture_id == vid))
        return int(session.scalar(select(func.count()).select_from(AppEvent).where(
            AppEvent.app_id == app_id, AppEvent.name == "signup")) or 0)


def test_consent_persistence_dedupe_and_tenant_boundary(client):
    vid, wid, path, payload = setup_waitlist(client)
    public = f"/public/waitlists/{vid}"
    html = client.get(public)
    assert html.status_code == 200
    assert 'name="consent"' in html.text and 'form-action' in html.headers['content-security-policy']
    assert client.post(public, json={**payload, "consent": False}).status_code == 422
    assert client.post(public, json={**payload, "consent": []}).status_code == 422
    assert client.post(public, json={**payload, "email": "invalid"}).status_code == 422
    assert client.post(public, json={**payload, "test_token": "forged"}).status_code == 422
    assert client.post(public, json={**payload, "email": "test@example.com"}).status_code == 422
    a = client.post(public, json=payload)
    b = client.post(public, json={**payload, "email": payload['email'].upper()})
    assert a.status_code == b.status_code == 201
    assert a.json() == b.json() == {"received": True, "email_verified": False}
    assert signup_event_count(vid) == 1
    assert payload['email'] not in a.text
    data = request(client, "GET", path).json()
    assert data['total'] == 1 and len(data['leads']) == 1
    assert data['leads'][0]['consent_text']
    assert data['submission_verified'] is False
    inbox=request(client,'GET',f'/ventures/{vid}/leads').json()
    assert inbox['venture_id']==vid and inbox['total']==1 and inbox['source']=='managed_venture_waitlist'
    assert request(client,'GET',f'/ventures/{vid}/leads',subject='usr_bob').status_code==404
    for method in ('GET','PUT','POST'):
        target = path + '/test' if method == 'POST' else path
        args = {'json': {'enabled': False}} if method == 'PUT' else {}
        assert request(client, method, target, subject='usr_bob', **args).status_code == 404
    assert client.get(path).status_code in {401,403}
    request(client, 'PUT', path, json={'enabled': False})
    assert client.get(public).status_code == client.post(public, json=payload).status_code == 404


def test_public_native_form_and_limits(client):
    vid, _, _, payload = setup_waitlist(client)
    path = f'/public/waitlists/{vid}'
    r = client.post(path, data=payload, headers={'Accept':'text/html'})
    assert r.status_code == 201 and 'Request recorded' in r.text and payload['email'] not in r.text
    assert client.post(path, content='x'*4097, headers={'content-type':'application/json'}).status_code == 413
    assert client.post(path, content='email=a&email=b', headers={'content-type':'application/x-www-form-urlencoded'}).status_code == 422
    assert client.post(path, json={**payload,'workspace_id':'other'}).status_code == 422
    assert client.post(path, json={**payload,'consent_digest':'old-name'}).status_code == 422
    assert client.get('/public/waitlists/missing').status_code == 404
    for accept in ('text/html;q=0','application/json;q=1,text/html;q=0.5','text/html;q=nan'):
        assert client.post(path,json=payload,headers={'Accept':accept}).headers['content-type'].startswith('application/json')


def test_public_probe_readback_excluded_from_traction_and_revocable(client, monkeypatch):
    vid, wid, path, payload = setup_waitlist(client)
    assert request(client, 'GET', path).json()['probe_retry_after_seconds'] == 0
    from app.conversion import waitlist
    class PublicRoute:
        def __init__(self, **kwargs):
            assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            assert url == waitlist.public_url(vid)
            return client.post(f'/public/waitlists/{vid}', **kwargs)
    monkeypatch.setattr(waitlist.httpx, 'Client', PublicRoute)
    result = request(client,'POST',path+'/test')
    assert result.status_code == 200, result.text
    data=result.json()
    assert data['submission_verified'] and data['total']==0
    assert data['published_site'] is None  # A probe alone cannot create a shareable page.
    from app.db.supymem_models import Deployment, FounderApp
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id
    with db()() as session:
        app_id = session.scalar(select(FounderApp.id).where(FounderApp.venture_id == vid))
        deployment_id = new_id('dep')
        session.add(Deployment(id=deployment_id, app_id=app_id, workspace_id=wid, venture_id=vid,
            target='supymem_static', state='live', version=1,
            url=f'https://sites.example/{vid}', live_at=utcnow()))
        session.commit()
    published = request(client, 'GET', path).json()['published_site']
    assert published['id'] == deployment_id and published['url'] == f'https://sites.example/{vid}'
    assert 0 < data['probe_retry_after_seconds'] <= 60
    assert request(client,'GET',path).json()['leads']==[]
    assert signup_event_count(vid) == 0
    assert client.post(f'/public/waitlists/{vid}', json=payload).status_code == 201
    assert signup_event_count(vid) == 1
    recent = request(client,'POST',path+'/test')
    assert recent.status_code==429 and recent.json()['detail']['code']=='waitlist_probe_recent'
    after_recent = request(client,'GET',path).json()
    assert after_recent['submission_verified'] and after_recent['total']==1
    assert 0 < after_recent['probe_retry_after_seconds'] <= 60
    request(client,'PUT',path,json={'enabled':False})
    request(client,'PUT',path,json={'enabled':True})
    assert request(client,'GET',path).json()['submission_verified'] is False


def test_signup_event_failure_rolls_back_waitlist_entry(client):
    vid, _, path, payload = setup_waitlist(client)
    from app.db.supymem_models import AppEvent

    def fail_event(_mapper, _connection, _target):
        raise RuntimeError("simulated analytics storage failure")

    event.listen(AppEvent, "before_insert", fail_event)
    try:
        response = client.post(f"/public/waitlists/{vid}", json=payload)
    finally:
        event.remove(AppEvent, "before_insert", fail_event)
    assert response.status_code == 503
    assert response.json() == {"code": "waitlist_submission_unconfirmed"}
    assert request(client, "GET", path).json()["total"] == 0
    assert signup_event_count(vid) == 0


def test_waitlist_beacon_and_signup_choose_same_app_when_venture_has_two(client):
    vid, _, _, payload = setup_waitlist(client)
    second_path = f"/ventures/{vid}/apps"
    second = request(client, "POST", second_path, json={"name": "Second", "slug": f"second{vid[-6:]}"})
    assert second.status_code == 201, second.text

    from app.product_analytics.beacon import _app_for_venture
    from app.db.supymem_models import AppEvent
    from app.state import db

    selected_app_id, selected_key = _app_for_venture(vid)
    public = f"/public/waitlists/{vid}"
    assert selected_key in client.get(public).text
    assert client.post(public, json=payload).status_code == 201
    with db()() as session:
        events = list(session.scalars(select(AppEvent).where(AppEvent.name == "signup")))
    assert len(events) == 1 and events[0].app_id == selected_app_id
    assert events[0].app_id != second.json()["id"]


def test_first_signup_notice_once_and_notice_failure_does_not_lose_request(client, monkeypatch):
    vid, _, path, payload = setup_waitlist(client)
    from app import desktop_notices

    notices = []
    monkeypatch.setattr(desktop_notices, "enqueue", lambda **kwargs: notices.append(kwargs))
    public = f"/public/waitlists/{vid}"
    assert client.post(public, json=payload).status_code == 201
    assert client.post(public, json=payload).status_code == 201
    assert len(notices) == 1
    assert notices[0]["venture_id"] == vid

    def notice_down(**_kwargs):
        raise RuntimeError("notification unavailable")

    monkeypatch.setattr(desktop_notices, "enqueue", notice_down)
    other_vid, _, other_path, other_payload = setup_waitlist(client)
    assert client.post(f"/public/waitlists/{other_vid}", json=other_payload).status_code == 201
    assert request(client, "GET", other_path).json()["total"] == 1
    assert signup_event_count(other_vid) == 1


def test_concurrent_public_duplicates_and_changed_consent(client):
    vid,wid,path,payload=setup_waitlist(client)
    public=f'/public/waitlists/{vid}'
    with ThreadPoolExecutor(max_workers=5) as pool:
        results=list(pool.map(lambda _:client.post(public,json=payload),range(5)))
    assert all(r.status_code==201 for r in results)
    assert request(client,'GET',path).json()['total']==1
    from app.db.models import Venture
    from app.state import db
    with db()() as session:
        session.get(Venture,vid).name='A revised venture name'
        session.commit()
    assert client.post(public,json=payload).status_code==422


def test_false_success_from_transport_cannot_establish_storage_receipt(client, monkeypatch):
    vid, wid, path, _ = setup_waitlist(client)
    from app.conversion import waitlist
    class FalseSuccess:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self,*args,**kwargs):
            import httpx
            return httpx.Response(201,json={'received':True})
    monkeypatch.setattr(waitlist.httpx,'Client',FalseSuccess)
    assert request(client,'POST',path+'/test').status_code==503
    assert request(client,'GET',path).json()['submission_verified'] is False
