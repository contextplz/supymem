"""A firstlook concept page is published through the static-hosting pipeline, exactly and fail-closed (real PostgreSQL)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.unit.product.test_static_publication import _config

COPY = {
    "headline": "We're building a calmer way for bakeries to take orders",
    "subhead": "Early idea for small bakery owners. Join the waitlist to hear first.",
    "points": [
        {"title": "The idea", "body": "A simple order board a bakery owner can review each morning."},
        {"title": "Who it is for", "body": "We want to start with one bakery and learn what actually helps."},
        {"title": "Status", "body": "Nothing here is final. We are talking to owners before writing code."},
    ],
    "how_it_works": [{"step": 1, "text": "We plan to interview bakery owners."},
                     {"step": 2, "text": "Then we will build the smallest useful slice."}],
    "cta": {"label": "Join the waitlist", "kind": "waitlist"},
    "audience": "bakery owners",
    "name_candidates": ["Order Board", "Crumb", "Loaf"],
    "assumptions": ["Bakery owners take orders by hand today."],
}


class FakeProvider:
    """Only the AWS/network seams: object store, KVS mapping and the HTTPS readback."""

    def __init__(self, *, readback_error: str | None = None):
        self.config = _config()
        self.files, self.switches, self.restored, self.verified = [], [], [], []
        self.readback_error = readback_error

    def put_files(self, *, app_id, deployment_id, artifact_id, files):
        self.files.append((app_id, deployment_id, artifact_id, files))

    def switch(self, hostname, mapping, **kwargs):
        self.switches.append((hostname, mapping, kwargs))
        return None

    def verify_public(self, hostname, sha):
        from app.hosting.service import HostingError

        self.verified.append((hostname, sha))
        if self.readback_error:
            raise HostingError(self.readback_error, status=503)

    def restore(self, hostname, mapping, previous):
        self.restored.append((hostname, previous))

    def disable(self, hostname, mapping):
        self.switches.append(("disable", hostname, mapping))

    def verify_offline(self, hostname, mapping):
        self.verified.append(("offline", hostname))

    def verify_disabled_mapping(self, hostname, mapping):
        pass


@pytest.fixture()
def site(journey_db, monkeypatch):
    from app.firstlook import service as firstlook, serve
    from app.hosting.service import HostingConfig

    d = journey_db
    from tests.firstlook_legacy import seed_legacy_page

    seed_legacy_page(d["wid"], d["vid"])
    monkeypatch.setattr(HostingConfig, "from_environment", classmethod(lambda _: _config()))
    monkeypatch.setenv("PUBLIC_GATEWAY_URL", "https://gateway.supymem.com")

    async def no_code(*args, **kwargs):
        return None

    monkeypatch.setattr(firstlook, "try_code_html", no_code)
    monkeypatch.setattr(serve, "_commit_site", lambda *args, **kwargs: None)
    from app.tenancy import bind

    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        view = asyncio.run(firstlook.generate_first_look(d["wid"], "Bakery order board", venture_id=d["vid"],
                                                        reuse_copy=dict(COPY), allow_model=False))
    return {**d, "view": view}


def _verified_waitlist(d, *, age: timedelta = timedelta(0)):
    from app.conversion import waitlist
    from app.db.models import Venture
    from app.db.supymem_models import VentureWaitlist, VentureWaitlistEntry
    from core.clock import utcnow
    from core.ids import new_id

    waitlist.configure(d["vid"], d["wid"], enabled=True, actor=d["actor"])
    with d["factory"]() as session:
        row = session.get(VentureWaitlist, d["vid"])
        entry = VentureWaitlistEntry(id=new_id("wle"), venture_id=d["vid"], workspace_id=d["wid"],
                                     email=new_id("p") + "@qa.invalid", is_test=True, consent_version=waitlist.VERSION,
                                     consent_text=waitlist.consent_text(session.get(Venture, d["vid"]).name),
                                     created_at=utcnow())
        session.add(entry)
        row.verified_entry_id, row.verified_at = entry.id, utcnow() - age
        session.commit()


def _queue(d):
    from app.hosting import firstlook_publish as fp

    page = fp.public_page(d["vid"])
    out = fp.queue_publication(d["vid"], actor=d["actor"], site_version_id=page["site_version_id"],
                               public_sha256=page["sha256"])
    return page, out


def _payload(d, job_id):
    from app.db.models import Job

    with d["factory"]() as session:
        return json.loads(session.get(Job, job_id).payload_json)


def test_public_page_is_one_file_with_one_waitlist_link_and_no_form(site):
    from app.hosting import firstlook_publish as fp

    page = fp.public_page(site["vid"])
    html = page["html"]
    assert "<form" not in html.lower() and "This link only works on your machine" not in html
    assert html.count("https://gateway.supymem.com/public/waitlists/" + site["vid"]) == 1
    assert hashlib.sha256(page["body"]).hexdigest() == page["sha256"]
    assert fp.public_page(site["vid"])["sha256"] == page["sha256"]  # deterministic: the confirmed bytes are the uploaded bytes


def test_publication_needs_a_verified_waitlist_and_creates_nothing_without_it(site):
    from app.db.supymem_models import Deployment, FounderApp
    from app.hosting import firstlook_publish as fp
    from app.hosting.service import HostingError

    page = fp.public_page(site["vid"])
    with pytest.raises(HostingError, match="hosting_conversion_verification_required"):
        fp.queue_publication(site["vid"], actor=site["actor"], site_version_id=page["site_version_id"],
                             public_sha256=page["sha256"])
    _verified_waitlist(site, age=timedelta(hours=25))  # a stale probe does not count
    with pytest.raises(HostingError, match="hosting_conversion_verification_required"):
        fp.queue_publication(site["vid"], actor=site["actor"], site_version_id=page["site_version_id"],
                             public_sha256=page["sha256"])
    with site["factory"]() as session:
        assert not session.scalars(select(Deployment).where(Deployment.venture_id == site["vid"])).all()
        assert not session.scalars(select(FounderApp).where(FounderApp.venture_id == site["vid"])).all()


def test_publish_uploads_exact_bytes_switches_reads_back_then_marks_published(site):
    from app.authz.workspace import ensure_subject_person
    from app.db.models import SiteVersion, Venture
    from app.db.supymem_models import AppDomain, Deployment, FounderApp
    from app.hosting import firstlook_publish as fp
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    from core.hosting_contracts import public_hostname
    from core.ids import new_id

    # Global addresses survive between these module-scoped PostgreSQL cases.
    # Reserve a real same-name app in another tenant before this publication,
    # so the test cannot depend on the preferred address being unoccupied.
    foreign_actor, foreign_vid, now = new_id("usr"), new_id("ven"), utcnow()
    ensure_subject_person(foreign_actor)
    foreign_workspace = create_workspace(kind="company", name="Prior address owner", owner_id=foreign_actor)
    with site["factory"]() as session:
        name = session.get(Venture, site["vid"]).name
        session.add(Venture(id=foreign_vid, workspace_id=foreign_workspace.id, name=name, raw_idea=name,
                            stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
        session.commit()
    foreign_app_id = fp._ensure_app(foreign_vid, foreign_workspace.id, foreign_actor)
    with site["factory"]() as session:
        foreign_slug = session.get(FounderApp, foreign_app_id).slug

    _verified_waitlist(site)
    page, queued = _queue(site)
    again = fp.queue_publication(site["vid"], actor=site["actor"], site_version_id=page["site_version_id"],
                                 public_sha256=page["sha256"])
    assert again["id"] == queued["id"] and again["job_id"] == queued["job_id"]  # one durable intent
    with site["factory"]() as session:
        assert session.get(SiteVersion, page["site_version_id"]).published == 0
        app = session.scalars(select(FounderApp).where(FounderApp.venture_id == site["vid"])).one()
        assert app.build_mode == "firstlook" and app.slug != foreign_slug
        assert app.slug == "order-board" or app.slug.startswith("order-board-")
        assert (app.workspace_id, app.venture_id) == (site["wid"], site["vid"])
        hostname = public_hostname(app.slug, _config().public_domain)
        public_url = f"https://{hostname}/"
        assert session.scalars(select(Deployment).where(Deployment.venture_id == site["vid"])).one().state == "pending"
    provider = FakeProvider()
    result = fp.publish(_payload(site, queued["job_id"]), provider=provider)
    assert result["state"] == "live" and result["url"] == public_url
    (_, dep_id, _, files), = provider.files
    assert [f["path"] for f in files] == ["index.html"] and files[0]["body"] == page["body"]
    assert hashlib.sha256(files[0]["body"]).hexdigest() == page["sha256"]
    assert provider.verified == [(hostname, page["sha256"])]  # HTTPS readback of the exact hash
    assert provider.switches[0][0] == hostname and provider.switches[0][1]["deployment"] == dep_id
    with site["factory"]() as session:
        assert session.get(SiteVersion, page["site_version_id"]).published == 1
        assert session.scalars(select(AppDomain).where(AppDomain.workspace_id == site["wid"])).one().state == "active"
    assert fp.describe(site["vid"])["state"] == "live"
    # The same evidence the Website page and outreach use now sees a live page.
    from app.planning.publication_evidence import public_site_evidence

    with site["factory"]() as session:
        evidence = public_site_evidence(session, workspace_id=site["wid"], venture_id=site["vid"])
    assert evidence and evidence["url"] == public_url


def test_failed_readback_restores_the_mapping_and_never_marks_the_version_published(site):
    from app.db.models import SiteVersion
    from app.db.supymem_models import Deployment
    from app.hosting import firstlook_publish as fp
    from app.hosting.service import HostingError

    _verified_waitlist(site)
    page, queued = _queue(site)
    provider = FakeProvider(readback_error="hosting_public_health_mismatch")
    with pytest.raises(HostingError, match="hosting_public_health_mismatch"):
        fp.publish(_payload(site, queued["job_id"]), provider=provider)
    assert provider.restored  # the previous (absent) mapping is put back
    with site["factory"]() as session:
        dep = session.scalars(select(Deployment).where(Deployment.venture_id == site["vid"])).one()
        assert (dep.state, dep.error) == ("failed", "hosting_public_health_mismatch")
        assert session.get(SiteVersion, page["site_version_id"]).published == 0


@pytest.mark.parametrize("uncertain_at", ["switch", "restore"])
def test_uncertain_public_state_blocks_confirmation_and_worker_replay(site, monkeypatch, uncertain_at):
    from app.db.models import Job, SiteVersion
    from app.db.supymem_models import Deployment
    from app.firstlook import versions
    from app.hosting import firstlook_publish as fp
    from app.hosting.service import HostingError

    _verified_waitlist(site)
    page, queued = _queue(site)
    payload = _payload(site, queued["job_id"])
    provider = FakeProvider(readback_error="hosting_public_health_mismatch" if uncertain_at == "restore" else None)
    if uncertain_at == "switch":
        original_switch = provider.switch

        def lost_switch_response(*args, **kwargs):
            original_switch(*args, **kwargs)
            raise HostingError("hosting_mapping_write_failed", status=503)

        monkeypatch.setattr(provider, "switch", lost_switch_response)
    else:
        def restore_failed(*args, **kwargs):
            raise RuntimeError("mapping restoration could not be confirmed")

        monkeypatch.setattr(provider, "restore", restore_failed)

    with pytest.raises(HostingError, match="hosting_public_state_uncertain"):
        fp.publish(payload, provider=provider)
    state = fp.describe(site["vid"])
    assert state["state"] == "blocked"
    assert any(b["code"] == "hosting_public_state_uncertain" for b in state["blockers"])

    # Neither the same confirmation nor an automatic worker retry may clear the hold.
    with pytest.raises(HostingError, match="hosting_public_state_uncertain"):
        fp.queue_publication(site["vid"], actor=site["actor"], site_version_id=page["site_version_id"],
                             public_sha256=page["sha256"])
    retry_provider = FakeProvider()
    with pytest.raises(HostingError, match="hosting_public_state_uncertain"):
        fp.publish(payload, provider=retry_provider)
    assert not retry_provider.files and not retry_provider.switches and not retry_provider.verified

    # Changing the draft does not authorize overwriting an uncertain public mapping either.
    versions.patch_copy(site["wid"], site["view"].version,
                        {**COPY, "headline": "We're building a simpler order board for bakeries"})
    fresh = fp.public_page(site["vid"])
    with pytest.raises(HostingError, match="hosting_public_state_uncertain"):
        fp.queue_publication(site["vid"], actor=site["actor"], site_version_id=fresh["site_version_id"],
                             public_sha256=fresh["sha256"])
    with site["factory"]() as session:
        dep = session.get(Deployment, queued["id"])
        assert (dep.state, dep.error) == ("failed", "hosting_public_state_uncertain")
        assert session.get(SiteVersion, page["site_version_id"]).published == 0
        assert len(session.scalars(select(Job).where(Job.venture_id == site["vid"], Job.kind == "deploy.run")).all()) == 1


def test_a_draft_changed_after_confirmation_is_refused_before_any_upload(site):
    from app.firstlook import versions
    from app.hosting import firstlook_publish as fp
    from app.hosting.service import HostingError

    _verified_waitlist(site)
    page, queued = _queue(site)
    changed = {**COPY, "headline": "We're building a different thing for bakeries"}
    versions.patch_copy(site["wid"], site["view"].version, changed)
    provider = FakeProvider()
    with pytest.raises(HostingError, match="hosting_firstlook_draft_changed"):
        fp.publish(_payload(site, queued["job_id"]), provider=provider)
    assert not provider.files and not provider.switches
    with pytest.raises(HostingError, match="hosting_firstlook_draft_changed"):
        fp.queue_publication(site["vid"], actor=site["actor"], site_version_id=page["site_version_id"],
                             public_sha256=page["sha256"])


def test_the_waitlist_test_expiring_before_the_switch_stops_the_publication(site):
    from app.hosting import firstlook_publish as fp
    from app.hosting.service import HostingError

    _verified_waitlist(site)
    _, queued = _queue(site)
    _verified_waitlist(site, age=timedelta(hours=25))
    provider = FakeProvider()
    with pytest.raises(HostingError, match="hosting_conversion_verification_required"):
        fp.publish(_payload(site, queued["job_id"]), provider=provider)
    assert not provider.files and not provider.switches


def test_copy_that_claims_unbuilt_features_cannot_be_published(site):
    from app.firstlook import versions
    from app.hosting import firstlook_publish as fp
    from app.hosting.service import HostingError

    versions.patch_copy(site["wid"], site["view"].version,
                        {**COPY, "subhead": "Our app automatically sends reminders to members."})
    with pytest.raises(HostingError, match="hosting_firstlook_copy_rejected"):
        fp.public_page(site["vid"])
    versions.patch_copy(site["wid"], site["view"].version,
                        {**COPY, "points": [{**COPY["points"][0], "body": "TODO replace this placeholder text"}, *COPY["points"][1:]]})
    with pytest.raises(HostingError, match="hosting_public_copy_unreviewed"):
        fp.public_page(site["vid"])


def test_republishing_a_newer_version_supersedes_and_a_takedown_works(site):
    from app.db.models import SiteVersion
    from app.db.supymem_models import Deployment
    from app.firstlook import service as firstlook
    from app.hosting import firstlook_publish as fp, unpublish

    _verified_waitlist(site)
    _, first = _queue(site)
    provider = FakeProvider()
    fp.publish(_payload(site, first["job_id"]), provider=provider)
    from app.tenancy import bind

    with bind(workspace_id=site["wid"], venture_id=site["vid"], actor=site["actor"]):
        newer = asyncio.run(firstlook.generate_first_look(site["wid"], "Bakery order board", venture_id=site["vid"],
                                                          reuse_copy={**COPY, "headline": "We're building order boards for bakeries"},
                                                          allow_model=False))
    page, second = _queue(site)
    assert page["site_version_id"] == newer.id and second["id"] != first["id"]
    provider = FakeProvider()
    live = fp.publish(_payload(site, second["job_id"]), provider=provider)
    assert provider.switches[0][2]["expected_previous_deployment_id"] == first["id"]
    with site["factory"]() as session:
        assert session.get(Deployment, first["id"]).state == "superseded" and session.get(Deployment, live["id"]).state == "live"
        app_id = session.get(Deployment, live["id"]).app_id
    receipt = unpublish.queue(app_id, live["id"], actor=site["actor"])
    payload = _payload(site, receipt["unpublish"]["job_id"])
    unpublish.execute(payload, provider=FakeProvider())
    with site["factory"]() as session:
        assert session.get(Deployment, live["id"]).state == "unpublished"
        assert session.get(SiteVersion, newer.id).published == 0


def test_the_job_handler_routes_firstlook_payloads(site, monkeypatch):
    from app.hosting import firstlook_publish as fp
    from app.worker import handlers

    seen = []
    monkeypatch.setattr(fp, "publish", lambda payload: seen.append(payload["site_version_id"]))
    handlers._deploy_run({"schema_version": 1, "kind": "firstlook", "site_version_id": "sv_1"})
    assert seen == ["sv_1"]


def test_chat_offers_a_confirm_card_bound_to_the_page_and_the_executor_queues_it_once(site):
    from app.chat import site_actions
    from app.db.models import Job

    blocked = site_actions.route(site["vid"], "site.publish", "publish my landing page")
    assert blocked["kind"] == "needs_you" and "waitlist" in blocked["body_md"].lower()
    _verified_waitlist(site)
    reply = site_actions.route(site["vid"], "site.publish", "publish my landing page")
    (card,) = reply["actions"]
    state = card["confirmation"]["resulting_state"]
    assert reply["kind"] == "confirmation" and state["mode"] == "firstlook" and len(state["public_sha256"]) == 64
    first = asyncio.run(site_actions.execute(site["vid"], "site.publish", state, site["actor"]))
    second = asyncio.run(site_actions.execute(site["vid"], "site.publish", state, site["actor"]))
    assert "queued" in first["body_md"] and "not live yet" in first["body_md"] and "queued" in second["body_md"]
    with site["factory"]() as session:
        jobs = session.scalars(select(Job).where(Job.kind == "deploy.run", Job.venture_id == site["vid"])).all()
    assert len(jobs) == 1
    stale = asyncio.run(site_actions.execute(site["vid"], "site.publish", {**state, "public_sha256": "0" * 64}, site["actor"]))
    assert "changed after you reviewed" in stale["body_md"] and "Review the latest landing page and confirm again" in stale["body_md"]
    assert "queued" not in stale["body_md"] and stale["actions"] == []
    from app.hosting import firstlook_publish as fp

    current = fp.public_page(site["vid"])
    assert (current["site_version_id"], current["sha256"]) == (state["site_version_id"], state["public_sha256"])
    with site["factory"]() as session:
        after = session.scalars(select(Job).where(Job.kind == "deploy.run", Job.venture_id == site["vid"])).all()
    assert [row.id for row in after] == [row.id for row in jobs]


def test_concurrent_confirmations_share_one_publication_intent(site):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.db.supymem_models import Deployment, FounderApp
    from app.hosting import firstlook_publish as fp

    _verified_waitlist(site)
    page = fp.public_page(site["vid"])
    barrier = Barrier(4)

    def contender(_):
        barrier.wait(timeout=10)
        return fp.queue_publication(site["vid"], actor=site["actor"], site_version_id=page["site_version_id"],
                                    public_sha256=page["sha256"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(contender, range(4)))
    assert len({r["id"] for r in results}) == 1 and len({r["job_id"] for r in results}) == 1
    with site["factory"]() as session:
        assert len(session.scalars(select(FounderApp).where(FounderApp.venture_id == site["vid"])).all()) == 1
        assert len(session.scalars(select(Deployment).where(Deployment.venture_id == site["vid"])).all()) == 1


@pytest.mark.parametrize("retirement", ["venture_archived", "workspace_archived", "actor_removed"])
@pytest.mark.parametrize("at", ["before_worker", "after_upload"])
def test_publication_rechecks_current_authority_before_any_public_switch(site, retirement, at):
    """A valid confirmation cannot survive retired founder/venture authority."""
    from sqlalchemy import delete

    from app.authz.workspace import ensure_subject_person
    from app.db.models import SiteVersion, Venture, Workspace, WorkspaceMember
    from app.db.supymem_models import Deployment
    from app.hosting import firstlook_publish as fp
    from app.hosting.service import HostingError
    from core.clock import utcnow
    from core.ids import new_id

    _verified_waitlist(site)
    page, queued = _queue(site)
    payload = _payload(site, queued["job_id"])
    new_actor = new_id("usr")
    if retirement == "actor_removed":
        ensure_subject_person(new_actor)

    def retire():
        with site["factory"]() as session:
            if retirement == "venture_archived":
                session.get(Venture, site["vid"]).archived_at = utcnow()
            elif retirement == "workspace_archived":
                session.get(Workspace, site["wid"]).archived_at = utcnow()
            else:
                session.get(Workspace, site["wid"]).owner_id = new_actor
                session.execute(delete(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == site["wid"],
                    WorkspaceMember.person_id == site["actor"]))
            session.commit()

    class RetiringProvider(FakeProvider):
        def put_files(self, **kwargs):
            super().put_files(**kwargs)
            if at == "after_upload":
                retire()

    if at == "before_worker":
        retire()
    provider = RetiringProvider()
    with pytest.raises(HostingError):
        fp.publish(payload, provider=provider)
    assert len(provider.files) == (1 if at == "after_upload" else 0)
    assert not provider.switches and not provider.verified and not provider.restored
    with site["factory"]() as session:
        row = session.get(Deployment, queued["id"])
        assert row.state == "failed" and row.error
        assert row.error != "hosting_public_state_uncertain"  # refusal precedes mapping I/O
        assert session.get(SiteVersion, page["site_version_id"]).published == 0


@pytest.mark.parametrize("changed", ["draft_copy", "waitlist_disabled"])
def test_publication_rechecks_current_source_and_waitlist_after_upload(site, changed):
    from app.db.models import SiteVersion
    from app.db.supymem_models import Deployment, VentureWaitlist
    from app.firstlook import versions
    from app.hosting import firstlook_publish as fp
    from app.hosting.service import HostingError

    _verified_waitlist(site)
    page, queued = _queue(site)

    class ChangingProvider(FakeProvider):
        def put_files(self, **kwargs):
            super().put_files(**kwargs)
            if changed == "draft_copy":
                versions.patch_copy(site["wid"], site["view"].version,
                                    {**COPY, "headline": "We're building a different bakery order board"})
            else:
                with site["factory"]() as session:
                    session.get(VentureWaitlist, site["vid"]).enabled = False
                    session.commit()

    provider = ChangingProvider()
    with pytest.raises(HostingError):
        fp.publish(_payload(site, queued["job_id"]), provider=provider)
    assert len(provider.files) == 1
    assert not provider.switches and not provider.verified and not provider.restored
    with site["factory"]() as session:
        row = session.get(Deployment, queued["id"])
        assert row.state == "failed" and row.error
        assert row.error != "hosting_public_state_uncertain"
        assert session.get(SiteVersion, page["site_version_id"]).published == 0


def test_first_publication_refuses_an_unrecorded_public_mapping(site):
    """A free database slug cannot authorize replacing an unknown KVS owner."""
    from app.db.models import SiteVersion
    from app.db.supymem_models import Deployment
    from app.hosting import firstlook_publish as fp
    from app.hosting.service import AwsStaticPublisher, HostingError
    from core.artifact_snapshot import canonical
    from core.hosting_contracts import host_mapping

    _verified_waitlist(site)
    page, queued = _queue(site)
    foreign = host_mapping(app_id="app_foreign00", deployment_id="dep_foreign00", generation=1, enabled=True)

    class Kvs:
        value = canonical(foreign).decode()
        writes = 0

        def describe_key_value_store(self, **kwargs):
            return {"ETag": "1", "TotalSizeInBytes": 400}

        def get_key(self, **kwargs):
            return {"Value": self.value}

        def put_key(self, *, Value, **kwargs):
            self.value = Value
            self.writes += 1

    kvs = Kvs()

    class BaselineProvider(FakeProvider):
        def switch(self, hostname, mapping, **kwargs):
            self.switches.append((hostname, mapping, kwargs))
            return AwsStaticPublisher(self.config, None, kvs).switch(hostname, mapping, **kwargs)

    provider = BaselineProvider()
    with pytest.raises(HostingError, match="^subscription_static_publication_baseline_changed$"):
        fp.publish(_payload(site, queued["job_id"]), provider=provider)
    assert len(provider.files) == 1 and len(provider.switches) == 1
    assert provider.switches[0][2].get("expected_absent") is True
    assert kvs.writes == 0 and kvs.value == canonical(foreign).decode()
    assert not provider.verified and not provider.restored
    with site["factory"]() as session:
        row = session.get(Deployment, queued["id"])
        assert (row.state, row.error) == ("failed", "subscription_static_publication_baseline_changed")
        assert session.get(SiteVersion, page["site_version_id"]).published == 0


def test_normal_takedown_retires_only_its_owned_public_predecessor_chain(site):
    from app.api.firstlook_api import serve_site_html
    from app.db.models import SiteVersion, Venture
    from app.firstlook.service import public_site
    from app.planning.publication_evidence import public_site_evidence
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    from core.ids import new_id

    foreign_workspace = create_workspace(kind="company", name="Unrelated site", owner_id=site["actor"])
    foreign_venture, foreign_source = new_id("ven"), new_id("site")
    with site["factory"]() as session:
        original = session.get(SiteVersion, site["view"].id)
        values = {column.name: getattr(original, column.name) for column in SiteVersion.__table__.columns}
        values.update(id=foreign_source, workspace_id=foreign_workspace.id, venture_id=foreign_venture,
                      published=1, url="https://unrelated-site.example.invalid/", version=1)
        session.add(Venture(id=foreign_venture, workspace_id=foreign_workspace.id, name="Unrelated site",
                            raw_idea="Unrelated owned publication", stage="draft", autonomy_level="L1",
                            created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(SiteVersion(**values))
        session.commit()

    # Use the existing normal two-version publish and exact-deployment takedown.
    test_republishing_a_newer_version_supersedes_and_a_takedown_works(site)
    with site["factory"]() as session:
        owned = session.scalars(select(SiteVersion).where(
            SiteVersion.workspace_id == site["wid"], SiteVersion.venture_id == site["vid"])).all()
        assert len([row for row in owned if row.template != "legacy_fixture"]) == 2
        assert all(row.published == 0 for row in owned)
        assert session.get(SiteVersion, foreign_source).published == 1
        assert public_site_evidence(session, workspace_id=site["wid"], venture_id=site["vid"]) is None
    assert public_site(site["wid"]) is None
    assert serve_site_html(site["wid"], v=None).status_code == 404
    assert serve_site_html(site["wid"], v=site["view"].version).status_code == 404
