"""Exact hosting custody, retry/lost ACK and truthful takedown receipts."""
import json
from io import BytesIO
from urllib.error import HTTPError

import pytest
from core.artifact_snapshot import canonical
from core.clock import utcnow
from core.hosting_contracts import host_mapping
from tests.unit.chat.test_round3_matrix import venture as venture_fixture
from tests.unit.product.test_static_publication import _config, _client_error
from tests.unit.product.test_site_address import hosted_address_tenants as hosted_address_tenants
from tests.integration.test_postgres_auth_security import service_modules
from tests.company_os_harness import load_company_os_app

load_company_os_app()

venture = venture_fixture


def test_disable_cas_lost_ack_and_later_publication():
    with service_modules("company_os"):
        from app.hosting.service import AwsStaticPublisher, HostingError
    expected = host_mapping(app_id="app_12345678", deployment_id="dep_12345678", generation=1, enabled=True)
    class Kvs:
        value = expected
        calls = 0
        lost = True
        def describe_key_value_store(self, **_): return {"ETag": str(self.calls)}
        def get_key(self, **_): return {"Value": canonical(self.value).decode()}
        def put_key(self, **kwargs):
            assert kwargs["IfMatch"] == str(self.calls)
            self.calls += 1
            self.value = json.loads(kwargs["Value"])
            if self.lost:
                self.lost = False
                raise _client_error("InternalServerError")
    kvs = Kvs()
    provider = AwsStaticPublisher(_config(), None, kvs)
    with pytest.raises(HostingError, match="write_uncertain"):
        provider.disable("studio.supymem.com", expected)
    assert kvs.value == {**expected, "enabled": False}
    provider.disable("studio.supymem.com", expected)
    provider.verify_disabled_mapping("studio.supymem.com", expected)
    assert kvs.calls == 1
    later = host_mapping(app_id=expected["app"], deployment_id="dep_87654321", generation=2, enabled=True)
    kvs.value = later
    with pytest.raises(HostingError, match="baseline_changed"):
        provider.disable("studio.supymem.com", expected)
    assert kvs.value == later and kvs.calls == 1


def test_public_readback_requires_404_and_matching_disabled_mapping(monkeypatch):
    with service_modules("company_os"):
        from app.hosting import service
    expected = host_mapping(app_id="app_12345678", deployment_id="dep_12345678", generation=1, enabled=True)
    class Kvs:
        value = {**expected, "enabled": False}
        def get_key(self, **_): return {"Value": canonical(self.value).decode()}
    class Opener:
        code = 404
        def open(self, request, **_):
            raise HTTPError(request.full_url, self.code, "test", {}, BytesIO(b""))
    kvs, opener = Kvs(), Opener()
    provider = service.AwsStaticPublisher(_config(), None, kvs)
    monkeypatch.setattr(service, "build_opener", lambda *_: opener)
    monkeypatch.setattr(service.time, "sleep", lambda _: None)
    provider.verify_offline("studio.supymem.com", expected)
    opener.code = 503
    with pytest.raises(service.HostingError, match="verification_pending"):
        provider.verify_offline("studio.supymem.com", expected)
    kvs.value = expected
    with pytest.raises(service.HostingError, match="baseline_changed"):
        provider.verify_offline("studio.supymem.com", expected)


@pytest.fixture()
def hosted(venture, monkeypatch):
    from app.db.models import Person, WorkspaceMember
    from app.db.supymem_models import FounderApp, BuildRun, BuildArtifact, Deployment, AppDomain
    from app.state import db
    from app.hosting.service import HostingConfig
    client, vid, ws = venture
    monkeypatch.setattr(HostingConfig, "from_environment", classmethod(lambda _: _config()))
    now = utcnow()
    app_id, dep_id, artifact_id, build_id = "app_12345678", "dep_12345678", "bar_12345678", "brn_12345678"
    with db()() as session:
        session.add(Person(id="tester", display_name="Taylor", created_at=now))
        session.flush()
        session.add(WorkspaceMember(id="wsm_tester", workspace_id=ws, person_id="tester",
                                    role="maintainer", can_approve=1, created_at=now))
        session.add(FounderApp(id=app_id, workspace_id=ws, venture_id=vid, name="Studio", slug="studio",
                              kind="web", created_at=now, updated_at=now))
        session.flush()
        session.add(BuildRun(id=build_id, app_id=app_id, workspace_id=ws, venture_id=vid, state="succeeded",
            trigger="test", agent="test", created_at=now, artifact_id=artifact_id,
            spec=json.dumps({"generation": 1, "artifact_capture": {"artifact_id": artifact_id, "sha256": "a" * 64}})))
        session.flush()
        session.add(BuildArtifact(id=artifact_id, build_run_id=build_id, app_id=app_id, workspace_id=ws,
            kind="source_zip", s3_key="retained-source", sha256="a" * 64, size_bytes=10, created_at=now))
        session.add(Deployment(id=dep_id, app_id=app_id, workspace_id=ws, venture_id=vid,
            build_artifact_id=artifact_id, target="supymem_static", state="live", version=1,
            url="https://studio.supymem.com/", live_at=now))
        session.add(AppDomain(id="dom_12345678", app_id=app_id, workspace_id=ws,
            hostname="studio.supymem.com", kind="supymem", state="active", created_at=now))
        session.commit()
    return client, app_id, dep_id, vid, ws


