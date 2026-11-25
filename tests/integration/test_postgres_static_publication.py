"""Accepted static publication is durable, exact and retry-safe on PostgreSQL."""

from __future__ import annotations

import pytest
import json
from datetime import timedelta
from sqlalchemy import select

from tests.integration.test_postgres_managed_lifecycle import (
    journey_db,  # noqa: F401
    lifecycle_run,  # noqa: F401
    pg_database,  # noqa: F401
    planning,  # noqa: F401
    ready_plan,  # noqa: F401
    retire,
    step,
)


def _accept(d):
    from app.builder.execution import read, review

    retire(d)
    step(d)
    d["qa_state"]["phase"] = "completed"
    step(d)
    completed = step(d)
    assert completed["closed"] and completed["state"] == "succeeded"
    view = read(d["build_run_id"], actor=d["actor"])
    review(
        d["build_run_id"],
        actor=d["actor"],
        source_artifact_id=view["artifact_id"],
        report_digest=view["review"]["report_digest"],
        decision="accept",
    )
    return d


def _hosting_environment(monkeypatch):
    values = {
        "PUBLIC_SITE_S3_BUCKET": "selfhost-prod-sites",
        "PUBLIC_SITE_AWS_REGION": "us-east-1",
        "PUBLIC_SITE_S3_EXPECTED_OWNER": "123456789012",
        "PUBLIC_SITE_KVS_ARN": "arn:aws:cloudfront::123456789012:key-value-store/kvs_test",
        "PUBLIC_SITE_DOMAIN": "supymem.com",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(autouse=True)
def configured_publication_before_plan(monkeypatch):
    # Publishing availability is now part of the reviewed plan dependency.
    # Set it before planning/build admission, as on an unchanged production host.
    _hosting_environment(monkeypatch)


def _verified_waitlist(d):
    """Storage fixture only. HTTP route/probe tests exercise the network boundary separately."""
    from app.conversion import waitlist
    from app.db.models import Venture
    from app.db.supymem_models import VentureWaitlist, VentureWaitlistEntry
    from core.clock import utcnow
    from core.ids import new_id
    waitlist.configure(d["vid"], d["wid"], enabled=True, actor=d["actor"])
    with d["factory"]() as session:
        row=session.get(VentureWaitlist,d["vid"])
        entry=VentureWaitlistEntry(id=new_id('wle'),venture_id=d['vid'],workspace_id=d['wid'],
            email='publication-probe@qa.invalid',is_test=True,consent_version=waitlist.VERSION,
            consent_text=waitlist.consent_text(session.get(Venture,d['vid']).name),created_at=utcnow())
        session.add(entry)
        row.verified_entry_id,row.verified_at=entry.id,utcnow()
        session.commit()


def test_only_accepted_exact_build_queues_once(lifecycle_run, monkeypatch):  # noqa: F811
    from app.db.models import Job
    from app.db.supymem_models import AppDomain, Deployment
    from app.hosting.service import HostingError, queue_accepted_build

    d = lifecycle_run
    _hosting_environment(monkeypatch)
    try:
        queue_accepted_build(d["app_id"], d["build_run_id"], actor=d["actor"])
        raise AssertionError("unaccepted build was publishable")
    except HostingError as exc:
        assert exc.code == "hosting_build_not_accepted"

    _accept(d)
    with pytest.raises(HostingError, match="hosting_conversion_verification_required"):
        queue_accepted_build(d["app_id"], d["build_run_id"], actor=d["actor"])
    _verified_waitlist(d)
    first = queue_accepted_build(d["app_id"], d["build_run_id"], actor=d["actor"])
    second = queue_accepted_build(d["app_id"], d["build_run_id"], actor=d["actor"])
    assert first["id"] == second["id"]
    assert first["job_id"] == second["job_id"]

    with d["factory"]() as session:
        deployments = session.scalars(
            select(Deployment).where(Deployment.app_id == d["app_id"])
        ).all()
        domains = session.scalars(
            select(AppDomain).where(AppDomain.app_id == d["app_id"])
        ).all()
        jobs = session.scalars(
            select(Job).where(Job.dedupe_key == "static-publish:" + first["id"])
        ).all()
    assert len(deployments) == len(domains) == len(jobs) == 1
    assert domains[0].hostname.endswith(".supymem.com")


@pytest.mark.parametrize('fault,code', [
    ('stale','hosting_conversion_verification_required'),
    ('other_venture','hosting_conversion_link_required'),
    ('changed_contract','hosting_conversion_contract_unverified'),
])
def test_expired_or_changed_conversion_cannot_queue(lifecycle_run, monkeypatch, fault, code):
    from app.db.supymem_models import VentureWaitlist, BuildRun, QaRun, Deployment
    from app.hosting.service import queue_accepted_build, HostingError
    from core.clock import utcnow
    from core.static_qa_contracts import Contract
    d=_accept(lifecycle_run)
    _hosting_environment(monkeypatch)
    _verified_waitlist(d)
    with d['factory']() as session:
        if fault=='stale':
            session.get(VentureWaitlist,d['vid']).verified_at=utcnow()-timedelta(hours=25)
        else:
            run=session.get(BuildRun,d['build_run_id']);spec=json.loads(run.spec)
            contract=spec['inputs']['task']['landing_expectations']['contract']
            contract['actions'][0]['target']='https://gateway.supymem.com/public/waitlists/foreign'
            run.spec=json.dumps(spec)
            if fault=='other_venture':
                session.get(QaRun,spec['lifecycle']['completion']['qa_id']).contract_digest=Contract.model_validate(contract).digest
        session.commit()
    with pytest.raises(HostingError,match=code):
        queue_accepted_build(d['app_id'],d['build_run_id'],actor=d['actor'])
    with d['factory']() as session:
        assert session.scalar(select(Deployment).where(Deployment.app_id==d['app_id'])) is None


def test_closing_waitlist_after_queue_blocks_provider_and_updates_status(lifecycle_run,monkeypatch):
    from types import SimpleNamespace
    from app.conversion import waitlist
    from app.db.supymem_models import Deployment
    from app.hosting.service import queue_accepted_build,publish_accepted_build,HostingConfig,HostingError
    d=_accept(lifecycle_run)
    _hosting_environment(monkeypatch)
    _verified_waitlist(d)
    queued=queue_accepted_build(d['app_id'],d['build_run_id'],actor=d['actor'])
    waitlist.configure(d['vid'],d['wid'],enabled=False,actor=d['actor'])
    provider=SimpleNamespace(config=HostingConfig.from_environment()) # Any provider effect would fail the test.
    with pytest.raises(HostingError,match='hosting_conversion_verification_required'):
        publish_accepted_build(queued['id'],d['app_id'],d['build_run_id'],actor=d['actor'],provider=provider)
    with d['factory']() as session:
        row=session.get(Deployment,queued['id'])
        assert row.state=='failed' and row.error=='hosting_conversion_verification_required'


def test_worker_publishes_and_retry_returns_same_live_receipt(
    lifecycle_run,  # noqa: F811
    monkeypatch,
):
    from app.db.supymem_models import Deployment
    from app.hosting.service import (
        HostingConfig,
        publish_accepted_build,
        queue_accepted_build,
    )

    d = _accept(lifecycle_run)
    _hosting_environment(monkeypatch)
    _verified_waitlist(d)
    queued = queue_accepted_build(d["app_id"], d["build_run_id"], actor=d["actor"])

    class Provider:
        config = HostingConfig.from_environment()

        def __init__(self):
            self.puts = []
            self.switches = []
            self.health = []

        def put_files(self, **kwargs):
            self.puts.append(kwargs)

        def switch(self, hostname, mapping):
            self.switches.append((hostname, mapping))

        def verify_public(self, hostname, expected_sha256):
            self.health.append((hostname, expected_sha256))

        def restore(self, *_args):
            raise AssertionError("successful publication must not roll back")

    provider = Provider()
    first = publish_accepted_build(
        queued["id"],
        d["app_id"],
        d["build_run_id"],
        actor=d["actor"],
        provider=provider,
    )
    repeat = publish_accepted_build(
        queued["id"],
        d["app_id"],
        d["build_run_id"],
        actor=d["actor"],
        provider=provider,
    )

    assert first == repeat and first["state"] == "live"
    assert first["url"].startswith("https://")
    assert len(provider.puts) == len(provider.switches) == len(provider.health) == 1
    assert provider.puts[0]["files"][-1]["path"] == "index.html"
    with d["factory"]() as session:
        row = session.get(Deployment, queued["id"])
        assert row.state == "live" and row.error is None and row.live_at


def test_lost_switch_ack_is_uncertain_and_reuses_same_durable_intent(lifecycle_run, monkeypatch):
    from app.db.supymem_models import Deployment
    from app.hosting.service import HostingConfig, HostingError, publish_accepted_build, queue_accepted_build
    d = _accept(lifecycle_run)
    _hosting_environment(monkeypatch)
    _verified_waitlist(d)
    queued = queue_accepted_build(d['app_id'], d['build_run_id'], actor=d['actor'])
    class Provider:
        config = HostingConfig.from_environment()
        calls = 0
        def put_files(self, **_):
            pass
        def switch(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise HostingError('hosting_mapping_write_failed', status=503)
            return None
        def verify_public(self, *_args):
            pass
        def restore(self, *_args):
            pytest.fail('Unknown predecessor must not be guessed for rollback')
    provider = Provider()
    with pytest.raises(HostingError, match='^hosting_public_state_uncertain$'):
        publish_accepted_build(queued['id'], d['app_id'], d['build_run_id'], actor=d['actor'], provider=provider)
    with d['factory']() as session:
        failed = session.get(Deployment, queued['id'])
        assert failed.state == 'failed' and failed.error == 'hosting_public_state_uncertain'
    retry = queue_accepted_build(d['app_id'], d['build_run_id'], actor=d['actor'])
    assert retry['id'] == queued['id']
    finished = publish_accepted_build(retry['id'], d['app_id'], d['build_run_id'], actor=d['actor'], provider=provider)
    assert finished['id'] == queued['id'] and finished['state'] == 'live'
    with d['factory']() as session:
        assert len(session.scalars(select(Deployment).where(Deployment.app_id == d['app_id'])).all()) == 1
