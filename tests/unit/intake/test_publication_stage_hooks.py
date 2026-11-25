"""Synthetic SQLite commits and mocked provider effects exercise both real publishers."""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.product.test_static_publication import _config


@pytest.fixture(params=["accepted", "firstlook"])
def publication(request, tmp_path, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/publication.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.authz.workspace import ensure_subject_person
    from app.chat import milestone_hooks
    from app.db.models import SiteVersion, Venture
    from app.db.supymem_models import AppDomain, Deployment, FounderApp
    from app.hosting import firstlook_publish, revisions, service, unpublish
    from app.build import subscription_static_completion
    from app.state import db
    from app.ventures import stage
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    actor = ensure_subject_person("synthetic_publication_hook_founder")
    workspace = create_workspace(kind="company", name="Synthetic publication hook", owner_id=actor)
    factory, now = db(), utcnow()
    vid, app_id, dep_id, site_id = "ven_stage_hook", "app_12345678", "dep_12345678", "site_stage_hook"
    source = "art_stage_hook" if request.param == "accepted" else firstlook_publish.source_id(site_id)
    with factory() as session:
        session.add(Venture(id=vid, workspace_id=workspace.id, name="Fixture", raw_idea="Synthetic product",
            stage="draft", created_at=now, updated_at=now))
        session.flush()
        session.add(FounderApp(id=app_id, workspace_id=workspace.id, venture_id=vid, slug="stage-hook",
            name="Fixture", kind="static_site", build_mode="firstlook" if request.param == "firstlook" else "supymem",
            state="draft", created_at=now, updated_at=now))
        session.flush()
        session.add(Deployment(id=dep_id, app_id=app_id, workspace_id=workspace.id, venture_id=vid,
            build_artifact_id=source, target="supymem_static", state="pending", version=1, started_at=now))
        session.add(AppDomain(id="dom_stage_hook", app_id=app_id, workspace_id=workspace.id,
            hostname="stage-hook.supymem.com", kind="supymem", state="pending", created_at=now))
        session.add(SiteVersion(id=site_id, workspace_id=workspace.id, venture_id=vid, version=1,
            stage="first_look", template="fixture", archetype="fixture", brandkit_hash="fixture",
            copy_hash="fixture", path="fixture", sha256="a" * 64, published=0, created_at=now))
        session.commit()

    events, state = [], SimpleNamespace(active_sessions=0, fail_commit=False, source_changed=False, source_reads=0)
    module = service if request.param == "accepted" else firstlook_publish

    @contextmanager
    def tracked_session():
        with factory() as session:
            original_commit = session.commit
            def commit():
                if state.fail_commit and session.get(Deployment, dep_id).state == "live":
                    raise RuntimeError("synthetic_live_commit_failure")
                return original_commit()
            monkeypatch.setattr(session, "commit", commit)
            state.active_sessions += 1
            try:
                yield session
            finally:
                state.active_sessions -= 1

    monkeypatch.setattr(module, "db", lambda: tracked_session)
    monkeypatch.setattr(module, "serialize_writes", lambda *args, **kwargs: None)
    monkeypatch.setattr(unpublish, "assert_no_takedown", lambda *args: None)
    monkeypatch.setattr(module, "audit", lambda kind, **kwargs: events.append((kind, None)))
    monkeypatch.setattr(module, "_record_site_published", lambda *args: events.append(("analytics", None)))
    monkeypatch.setattr(milestone_hooks, "deployment_live", lambda *args: events.append(("notice", None)))
    monkeypatch.setattr(milestone_hooks, "deployment_failed", lambda *args: events.append(("failed_notice", None)))

    class Provider:
        config = _config(service)
        readback_failure = False
        restored = 0
        effects = 0
        def put_files(self, **kwargs):
            self.effects += 1
        def switch(self, *args, **kwargs):
            self.effects += 1
            return None
        def verify_public(self, *args):
            self.effects += 1
            if self.readback_failure:
                raise service.HostingError("hosting_public_health_mismatch")
        def restore(self, *args):
            self.restored += 1
    provider = Provider()

    def after_evidence(owner, event_key):
        assert state.active_sessions == 0, "the hook must run after the publishing session exits"
        with factory() as session:
            row = session.get(Deployment, event_key.removeprefix("publication:"))
            assert (row.state, row.venture_id, row.workspace_id) == ("live", owner, workspace.id)
            assert row.live_at is not None and row.url == "https://stage-hook.supymem.com/"
            if request.param == "firstlook":
                assert session.get(SiteVersion, site_id).published == 1
        events.append(("stage", (owner, event_key)))
    monkeypatch.setattr(stage, "after_evidence", after_evidence, raising=False)

    if request.param == "accepted":
        run, artifact, qa = SimpleNamespace(spec="{}"), SimpleNamespace(id=source), SimpleNamespace()
        capture, manifest = {"generation": 1}, {"files": [{"path": "index.html", "sha256": "a" * 64}]}
        monkeypatch.setattr(service, "_ensure_intent", lambda session, *args, **kwargs:
            (session.get(Deployment, dep_id), run, artifact, qa, capture, manifest, "stage-hook.supymem.com", {}))
        def accepted_source(*args):
            state.source_reads += 1
            if state.source_changed and state.source_reads == 2:
                raise service.HostingError("hosting_source_changed")
            return run, artifact, qa, capture, manifest
        monkeypatch.setattr(service, "_accepted_source", accepted_source)
        monkeypatch.setattr(service, "_source_scope", lambda *args: {})
        monkeypatch.setattr(service, "_conversion_proof", lambda *args: {})
        monkeypatch.setattr(revisions, "assert_publication_baseline", lambda *args: None)
        monkeypatch.setattr(subscription_static_completion, "complete_at_publication", lambda *args: None)
        monkeypatch.setattr(service.S3ArtifactStore, "configured", lambda: SimpleNamespace(source=lambda *args: b"fixture"))
        monkeypatch.setattr(service, "verified_public_files", lambda *args: [{"path": "index.html", "body": b"fixture"}])
        invoke = lambda: service.publish_accepted_build(dep_id, app_id, "brn_stage_hook", actor=actor, provider=provider)
    else:
        def public_page(*args, **kwargs):
            state.source_reads += 1
            return {"body": b"fixture", "workspace_id": workspace.id,
                "sha256": "b" * 64 if state.source_changed and state.source_reads == 3 else "a" * 64}
        monkeypatch.setattr(firstlook_publish, "public_page", public_page)
        monkeypatch.setattr(firstlook_publish, "_waitlist_receipt", lambda *args: {})
        payload = {"deployment_id": dep_id, "app_id": app_id, "venture_id": vid, "site_version_id": site_id,
            "public_sha256": "a" * 64, "actor": actor}
        invoke = lambda: firstlook_publish.publish(payload, provider=provider)
    return SimpleNamespace(invoke=invoke, provider=provider, state=state, factory=factory, vid=vid,
        dep_id=dep_id, site_id=site_id, stage=stage, events=events, Deployment=Deployment,
        SiteVersion=SiteVersion, HostingError=service.HostingError)


def test_stage_hook_observes_actual_live_commit_before_notices(publication):
    e = publication
    result = e.invoke()
    assert result["state"] == "live" and e.provider.restored == 0
    assert e.events == [("stage", (e.vid, "publication:" + e.dep_id)),
        ("hosting.publication_live", None), ("analytics", None), ("notice", None)]


@pytest.mark.parametrize("failure", ["readback", "source", "commit"])
def test_failed_or_pre_live_publication_never_reassesses_stage(publication, failure):
    e = publication
    e.provider.readback_failure = failure == "readback"
    e.state.source_changed, e.state.fail_commit = failure == "source", failure == "commit"
    with pytest.raises(e.HostingError):
        e.invoke()
    assert not any(kind == "stage" for kind, _ in e.events)
    assert e.provider.restored == 1
    with e.factory() as session:
        assert session.get(e.Deployment, e.dep_id).state == "failed"
        assert session.get(e.SiteVersion, e.site_id).published == 0


def test_stage_failure_retains_original_success_and_never_rolls_back(publication, monkeypatch, caplog):
    e = publication
    def failing_hook(*args):
        raise RuntimeError("synthetic_unlogged_hook_details")
    monkeypatch.setattr(e.stage, "after_evidence", failing_hook)
    assert e.invoke()["state"] == "live" and e.provider.restored == 0
    assert [kind for kind, _ in e.events] == ["hosting.publication_live", "analytics", "notice"]
    assert "founder_stage_publication_pending" in caplog.text
    assert "synthetic_unlogged_hook_details" not in caplog.text
    with e.factory() as session:
        assert session.get(e.Deployment, e.dep_id).state == "live"


def test_live_replay_does_not_repeat_hook_or_provider_effects(publication):
    e = publication
    first = e.invoke()
    effects, events = e.provider.effects, list(e.events)
    assert e.invoke() == first
    assert e.provider.effects == effects and e.events == events


def test_preexisting_live_read_path_is_not_a_new_evidence_event(publication):
    e = publication
    with e.factory() as session:
        row = session.get(e.Deployment, e.dep_id)
        row.state, row.url = "live", "https://stage-hook.supymem.com/"
        session.commit()
    assert e.invoke()["state"] == "live"
    assert e.events == [] and e.provider.effects == 0 and e.provider.restored == 0