@pytest.mark.parametrize("role", ["owner", "admin", "maintainer", "contributor", "viewer", None])
def test_unpublish_requires_app_management_at_both_entrypoints(hosted, role):
    from app.db.models import Job, WorkspaceMember
    from app.db.supymem_models import Deployment
    from app.hosting import unpublish
    from app.state import db

    client, app_id, dep_id, vid, _ = hosted
    with db()() as session:
        member = session.get(WorkspaceMember, "wsm_tester")
        if role:
            member.role = role
        else:
            session.delete(member)
        session.commit()
    permitted = role in {"owner", "admin", "maintainer"}
    expected = 202 if permitted else 403 if role else 404
    response = client.post(f"/apps/{app_id}/deployments/{dep_id}/unpublish", headers={"X-Subject": "tester"})
    assert response.status_code == expected, response.text
    response = client.post(f"/ventures/{vid}/chat/confirm", headers={"X-Subject": "tester"},
                           json={"action": "site.unpublish", "payload": {
                               "app_id": app_id, "deployment_id": dep_id, "version": 1}})
    assert response.status_code == (200 if permitted else expected), response.text
    with db()() as session:
        jobs = session.query(Job).filter_by(kind="deploy.unpublish").all()
        assert len(jobs) == int(permitted)
        assert session.get(Deployment, dep_id).state == "live"
        payload = json.loads(jobs[0].payload_json) if permitted else None
    if not permitted:
        with pytest.raises(unpublish.HostingError) as exc:
            unpublish.queue(app_id, dep_id, actor="tester")
        assert exc.value.status == expected
    else:
        calls = []
        class Provider:
            config = _config()
            def disable(self, *_): calls.append("disable")
            def verify_offline(self, *_): calls.append("offline")
            def verify_disabled_mapping(self, *_): calls.append("mapping")
        assert unpublish.execute(payload, provider=Provider())["state"] == "unpublished"
        assert calls == ["disable", "offline", "mapping"]


@pytest.mark.parametrize("role", ["contributor", "viewer", None])
def test_unpublish_rechecks_queued_actor_before_receipts_and_provider_effects(hosted, role):
    from app.db.models import Job, WorkspaceMember
    from app.db.supymem_models import Deployment
    from app.hosting import unpublish
    from app.state import db

    _, app_id, dep_id, _, _ = hosted
    queued = unpublish.queue(app_id, dep_id, actor="tester")
    with db()() as session:
        payload = json.loads(session.get(Job, queued["unpublish"]["job_id"]).payload_json)
        member = session.get(WorkspaceMember, "wsm_tester")
        if role:
            member.role = role
        else:
            session.delete(member)
        session.commit()
    class Provider:
        config = _config()
        def disable(self, *_): pytest.fail("unauthorized hosting effect")
        def verify_offline(self, *_): pytest.fail("unauthorized provider readback")
    for state in ("live", "unpublished"):
        with db()() as session:
            session.get(Deployment, dep_id).state = state
            session.commit()
        with pytest.raises(unpublish.HostingError) as exc:
            unpublish.queue(app_id, dep_id, actor="tester")
        assert exc.value.status == (403 if role else 404)
        with pytest.raises(unpublish.HostingError) as exc:
            unpublish.execute(payload, provider=Provider())
        assert exc.value.status == (403 if role else 404)


def test_durable_queue_idempotency_verified_completion_preserves_source(hosted):
    from app.hosting import unpublish
    from app.db.models import Job
    from app.db.supymem_models import BuildArtifact, Deployment, AppDomain
    from app.state import db
    client, app_id, dep_id, _, _ = hosted
    first = client.post(f"/apps/{app_id}/deployments/{dep_id}/unpublish")
    assert first.status_code == 202, first.text
    assert first.json()["state"] == "live"  # queued is not offline
    second = client.post(f"/apps/{app_id}/deployments/{dep_id}/unpublish")
    assert second.json()["unpublish"]["job_id"] == first.json()["unpublish"]["job_id"]
    with db()() as session:
        job = session.get(Job, first.json()["unpublish"]["job_id"])
        payload = json.loads(job.payload_json)
        app = session.get(unpublish.FounderApp, app_id)
        with pytest.raises(unpublish.HostingError, match="in_progress"):
            unpublish.assert_no_takedown(session, app)
    class Provider:
        config = _config()
        checks = []
        def disable(self, *_): self.checks.append("disable")
        def verify_offline(self, *_): self.checks.append("public404")
        def verify_disabled_mapping(self, *_): self.checks.append("mapping")
    provider = Provider()
    result = unpublish.execute(payload, provider=provider)
    assert result["state"] == "unpublished"
    assert provider.checks == ["disable", "public404", "mapping"]
    with db()() as session:
        assert session.get(BuildArtifact, "bar_12345678").s3_key == "retained-source"
        assert session.get(AppDomain, "dom_12345678").state == "disabled"
        assert session.get(Deployment, dep_id).state == "unpublished"
    assert unpublish.execute(payload, provider=provider)["state"] == "unpublished"
    assert len(provider.checks) == 3


