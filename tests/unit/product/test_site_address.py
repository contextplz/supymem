"""Founders choose the prefix of <prefix>.supymem.com; it is checked live, re-validated, and never breaks a live URL."""
import json

import pytest
from core.clock import utcnow
from core.hosting_contracts import host_mapping
from tests.unit.chat.test_round3_matrix import venture as venture_fixture
from tests.unit.product.test_static_publication import _config

venture = venture_fixture


@pytest.fixture(autouse=True)
def hosted_address_tenants(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)


@pytest.fixture()
def addr(venture, monkeypatch):
    from app.hosting.service import HostingConfig
    monkeypatch.setattr(HostingConfig, "from_environment", classmethod(lambda _: _config()))
    client, vid, ws = venture
    return client, vid, ws


def _check(client, vid, slug):
    return client.get(f"/ventures/{vid}/site-address/check", params={"slug": slug})


def _put(client, vid, slug, **kw):
    return client.put(f"/ventures/{vid}/site-address", json={"slug": slug}, **kw)


def _other_app(client, slug):
    """Another founder's venture already holds the address."""
    other = client.post("/ventures", json={"raw_idea": "A different idea about neighborhood bakeries"})
    assert other.status_code == 201, other.text
    assert _put(client, other.json()["id"], slug).status_code == 200


def test_check_available_taken_reserved_invalid_and_length(addr):
    client, vid, _ = addr
    ok = _check(client, vid, "  My Bakery ").json()
    assert ok == {"available": True, "normalized": "my-bakery", "reason": None, "current": None, "message": None,
                  "suggestion": None}
    _other_app(client, "bakery")
    taken = _check(client, vid, "bakery").json()
    assert (taken["available"], taken["reason"], taken["suggestion"]) == (False, "taken", "bakery-2")
    assert _check(client, vid, "admin").json()["reason"] == "reserved"
    assert _check(client, vid, "supymem-x").json()["reason"] == "reserved"
    assert _check(client, vid, "ab").json()["reason"] == "too_short"
    assert _check(client, vid, "a" * 49).json()["reason"] == "too_long"
    for bad in ("-abc", "abc-", "a.b.c", "café", "a--b", "12345"):
        body = _check(client, vid, bad).json()
        assert body["reason"] == "invalid" and body["suggestion"], bad


def test_check_requires_venture_access_and_is_rate_limited(addr):
    from app.api import firstlook_api
    client, vid, _ = addr
    assert client.get("/ventures/ven_missing/site-address/check", params={"slug": "abc"}).status_code == 404
    firstlook_api._ADDRESS._buckets.clear()
    codes = {_check(client, vid, "abc").status_code for _ in range(70)}
    assert codes == {200, 429}
    firstlook_api._ADDRESS._buckets.clear()


def test_put_sets_address_validates_again_and_blocks_strangers(addr):
    from app.db.supymem_models import FounderApp
    from app.state import db
    client, vid, _ = addr
    done = _put(client, vid, "My-Bakery")
    assert done.status_code == 200, done.text
    assert done.json()["hostname"] == "my-bakery.supymem.com" and done.json()["changed"] is True
    assert _put(client, vid, "my-bakery").json()["changed"] is False
    assert _check(client, vid, "my-bakery").json()["available"] is True  # its own address stays available to it
    with db()() as session:
        assert session.query(FounderApp).filter_by(venture_id=vid).one().slug == "my-bakery"
    reserved = _put(client, vid, "admin")
    assert reserved.status_code == 422 and reserved.json()["detail"]["reason"] == "reserved"
    assert reserved.json()["detail"]["suggestion"]
    assert _put(client, vid, "x").status_code == 422
    stranger = _put(client, vid, "intruder", headers={"X-Subject": "usr_stranger"})
    assert stranger.status_code in (403, 404)
    assert _check(client, vid, "intruder").json()["available"] is True


def test_put_taken_and_race_both_409_with_a_free_suggestion(addr, monkeypatch):
    from app.hosting import site_address
    client, vid, _ = addr
    _other_app(client, "bakery")
    taken = _put(client, vid, "bakery")
    assert taken.status_code == 409 and taken.json()["detail"]["suggestion"] == "bakery-2"
    # The check passed, then someone else won: only the unique constraint decides.
    monkeypatch.setattr(site_address, "_taken", lambda session, slug, app_id: False)
    race = _put(client, vid, "bakery")
    assert race.status_code == 409
    assert race.json()["detail"]["code"] == "site_address_taken" and race.json()["detail"]["suggestion"]


