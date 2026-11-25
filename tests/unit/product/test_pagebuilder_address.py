"""Page-builder pages honour the chosen address and re-point retired addresses on every publish (fake hosting adapter)."""
import json
from types import SimpleNamespace

import pytest
from core.clock import utcnow
from tests.unit.chat.test_round3_matrix import venture as venture_fixture
from tests.unit.product.test_static_publication import _config
from tests.unit.product.test_site_address import hosted_address_tenants as hosted_address_tenants

venture = venture_fixture


@pytest.fixture()
def built(venture, monkeypatch):
    """A venture whose page-builder app is live at old-name.supymem.com."""
    from app.db.supymem_models import AppDomain, Deployment, FounderApp
    from app.hosting.service import HostingConfig
    from app.state import db
    monkeypatch.setattr(HostingConfig, "from_environment", classmethod(lambda _: _config()))
    client, vid, ws = venture
    now = utcnow()
    with db()() as session:
        session.add(FounderApp(id="app_pb123456", workspace_id=ws, venture_id=vid, name="Page", slug="old-name",
                               kind="landing", created_at=now, updated_at=now))
        session.flush()
        session.add(Deployment(id="dep_11111111", app_id="app_pb123456", workspace_id=ws, venture_id=vid,
                               build_artifact_id="bar_old", target="supymem_static", state="live", version=1,
                               url="https://old-name.supymem.com/", live_at=now))
        session.add(AppDomain(id="dom_11111111", app_id="app_pb123456", workspace_id=ws, hostname="old-name.supymem.com",
                              kind="supymem", state="active", created_at=now, active_at=now))
        session.commit()
    return client, vid, ws


def _put(client, vid, slug, **kw):
    return client.put(f"/ventures/{vid}/site-address", json={"slug": slug, "target": "pagebuilder"}, **kw)


def test_check_and_put_target_the_page_builder_app(built):
    from app.db.supymem_models import FounderApp
    from app.state import db
    client, vid, _ = built
    check = client.get(f"/ventures/{vid}/site-address/check", params={"slug": "new-name", "target": "pagebuilder"}).json()
    assert check["available"] is True and check["current"] == "old-name"
    done = _put(client, vid, "new-name")
    assert done.status_code == 200 and done.json()["live"] is True, done.text
    with db()() as session:
        assert session.get(FounderApp, "app_pb123456").slug == "new-name"
        assert session.query(FounderApp).filter_by(venture_id=vid).count() == 1  # no second first-look app was made
    assert _put(client, vid, "admin").status_code == 422
    assert _put(client, vid, "mine", headers={"X-Subject": "usr_stranger"}).status_code in (403, 404)


def test_baseline_after_an_address_change_sees_nothing_at_the_new_host(built):
    from app.build import subscription_static_baseline as baseline
    from app.db.supymem_models import Deployment, FounderApp
    from app.state import db
    client, vid, _ = built
    assert _put(client, vid, "new-name").status_code == 200
    with db()() as session:
        app, dep = session.get(FounderApp, "app_pb123456"), session.get(Deployment, "dep_11111111")
        identity = baseline._identity(session, app, dep)
        assert identity["hostname"] == "new-name.supymem.com" and identity["deployment_id"] is None
        assert identity["serving_deployment_id"] == "dep_11111111" and identity["previous_mapping"] is None
        baseline.assert_current(session, app, identity)  # the live page is still the one this review saw
        dep.state = "superseded"
        session.flush()
        with pytest.raises(ValueError, match="baseline_changed"):
            baseline.assert_current(session, app, identity)


@pytest.fixture()
def publish(built, monkeypatch):
    """publish_accepted_build with every source/QA seam faked; only the hosting adapter is observed."""
    from app.build import subscription_static_completion as completion
    from app.db.supymem_models import AppDomain, Deployment
    from app.hosting import revisions, service
    from app.state import db
    client, vid, ws = built
    assert _put(client, vid, "new-name").status_code == 200
    now = utcnow()
    with db()() as session:
        session.add(Deployment(id="dep_22222222", app_id="app_pb123456", workspace_id=ws, venture_id=vid,
                               build_artifact_id="bar_new", target="supymem_static", state="pending", version=2,
                               previous_deployment_id="dep_11111111", started_at=now))
        session.add(AppDomain(id="dom_22222222", app_id="app_pb123456", workspace_id=ws,
                              hostname="new-name.supymem.com", kind="supymem", state="pending", created_at=now))
        session.commit()
    spec = json.dumps({"lifecycle_version": 3, "inputs": {"publication_baseline": {
        "deployment_id": None, "previous_mapping": None, "serving_deployment_id": "dep_11111111"}}})
    run, artifact = SimpleNamespace(spec=spec, id="run_1"), SimpleNamespace(id="bar_new")
    manifest = {"files": [{"path": "index.html", "sha256": "a" * 64}]}
    monkeypatch.setattr(service, "_ensure_intent", lambda session, app, build_id, config, deployment_id=None: (
        session.get(Deployment, deployment_id), run, artifact, None, {"generation": 2}, manifest, "new-name.supymem.com", {}))
    monkeypatch.setattr(service, "_accepted_source", lambda *a, **k: (run, artifact, None, {"generation": 2}, manifest))
    monkeypatch.setattr(service, "_source_scope", lambda *a: None)
    monkeypatch.setattr(service, "_conversion_proof", lambda *a: {})
    monkeypatch.setattr(service, "verified_public_files", lambda *a: [])
    monkeypatch.setattr(service, "S3ArtifactStore", SimpleNamespace(configured=lambda: SimpleNamespace(source=lambda *a: b"")))
    monkeypatch.setattr(revisions, "assert_publication_baseline", lambda *a: None)
    monkeypatch.setattr(completion, "complete_at_publication", lambda *a: None)
    return service, completion, db


class Adapter:
    def __init__(self):
        self.config = _config()
        self.switches, self.restored = [], []

    def put_files(self, **kw):
        pass

    def switch(self, hostname, mapping, **kw):
        self.switches.append((hostname, mapping["deployment"], kw.get("expected_absent")))
        return "previous-for-" + hostname

    def verify_public(self, hostname, sha):
        pass

    def restore(self, hostname, mapping, previous):
        self.restored.append((hostname, previous))


def test_pagebuilder_publish_repoints_the_retired_address_to_the_same_deployment(publish):
    service, _, db = publish
    from app.db.supymem_models import AppDomain, Deployment
    adapter = Adapter()
    result = service.publish_accepted_build("dep_22222222", "app_pb123456", "run_1", actor="usr", provider=adapter)
    assert result["state"] == "live" and result["url"] == "https://new-name.supymem.com/"
    # new host first (nothing was there), then the retired host follows the same deployment
    assert adapter.switches == [("new-name.supymem.com", "dep_22222222", True), ("old-name.supymem.com", "dep_22222222", None)]
    with db()() as session:
        assert session.get(Deployment, "dep_11111111").state == "superseded"
        assert session.query(AppDomain).filter_by(hostname="old-name.supymem.com").one().kind == "supymem_alias"


def test_pagebuilder_publish_failure_restores_both_hosts(publish):
    service, completion, db = publish
    from app.hosting.service import HostingError
    adapter = Adapter()

    def boom(*a):
        raise HostingError("hosting_source_changed")

    completion.complete_at_publication = boom
    with pytest.raises(HostingError):
        service.publish_accepted_build("dep_22222222", "app_pb123456", "run_1", actor="usr", provider=adapter)
    assert adapter.restored == [("new-name.supymem.com", "previous-for-new-name.supymem.com"),
                                ("old-name.supymem.com", "previous-for-old-name.supymem.com")]