def test_failed_readback_is_not_completion_and_stale_scope_has_no_effect(hosted):
    from app.hosting import unpublish
    from app.db.models import Job
    from app.db.supymem_models import Deployment
    from app.state import db
    _, app_id, dep_id, _, _ = hosted
    queued = unpublish.queue(app_id, dep_id, actor="tester")
    with db()() as session:
        payload = json.loads(session.get(Job, queued["unpublish"]["job_id"]).payload_json)
    class Provider:
        config = _config()
        calls = 0
        def disable(self, *_): self.calls += 1
        def verify_offline(self, *_): raise unpublish.HostingError("hosting_unpublish_verification_pending")
    provider = Provider()
    with pytest.raises(unpublish.HostingError, match="intent_invalid"):
        unpublish.execute({**payload, "venture_id": "ven_foreign"}, provider=provider)
    assert provider.calls == 0
    with pytest.raises(unpublish.HostingError, match="verification_pending"):
        unpublish.execute(payload, provider=provider)
    with db()() as session:
        assert session.get(Deployment, dep_id).state == "live"


def test_inflight_publish_prevents_takedown_and_chat_freezes_target(hosted):
    from app.hosting import unpublish
    from app.chat import unpublish as chat
    from app.db.supymem_models import Deployment
    from app.state import db
    client, app_id, dep_id, vid, ws = hosted
    routed = client.post(f"/ventures/{vid}/chat", json={"message": "Unpublish my website"})
    assert routed.status_code == 200, routed.text
    assert routed.json()["kind"] == "needs_you", routed.text  # the request runs; the in-flight publish refuses it
    card = chat.prepare(vid)
    assert card["kind"] == "confirmation"
    assert card["actions"][0]["action"] == "site.unpublish"
    assert chat.execute("ven_foreign", {"app_id": app_id, "deployment_id": dep_id, "version": 1}, "tester")["kind"] == "text"
    with db()() as session:
        session.add(Deployment(id="dep_87654321", app_id=app_id, workspace_id=ws, venture_id=vid,
            target="supymem_static", state="pushing", version=2))
        session.commit()
    with pytest.raises(unpublish.HostingError, match="publication_in_progress"):
        unpublish.queue(app_id, dep_id, actor="tester")


@pytest.mark.parametrize("text, expected", [
    ("Unpublish my website", "site.takedown"), ("Take the site offline", "site.takedown"),
    ("Take my landing page down", "site.takedown"), ("Do not unpublish my website", "assistant"),
    ("How do I unpublish my website?", "assistant"), ("Don't take the site offline", "assistant"),
])
def test_unpublish_routing_does_not_treat_negations_or_questions_as_authority(text, expected):
    from app.chat.site_actions import classify_site_intent
    assert classify_site_intent(text) == expected


def test_cas_conflict_never_retries_over_a_newer_mapping():
    with service_modules("company_os"):
        from app.hosting.service import AwsStaticPublisher, HostingError
    expected = host_mapping(app_id="app_12345678", deployment_id="dep_12345678", generation=1, enabled=True)
    later = host_mapping(app_id="app_12345678", deployment_id="dep_87654321", generation=2, enabled=True)
    class Kvs:
        value = expected
        writes = 0
        def describe_key_value_store(self, **_): return {"ETag": str(self.writes)}
        def get_key(self, **_): return {"Value": canonical(self.value).decode()}
        def put_key(self, **_):
            self.writes += 1
            self.value = later
            raise _client_error("PreconditionFailedException")
    kvs = Kvs()
    with pytest.raises(HostingError, match="baseline_changed"):
        AwsStaticPublisher(_config(), None, kvs).disable("studio.supymem.com", expected)
    assert kvs.value == later and kvs.writes == 1


def test_foreign_owner_api_cannot_queue_public_takedown(hosted):
    from app.db.models import Job
    from app.state import db
    from sqlalchemy import select
    client, app_id, dep_id, _, _ = hosted
    response = client.post(f"/apps/{app_id}/deployments/{dep_id}/unpublish", headers={"X-Subject": "usr_foreign_unpublish"})
    assert response.status_code == 404, response.text
    with db()() as session:
        assert not session.scalars(select(Job).where(Job.kind == "deploy.unpublish")).all()
