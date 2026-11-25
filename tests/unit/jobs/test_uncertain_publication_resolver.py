"""A public mapping read can close uncertainty, never repeat publication."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from core.clock import utcnow
from core.hosting_contracts import host_mapping
from sqlalchemy import select

from tests.unit.credits.conftest import account as account, credits_env as credits_env
from tests.unit.product.test_static_publication import _config


@pytest.fixture
def publication(account, monkeypatch):
    from app.db.models import SiteVersion
    from app.db.supymem_models import AppDomain, Deployment, FounderApp
    from app.hosting import firstlook_publish
    from app.hosting.service import AwsStaticPublisher, HostingConfig
    from app.state import db

    now = utcnow()
    venture_id = account.attribution_venture_id
    workspace_id = "ws_wallet:" + venture_id
    page = {"workspace_id": workspace_id, "sha256": "a" * 64, "waitlist_url": "https://example.test/waitlist"}
    monkeypatch.setattr(firstlook_publish, "public_page", lambda *args, **kwargs: page)
    monkeypatch.setattr(firstlook_publish, "_waitlist_receipt", lambda *args: {})
    monkeypatch.setattr(firstlook_publish, "_ensure_app", lambda *args: "app_12345678")
    monkeypatch.setattr(HostingConfig, "from_environment", classmethod(lambda _: _config()))
    with db()() as session:
        session.add(FounderApp(id="app_12345678", workspace_id=workspace_id, venture_id=venture_id,
            slug="readback", name="Readback", kind="landing", build_mode="firstlook", created_at=now, updated_at=now))
        session.flush()
        session.add_all([
            Deployment(id="dep_12345678", app_id="app_12345678", workspace_id=workspace_id, venture_id=venture_id,
                target="supymem_static", state="live", version=1, live_at=now,
                url="https://readback.supymem.com/", build_artifact_id="firstlook:site_old"),
            Deployment(id="dep_87654321", app_id="app_12345678", workspace_id=workspace_id, venture_id=venture_id,
                target="supymem_static", state="failed", error="hosting_public_state_uncertain", version=2,
                previous_deployment_id="dep_12345678", started_at=now - timedelta(minutes=6),
                build_artifact_id="firstlook:site_current"),
            AppDomain(id="dom_readback", app_id="app_12345678", workspace_id=workspace_id,
                hostname="readback.supymem.com", kind="supymem", state="pending", created_at=now),
            SiteVersion(id="site_current", workspace_id=workspace_id, venture_id=venture_id, version=1,
                stage="first_look", template="synthetic", archetype="synthetic", brandkit_hash="b" * 64,
                copy_hash="c" * 64, path="synthetic", sha256="a" * 64, created_at=now),
        ])
        session.commit()

    class ReadOnlyPublisher(AwsStaticPublisher):
        def __init__(self):
            self.mapping = host_mapping(app_id="app_12345678", deployment_id="dep_87654321", generation=2, enabled=True)
            self.error = None
            self.reads = []
            self.writes = []
            super().__init__(_config(), SimpleNamespace(put_object=self.forbidden),
                SimpleNamespace(get_key=self.get_key, put_key=self.forbidden, delete_key=self.forbidden))

        def get_key(self, **kwargs):
            self.reads.append(kwargs)
            if self.error:
                raise self.error
            return {"Value": json.dumps(self.mapping)}

        def forbidden(self, *args, **kwargs):
            self.writes.append(True)
            raise AssertionError("recovery must never write KVS or publish")

        switch = restore = disable = put_files = forbidden

    return SimpleNamespace(provider=ReadOnlyPublisher(), now=now, venture_id=venture_id, workspace_id=workspace_id)


@pytest.mark.parametrize("managed", [False, True])
def test_this_deployment_readback_commits_live_bookkeeping_once(publication, monkeypatch, managed):
    from app.build import subscription_static_completion
    from app.db.models import ChatMessage, EventLog, SiteVersion
    from app.db.supymem_models import AppDomain, BuildArtifact, BuildRun, Deployment
    from app.hosting import revisions, service
    from app.state import db

    completed = []
    if managed:
        with db()() as session:
            session.get(Deployment, "dep_87654321").build_artifact_id = "bar_current"
            run = BuildRun(id="run_current", app_id="app_12345678", workspace_id=publication.workspace_id,
                venture_id=publication.venture_id, trigger="synthetic", agent="synthetic", spec="{}")
            session.add(run)
            session.flush()
            artifact = BuildArtifact(id="bar_current", build_run_id=run.id, app_id="app_12345678",
                workspace_id=publication.workspace_id, kind="static", s3_key="synthetic",
                sha256="a" * 64, created_at=publication.now)
            session.add(artifact)
            session.commit()
            session.refresh(run)
            session.refresh(artifact)
            session.expunge(run)
            session.expunge(artifact)
        monkeypatch.setattr(service, "_accepted_source", lambda *args: (run, artifact, None, {}, {}))
        monkeypatch.setattr(revisions, "assert_publication_baseline", lambda *args: None)
        monkeypatch.setattr(service, "_conversion_proof", lambda *args: {})
        monkeypatch.setattr(subscription_static_completion, "complete_at_publication",
            lambda *args: completed.append(args[3].state))
    assert service.reconcile_uncertain_publications(now=publication.now, provider=publication.provider) == 1
    assert service.reconcile_uncertain_publications(now=publication.now, provider=publication.provider) == 0
    assert completed == (["live"] if managed else [])
    assert publication.provider.writes == []
    assert publication.provider.reads == [{"KvsARN": _config().kvs_arn, "Key": "readback.supymem.com"}]
    with db()() as session:
        deployment = session.get(Deployment, "dep_87654321")
        assert (deployment.state, deployment.error) == ("live", None)
        assert deployment.live_at is not None and deployment.previous_deployment_id == "dep_12345678"
        assert deployment.url == deployment.health_url == "https://readback.supymem.com/"
        assert session.get(Deployment, "dep_12345678").state == "superseded"
        domain = session.get(AppDomain, "dom_readback")
        assert domain.state == "active" and domain.active_at is not None
        assert session.get(SiteVersion, "site_current").published == int(not managed)
        messages = list(session.scalars(select(ChatMessage).where(ChatMessage.venture_id == publication.venture_id)))
        assert len(messages) == 1 and messages[0].kind == "site_live" and "is live" in messages[0].body_md
        audit = session.scalar(select(EventLog).where(EventLog.topic == "hosting.publication_live"))
        assert audit.surface == "internal" and json.loads(audit.payload_json)["resolved_by"] == "maintenance_readback"


@pytest.mark.parametrize("absent", [False, True])
def test_predecessor_or_absent_readback_allows_new_firstlook_publication(publication, absent):
    from app.db.models import ChatMessage
    from app.db.supymem_models import Deployment
    from app.hosting import firstlook_publish, service
    from app.state import db

    if absent:
        publication.provider.error = ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetKey")
        with db()() as session:
            session.get(Deployment, "dep_12345678").state = "superseded"
            session.commit()
    else:
        publication.provider.mapping = host_mapping(app_id="app_12345678", deployment_id="dep_12345678", generation=1, enabled=True)
    assert service.reconcile_uncertain_publications(now=publication.now, provider=publication.provider) == 1
    assert service.reconcile_uncertain_publications(now=publication.now, provider=publication.provider) == 0
    with db()() as session:
        deployment = session.get(Deployment, "dep_87654321")
        assert (deployment.state, deployment.error) == ("failed", "hosting_publication_interrupted")
        messages = list(session.scalars(select(ChatMessage).where(ChatMessage.venture_id == publication.venture_id)))
        assert len(messages) == 1 and messages[0].kind == "site_publish_failed"
        assert "You can publish again." in messages[0].body_md
    result = firstlook_publish.queue_publication(publication.venture_id, actor="system",
        site_version_id="site_current", public_sha256="a" * 64)
    assert result["state"] == "pending" and result["id"] != "dep_87654321" and result["job_id"]
    assert len(publication.provider.reads) == 1
    assert publication.provider.writes == []


@pytest.mark.parametrize("failure", ["read", "invalid", "disabled", "foreign", "domain"])
def test_unconfirmed_readback_or_invalid_bookkeeping_leaves_everything_unchanged(publication, monkeypatch, failure):
    from app.db.models import ChatMessage, SiteVersion
    from app.db.supymem_models import AppDomain, Deployment
    from app.hosting import service
    from app.state import db

    if failure == "read":
        publication.provider.error = ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetKey")
    elif failure == "invalid":
        publication.provider.mapping = {"deployment": "dep_87654321"}
    elif failure == "disabled":
        publication.provider.mapping["enabled"] = False
    elif failure == "foreign":
        publication.provider.mapping = host_mapping(app_id="app_12345678", deployment_id="dep_99999999", generation=3, enabled=True)
    else:
        with db()() as session:
            session.get(AppDomain, "dom_readback").hostname = "other.supymem.com"
            session.commit()
    assert service.reconcile_uncertain_publications(now=publication.now, provider=publication.provider) == 0
    assert publication.provider.writes == []
    with db()() as session:
        deployment = session.get(Deployment, "dep_87654321")
        assert (deployment.state, deployment.error) == ("failed", "hosting_public_state_uncertain")
        assert deployment.url is None and deployment.live_at is None
        assert session.get(Deployment, "dep_12345678").state == "live"
        assert session.get(AppDomain, "dom_readback").state == "pending"
        assert session.get(SiteVersion, "site_current").published == 0
        assert session.scalar(select(ChatMessage.id).where(ChatMessage.venture_id == publication.venture_id)) is None


@pytest.mark.parametrize("failure", ["firstlook", "conversion", "completion"])
def test_proven_publication_stays_live_when_optional_gates_fail(publication, monkeypatch, caplog, failure):
    from app.build import subscription_static_completion
    from app.db.models import ChatMessage
    from app.db.supymem_models import AppDomain, BuildArtifact, BuildRun, Deployment
    from app.hosting import firstlook_publish, revisions, service
    from app.state import db

    def refuse(*args, **kwargs):
        raise service.HostingError("hosting_conversion_test_required")

    monkeypatch.setattr(firstlook_publish, "public_page", refuse)
    monkeypatch.setattr(firstlook_publish, "_waitlist_receipt", refuse)
    if failure != "firstlook":
        with db()() as session:
            session.get(Deployment, "dep_87654321").build_artifact_id = "bar_current"
            session.add(BuildRun(id="run_current", app_id="app_12345678", workspace_id=publication.workspace_id,
                venture_id=publication.venture_id, trigger="synthetic", agent="synthetic", spec="{}"))
            session.flush()
            session.add(BuildArtifact(id="bar_current", build_run_id="run_current", app_id="app_12345678",
                workspace_id=publication.workspace_id, kind="static", s3_key="synthetic",
                sha256="a" * 64, created_at=publication.now))
            session.commit()

        def accepted(session, app, build_id):
            return session.get(BuildRun, build_id), session.get(BuildArtifact, "bar_current"), None, {}, {}

        def fail_completion(session, app, run, *args):
            run.spec = '{"task_completion": "partial"}'
            session.flush()
            refuse()

        monkeypatch.setattr(service, "_accepted_source", accepted)
        monkeypatch.setattr(revisions, "assert_publication_baseline", lambda *args: None)
        monkeypatch.setattr(service, "_conversion_proof", fail_completion if failure == "conversion" else lambda *args: {})
        monkeypatch.setattr(subscription_static_completion, "complete_at_publication",
            lambda session, run, *args: fail_completion(session, None, run))
    assert service.reconcile_uncertain_publications(now=publication.now, provider=publication.provider) == 1
    assert service.reconcile_uncertain_publications(now=publication.now, provider=publication.provider) == 0
    assert publication.provider.writes == []
    with db()() as session:
        deployment = session.get(Deployment, "dep_87654321")
        assert (deployment.state, deployment.error) == ("live", None)
        assert deployment.live_at is not None and deployment.url == deployment.health_url == "https://readback.supymem.com/"
        assert session.get(Deployment, "dep_12345678").state == "superseded"
        assert session.get(AppDomain, "dom_readback").state == "active"
        messages = list(session.scalars(select(ChatMessage).where(ChatMessage.venture_id == publication.venture_id)))
        assert len(messages) == 1 and messages[0].kind == "site_live" and "is live" in messages[0].body_md
        if failure != "firstlook":
            assert session.get(BuildRun, "run_current").spec == "{}"
            assert "hosting_publication_completion_pending" in caplog.text


@pytest.mark.parametrize("candidate", ["none", "recent", "payload_job", "dedupe_job"])
def test_no_eligible_candidate_never_constructs_provider(publication, monkeypatch, candidate):
    from app.core.jobs import enqueue
    from app.db.supymem_models import Deployment
    from app.hosting import service
    from app.state import db

    with db()() as session:
        deployment = session.get(Deployment, "dep_87654321")
        if candidate == "none":
            deployment.error = "hosting_publication_interrupted"
        elif candidate == "recent":
            deployment.started_at = publication.now
        else:
            enqueue(session, "deploy.run", {"deployment_id": deployment.id} if candidate == "payload_job" else {},
                workspace_id=publication.workspace_id, venture_id=publication.venture_id,
                dedupe_key="static-publish:" + deployment.id if candidate == "dedupe_job" else None)
        session.commit()
    monkeypatch.setattr(service.AwsStaticPublisher, "configured", publication.provider.forbidden)
    assert service.reconcile_uncertain_publications(now=publication.now) == 0
    assert publication.provider.reads == []
    assert publication.provider.writes == []
