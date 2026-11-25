"""Independent source authority mutations; providers and browser reports are synthetic."""
import pytest
import uuid
from sqlalchemy import select
from tests.integration.test_postgres_subscription_static import (
    pg_database as pg_database, journey_db as journey_db,
    planning_standard as planning_standard, planning as planning,
    ready_plan as ready_plan, source_ready as source_ready,
)
from tests.integration.test_postgres_subscription_static import source_cloud as source_cloud, source_step
from tests.integration.test_postgres_static_publication import (
    configured_publication_before_plan as configured_publication_before_plan,
    _verified_waitlist,
)



@pytest.mark.parametrize('field', ['workspace_id', 'venture_id', 'app_id', 'actor_id', 'device_id'])
def test_foreign_custody_after_quote_cannot_allocate_or_launch(source_ready, field):
    from app.build import subscription_static_admission as admission
    from app.db.supymem_models import CodingSourceReceipt, BuildRun, QaRun, FounderApp
    from app.db.models import CreditReservation, Venture
    d = source_ready
    quote = admission.estimate(d['vid'], d['order_id'], d['caller'])
    with d['factory']() as session:
        source = session.get(CodingSourceReceipt, d['order_id'])
        foreign = 'foreign_' + uuid.uuid4().hex
        if field in {'venture_id', 'app_id'}:
            original = session.get(Venture, d['vid'])
            values = {c.name: getattr(original, c.name) for c in Venture.__table__.columns if c.name != 'id'}
            session.add(Venture(id=foreign, **values))
            session.flush()
        if field == 'app_id':
            original = session.get(FounderApp, d['app_id'])
            values = {c.name: getattr(original, c.name) for c in FounderApp.__table__.columns if c.name not in {'id', 'slug'}}
            values['venture_id'] = foreign
            session.add(FounderApp(id=foreign, slug='foreign-counterexample', **values))
            session.flush()
        setattr(source, field, foreign)
        session.commit()
    with pytest.raises(ValueError, match='subscription_static_source_scope_mismatch'):
        admission.admit(d['vid'], d['order_id'], d['caller'],
                        quote_id=quote['quote_id'], quote_digest=quote['quote_digest'])
    with d['factory']() as session:
        assert not session.scalar(select(BuildRun.id).where(BuildRun.app_id == d['app_id']))
        assert not session.scalar(select(QaRun.id).where(QaRun.app_id == d['app_id']))
        # PG shards retain unrelated earlier fixture receipts. This exact
        # quote fingerprint binds both reservations allocated by admission.
        fingerprint = quote['source_fingerprint']
        assert session.get(CreditReservation, 'crv_' + fingerprint[:48]) is None
        assert session.get(CreditReservation, 'crv_qa_' + fingerprint[:44]) is None




@pytest.mark.parametrize('rollback_fails', [False, True, 'transport_timeout'])
@pytest.mark.parametrize('changed_authority', ['contract', 'conversion'])
def test_contract_drift_during_public_readback_cannot_finalize_live(source_cloud, rollback_fails, changed_authority):
    import json
    from app.builder.execution import read, review
    from app.db.supymem_models import ExecutionPlan, Deployment, FounderApp, VentureWaitlist
    from app.hosting.service import HostingConfig, queue_accepted_build, publish_accepted_build, _accepted_source, HostingError
    d = source_cloud
    source_step(d)
    d['qa_state']['phase'] = 'completed'
    source_step(d)
    assert source_step(d)['state'] == 'succeeded'
    evidence = read(d['build_run_id'], actor=d['actor'])
    review(d['build_run_id'], actor=d['actor'], source_artifact_id=evidence['artifact_id'],
           report_digest=evidence['review']['report_digest'], decision='accept')
    _verified_waitlist(d)
    queued = queue_accepted_build(d['app_id'], d['build_run_id'], actor=d['actor'])
    effects = []
    predecessor = {'previous': 'reviewed mapping'}

    class Provider:
        config = HostingConfig.from_environment()
        def put_files(self, **_kwargs):
            pass
        def switch(self, _hostname, _mapping, **_kwargs):
            effects.append('switch')
            return predecessor
        def restore(self, _hostname, _mapping, previous):
            assert previous == predecessor
            effects.append('restore')
            if rollback_fails == 'transport_timeout':
                raise TimeoutError('private provider transport detail')
            if rollback_fails:
                raise HostingError('hosting_mapping_restore_failed')
        def verify_public(self, _hostname, _expected):
            with d['factory']() as session:
                if changed_authority == 'contract':
                    plan = session.get(ExecutionPlan, d['plan_id'])
                    structured = json.loads(plan.structured)
                    structured['proposal']['milestones'][0]['tasks'][0]['landing_expectations']['contract']['product_name'] = 'Changed current reviewed contract'
                    plan.structured = json.dumps(structured)
                else:
                    session.get(VentureWaitlist, d['vid']).enabled = False
                session.commit()

    refusal = 'hosting_build_not_accepted' if changed_authority == 'contract' else 'hosting_conversion_verification_required'
    expected_error = 'hosting_public_state_uncertain' if rollback_fails else refusal
    with pytest.raises(HostingError, match=expected_error):
        publish_accepted_build(queued['id'], d['app_id'], d['build_run_id'],
                               actor=d['actor'], provider=Provider())
    assert effects == ['switch', 'restore']
    with d['factory']() as session:
        if changed_authority == 'contract':
            with pytest.raises(HostingError, match='hosting_build_not_accepted'):
                _accepted_source(session, session.get(FounderApp, d['app_id']), d['build_run_id'])
        deployment = session.get(Deployment, queued['id'])
        assert deployment.state == 'failed'
        assert deployment.error == expected_error
        assert deployment.live_at is None