@pytest.fixture()
def live(addr):
    """A page live at old-name.supymem.com."""
    from app.db.supymem_models import AppDomain, Deployment, FounderApp
    from app.state import db
    client, vid, ws = addr
    assert _put(client, vid, "old-name").status_code == 200
    now = utcnow()
    with db()() as session:
        app_id = session.query(FounderApp).filter_by(venture_id=vid).one().id
        session.add(Deployment(id="dep_12345678", app_id=app_id, workspace_id=ws, venture_id=vid,
                               build_artifact_id="firstlook:sv_1", target="supymem_static", state="live", version=1,
                               url="https://old-name.supymem.com/", live_at=now))
        session.add(AppDomain(id="dom_12345678", app_id=app_id, workspace_id=ws, hostname="old-name.supymem.com",
                              kind="supymem", state="active", created_at=now, active_at=now))
        session.commit()
    return client, vid, ws, app_id


def test_changing_a_live_address_keeps_the_old_host_reserved_and_audited(live):
    from app.db.models import EventLog
    from app.db.supymem_models import AppDomain
    from app.hosting import site_address
    from app.state import db
    client, vid, _, app_id = live
    done = _put(client, vid, "new-name")
    assert done.status_code == 200 and done.json()["live"] is True
    assert done.json()["previous_hostname"] == "old-name.supymem.com"
    assert site_address.aliases(app_id) == ["old-name.supymem.com"]  # kept serving, never redirected
    with db()() as session:
        assert session.query(AppDomain).filter_by(hostname="old-name.supymem.com").one().state == "active"
        assert site_address._taken(session, "old-name", "app_someone_else") is True
        events = session.query(EventLog).filter_by(topic="hosting.site_address_changed").all()
        assert events and json.loads(events[-1].payload_json)["to"] == "new-name.supymem.com"
    assert _check(client, vid, "old-name").json()["available"] is True  # still this venture's own
    back = _put(client, vid, "old-name")  # returning promotes the retired row instead of colliding with it
    assert back.status_code == 200
    assert site_address.aliases(app_id) == ["new-name.supymem.com"] or site_address.aliases(app_id) == []


def test_change_is_refused_while_a_publication_is_running(live):
    from app.db.supymem_models import Deployment
    from app.state import db
    client, vid, ws, app_id = live
    with db()() as session:
        session.add(Deployment(id="dep_87654321", app_id=app_id, workspace_id=ws, venture_id=vid,
                               build_artifact_id="firstlook:sv_2", target="supymem_static", state="pushing", version=2))
        session.commit()
    busy = _put(client, vid, "new-name")
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "hosting_publication_in_progress"


@pytest.mark.parametrize("republished", [False, True])
def test_unpublish_takes_retired_hosts_offline_with_the_page(live, monkeypatch, republished):
    from app.db.models import Job
    from app.db.supymem_models import Deployment, FounderApp
    from app.hosting import unpublish
    from app.state import db
    client, vid, ws, app_id = live
    assert _put(client, vid, "new-name").status_code == 200
    mapping = host_mapping(app_id=app_id, deployment_id="dep_12345678", generation=1, enabled=True)
    expected = {"schema_version": 1, "app_id": app_id, "deployment_id": "dep_12345678", "version": 1,
                "hostname": "new-name.supymem.com", "mapping": mapping, "firstlook_source_ids": []}
    monkeypatch.setattr(unpublish, "_baseline", lambda *a: expected)  # firstlook source custody is covered elsewhere
    with db()() as session:  # between the change and the republish, the page is still live on the old host
        assert unpublish._serving_host(session, session.get(FounderApp, app_id),
                                       session.get(Deployment, "dep_12345678"), _config()) == "old-name.supymem.com"
    if republished:  # the new host went live; the old one is an alias of the same deployment
        with db()() as session:
            session.get(Deployment, "dep_12345678").url = "https://new-name.supymem.com/"
            session.commit()
        expected["hostname"] = "new-name.supymem.com"
    else:
        expected["hostname"] = "old-name.supymem.com"
    first = client.post(f"/apps/{app_id}/deployments/dep_12345678/unpublish")
    assert first.status_code == 202, first.text
    with db()() as session:
        payload = json.loads(session.get(Job, first.json()["unpublish"]["job_id"]).payload_json)

    class Provider:
        config = _config()
        disabled = []

        def disable(self, host, mapping):
            self.disabled.append(host)

        def verify_offline(self, *_):
            pass

        def verify_disabled_mapping(self, *_):
            pass

    provider = Provider()
    assert unpublish.execute(payload, provider=provider)["state"] == "unpublished"
    # the live host first, then the other one; never the same host twice
    assert provider.disabled == (["new-name.supymem.com", "old-name.supymem.com"] if republished else ["old-name.supymem.com"])
