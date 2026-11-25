"""A founder's site correction stays tenant-bound and cannot mutate publication."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app


@pytest.mark.parametrize("domain", ["factory-parts", "family-walking"])
def test_local_revision_rejects_credentials_before_storage(monkeypatch, domain):
    from app.hosting import revisions
    from app.hosting.service import HostingError
    from app.secrets import detect

    monkeypatch.setattr(revisions, "is_local", lambda: True)
    monkeypatch.setattr(detect, "looks_like_secret", lambda value: True)
    monkeypatch.setattr(revisions, "db", lambda: pytest.fail("Credentials reached revision storage"))
    with pytest.raises(HostingError, match="device_chat_secret_use_form"):
        revisions.request_revision("app", actor="you", instruction=f"Change the {domain} page.",
                                   deployment_id="deployment", source_sha256="a" * 64)


@pytest.mark.parametrize("host", ["inventory.example", "gardening.example"])
def test_local_revision_brief_uses_only_public_measurement_key(monkeypatch, host):
    load_company_os_app()
    from app.apps import keys
    from app.hosting import revisions
    from app.product_analytics import beacon

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(beacon, "public_events_url", lambda: f"https://{host}/e")
    monkeypatch.setattr(keys, "ensure_ingest_key", lambda app_id: {
        "prefix": "pk_app_fixture", "secret": "sk_app_fixture.private"
    })
    script = revisions._public_analytics_script("app_owned")
    assert 'var KEY = "pk_app_fixture"' in script
    assert "sk_app_fixture" not in script
    assert f"https://{host}/e" in script


@pytest.fixture(autouse=True)
def revision_env(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/revision.db")
    load_company_os_app()


@pytest.fixture
def revision_db(monkeypatch, tmp_path):
    from app.db.models import Base, Person, Venture, Workspace
    from app.db.session import make_engine
    from app.db.supymem_models import BuildArtifact, BuildRun, Deployment, FounderApp
    from app.hosting import revisions
    from core.clock import utcnow

    engine = make_engine(f"sqlite:///{tmp_path}/revision.db")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    now = utcnow()
    with factory() as session:
        session.add(Person(id="per_owner", display_name="Owner", email="owner@example.invalid", created_at=now))
        session.flush()
        session.add(Workspace(id="ws_owner", kind="company", name="Owner", entry_mode="idea",
                              owner_id="per_owner", tenancy="hosted", created_at=now, updated_at=now))
        session.flush()
        session.add(Venture(id="ven_owner", workspace_id="ws_owner", name="Owner",
                            raw_idea="A real venture", stage="readiness_review", created_at=now, updated_at=now))
        session.add(FounderApp(id="app_owner", workspace_id="ws_owner", venture_id="ven_owner",
                               name="Owner", slug="owner-test", kind="static_site", state="active",
                               created_at=now, updated_at=now))
        session.flush()
        original_spec = {"artifact_profile": "static_landing_v1",
                         "artifact_capture": {"manifest_digest": "c" * 64},
                         "inputs": {"plan_id": "plan_original", "plan_task_id": "task_original",
                                    "milestone": {"kind": "landing"},
                                    "task": {"capability_id": "build.run", "landing_expectations": {"contract": {"product_name": "Owner"}}},
                                    "product_context": {"product_name": "Owner"},
                                    "source": {"kind": "empty"}}}
        session.add(BuildRun(id="bld_owner", app_id="app_owner", workspace_id="ws_owner",
                             venture_id="ven_owner", trigger="founder_approved", agent="openhands",
                             state="succeeded", created_at=now, closed_at=now, runtime_version=1,
                             artifact_id="bar_owner", spec=json.dumps(original_spec)))
        session.flush()
        session.add(BuildArtifact(id="bar_owner", build_run_id="bld_owner", app_id="app_owner",
                                  workspace_id="ws_owner", kind="source_zip", s3_key="private/source",
                                  sha256="a" * 64, size_bytes=100, created_at=now))
        session.flush()
        session.add(Deployment(id="dep_owner", app_id="app_owner", workspace_id="ws_owner",
                               venture_id="ven_owner", build_artifact_id="bar_owner",
                               target="supymem_static", state="live", version=1, live_at=now))
        session.commit()

    def accepted_source(session, app, build_id):
        run = session.get(BuildRun, build_id)
        artifact = session.get(BuildArtifact, run.artifact_id)
        return run, artifact, None, {"sha256": artifact.sha256, "manifest_digest": "c" * 64}, None

    monkeypatch.setattr(revisions, "db", lambda: factory)
    monkeypatch.setattr(revisions, "_accepted_source", accepted_source)
    monkeypatch.setattr(revisions, "_public_analytics_script", lambda _app_id, *, session=None, read_only=False:
                        '<script data-supymem-analytics="1"></script>')
    yield factory, revisions
    engine.dispose()


def test_intake_is_idempotent_and_changes_no_public_artifact(revision_db):
    from app.db.supymem_models import BuildArtifact, Deployment, FounderRequest
    factory, revisions = revision_db
    baseline = revisions.revision_status("app_owner", actor="per_owner")["baseline"]
    instruction = "Remove the old setup footer and preserve the managed waitlist link."
    first = revisions.request_revision("app_owner", actor="per_owner", instruction=instruction,
                                       deployment_id=baseline["deployment_id"],
                                       source_sha256=baseline["source_sha256"])
    again = revisions.request_revision("app_owner", actor="per_owner", instruction=instruction,
                                       deployment_id=baseline["deployment_id"],
                                       source_sha256=baseline["source_sha256"])
    assert first == again
    assert first["state"] == "pending"
    assert revisions.revision_status("app_owner", actor="per_owner")["requests"] == [first]
    with factory() as session:
        assert session.get(Deployment, "dep_owner").state == "live"
        assert session.get(BuildArtifact, "bar_owner").sha256 == "a" * 64
        saved = session.get(FounderRequest, first["id"])
        assert saved.outcome == "pending"
        assert json.loads(saved.text)["source_artifact_id"] == "bar_owner"


def test_stale_baseline_and_unauthorized_actor_fail_closed(revision_db):
    from app.hosting.service import HostingError
    _, revisions = revision_db
    data = dict(instruction="Remove the outdated setup footer.", deployment_id="dep_owner",
                source_sha256="a" * 64)
    with pytest.raises(HostingError, match="hosting_revision_baseline_changed"):
        revisions.request_revision("app_owner", actor="per_owner", **(data | {"source_sha256": "b" * 64}))
    with pytest.raises(HostingError, match="hosting_app_not_found"):
        revisions.revision_status("app_owner", actor="per_other")
    with pytest.raises(HostingError, match="hosting_revision_instruction_invalid"):
        revisions.request_revision("app_owner", actor="per_owner", **(data | {"instruction": "Bad\x00text"}))


def test_legacy_published_static_source_without_profile_remains_revisable(revision_db):
    from app.db.supymem_models import BuildRun
    from app.hosting.service import HostingError

    factory, revisions = revision_db
    with factory() as session:
        run = session.get(BuildRun, "bld_owner")
        spec = json.loads(run.spec)
        spec.pop("artifact_profile")
        run.spec = json.dumps(spec)
        session.commit()
    baseline = revisions.revision_status("app_owner", actor="per_owner")["baseline"]
    assert baseline["build_id"] == "bld_owner"
    request = revisions.request_revision("app_owner", actor="per_owner",
        instruction="Remove the obsolete footer but keep the managed waitlist link.",
        deployment_id=baseline["deployment_id"], source_sha256=baseline["source_sha256"])
    assert request["state"] == "pending"

    with factory() as session:
        run = session.get(BuildRun, "bld_owner")
        spec = json.loads(run.spec)
        spec["artifact_profile"] = "founder_product_v1"
        run.spec = json.dumps(spec)
        session.commit()
    with pytest.raises(HostingError, match="hosting_live_source_unverified"):
        revisions.revision_status("app_owner", actor="per_owner")


def test_revision_build_inputs_seed_exact_source_and_publisher_requires_review(revision_db):
    from app.db.supymem_models import FounderApp, FounderRequest
    from app.hosting.service import HostingError
    factory, revisions = revision_db
    request = revisions.request_revision("app_owner", actor="per_owner",
        instruction="Remove the obsolete footer; keep the waitlist URL.",
        deployment_id="dep_owner", source_sha256="a" * 64)
    with factory() as session:
        app = session.get(FounderApp, "app_owner")
        inputs = revisions.build_inputs(session, app, request["id"], actor="per_owner")
        assert inputs["source"] == {"kind": "artifact", "id": "bar_owner",
                                    "sha256": "a" * 64, "size_bytes": 100}
        assert inputs["revision"]["source_manifest_digest"] == "c" * 64
        assert inputs["product_context"]["revision_instruction"].startswith("Remove")
        assert 'data-supymem-analytics' in inputs["product_context"]["analytics_beacon_script"]
        new_run = type("Revised", (), {"spec": json.dumps({"inputs": inputs})})()
        new_artifact = type("Artifact", (), {"id": "bar_new"})()
        with pytest.raises(HostingError, match="hosting_revision_not_approved"):
            revisions.assert_publication_baseline(session, app, new_run, new_artifact)
        session.get(FounderRequest, request["id"]).outcome = "approved"
        session.flush()
        revisions.assert_publication_baseline(session, app, new_run, new_artifact)


def test_revision_artifact_must_change_and_install_exactly_one_shared_beacon():
    from app.builder.artifacts import _validate_revision_artifact
    from app.builder.model_proxy import BuildModelError

    spec = {"inputs": {"revision": {"source_manifest_digest": "old"},
                       "product_context": {"analytics_beacon_script": '<script data-supymem-analytics="1">shared()</script>'}}}
    with pytest.raises(BuildModelError, match="artifact_revision_unchanged"):
        _validate_revision_artifact(spec, {"manifest_digest": "old"})
    with pytest.raises(BuildModelError, match="artifact_revision_analytics_missing"):
        _validate_revision_artifact(spec, {"manifest_digest": "new"}, html="<html><body>no beacon</body></html>")
    with pytest.raises(BuildModelError, match="artifact_revision_analytics_missing"):
        _validate_revision_artifact(spec, {"manifest_digest": "new"},
            html='<script data-supymem-analytics="1">old()</script>' + spec["inputs"]["product_context"]["analytics_beacon_script"])
    _validate_revision_artifact(spec, {"manifest_digest": "new"},
        html="<html><body>" + spec["inputs"]["product_context"]["analytics_beacon_script"] + "</body></html>")


def test_revision_switch_refuses_a_concurrent_public_mapping_change():
    from core.artifact_snapshot import canonical
    from core.hosting_contracts import host_mapping
    from app.hosting.service import AwsStaticPublisher, HostingConfig, HostingError

    old = host_mapping(app_id="app_12345678", deployment_id="dep_12345678", generation=1, enabled=True)
    new = host_mapping(app_id="app_12345678", deployment_id="dep_87654321", generation=1, enabled=True)

    class Kvs:
        writes = 0

        def describe_key_value_store(self, **_kwargs):
            return {"ETag": "etag", "TotalSizeInBytes": 400}

        def get_key(self, **_kwargs):
            return {"Value": canonical(old).decode()}

        def put_key(self, **_kwargs):
            self.writes += 1

    kvs = Kvs()
    config = HostingConfig(bucket="test-sites", region="us-east-1", expected_owner="123456789012",
                           kvs_arn="arn:aws:cloudfront::123456789012:key-value-store/kvs_test",
                           public_domain="supymem.com")
    publisher = AwsStaticPublisher(config, None, kvs)
    with pytest.raises(HostingError, match="hosting_revision_baseline_changed"):
        publisher.switch("site.supymem.com", new, expected_previous_deployment_id="dep_another")
    assert kvs.writes == 0
    publisher.switch("site.supymem.com", new, expected_previous_deployment_id="dep_12345678")
    assert kvs.writes == 1
