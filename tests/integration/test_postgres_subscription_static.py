"""Real migrated DB admission; subscription source and cloud providers are fixtures."""
import copy
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.integration.test_postgres_auth_security import pg_database as pg_database, concurrent
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_planning import planning as planning_standard  # noqa: F401 - pytest fixture dependency
from tests.integration.test_postgres_build_intents import ready_plan as ready_plan
from tests.lifecycle_fixtures import runtime
from tests.qa_storage_fixtures import CONTRACT, MemoryS3


@pytest.fixture()
def planning(planning_standard, monkeypatch):  # noqa: F811 - pytest resolves the imported fixture by name
    from tests.planning_fixtures import install_planner, proposal
    def native(context, _options):
        authored = proposal(context)
        task = authored['milestones'][0]['tasks'][0]
        task['capability_id'] = 'coding.subscription_static'
        contract = copy.deepcopy(CONTRACT)
        actions = task['landing_expectations']['contract']['actions']
        contract['actions'] = actions
        task['landing_expectations'] = {'contract': contract, 'criteria': [
            {'criterion': i, 'summary': 'Exact static fixture evidence', 'text_indices': [0],
             'action_ids': [item['id'] for item in actions]} for i in range(len(task['acceptance']))]}
        return authored
    install_planner(monkeypatch, native)
    return planning_standard


@pytest.fixture()
def source_ready(ready_plan, monkeypatch, tmp_path):
    from app.build import device_orders as device
    from app.builder.artifact_store import S3ArtifactStore, ArtifactStoreConfig
    from app.db.models import VentureDocument
    from app.db.supymem_models import CodingSourceReceipt
    from app.credits._txn import naive_utc
    from core.artifact_snapshot import freeze_source, manifest_digest
    d = ready_plan
    caller = SimpleNamespace(sub=d['actor'], role='user', legacy=False, device_id='dev_static_qa')
    fixture = runtime()
    monkeypatch.setenv('BUILD_QA_FARGATE_PROFILE', json.dumps(fixture['qa_profile']))
    monkeypatch.setenv('BUILD_FARGATE_TASK_PRICING', json.dumps(fixture['task_pricing']))
    monkeypatch.setenv('COMPANY_OS_VAULT_SECRET', 'source-static-qa-isolated-test-vault-thirty-two')
    monkeypatch.setattr('app.builder.budgets.runtime_quote', lambda *args, **kwargs:
                        pytest.fail('paid builder quote must not be called'))
    monkeypatch.setattr('app.planning.resources.resolve_llm_config', lambda *args, **kwargs:
                        pytest.fail('source QA must not resolve a paid model configuration'))
    config = ArtifactStoreConfig('test-artifacts', 'managed-builds',
        'arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111',
        'us-east-1', '123456789012')
    s3 = MemoryS3()
    store = S3ArtifactStore(config, s3)
    monkeypatch.setattr(S3ArtifactStore, 'configured', lambda: store)
    root = tmp_path / 'source'
    root.mkdir()
    from app.conversion.waitlist import public_url
    (root / 'index.html').write_text(f'<!doctype html><html><body><h1>Acorn</h1><p>Reading lists</p>'
        f'<a href="{public_url(d["vid"])}">Request early access</a></body></html>')
    (root / '.supymem-venture').write_text(d['vid'])
    (root / 'CODING_CARD.md').write_text('Private reviewed coding card fixture')
    review = root / 'review'
    review.mkdir()
    for name, value in {'MAC_CODING_TASK.md': 'Private task fixture',
            'browser-contract.json': '{"private":"contract"}',
            'check_local.py': 'raise RuntimeError("must never execute")',
            'check_source.cjs': 'throw new Error("must never execute")',
            'source-checks.json': '{"private":"checks"}'}.items():
        (review / name).write_text(value)
    manifest, blob = freeze_source(root)
    sha, md = hashlib.sha256(blob).hexdigest(), manifest_digest(manifest)
    suffix = uuid.uuid4().hex
    oid, eid = 'dwo_static_' + suffix, 'dex_static_' + suffix
    with d['factory']() as session:
        snapshot = device._coding_snapshot_profile(session, device._task_snapshot(
            session, d['wid'], d['vid'], d['plan_task_id']))
        now = naive_utc()
        native = {'id': oid, 'workspace_id': d['wid'], 'venture_id': d['vid'], 'actor_id': d['actor'],
            'device_id': caller.device_id, 'state': 'stopped', 'claimed_at': now.isoformat(),
            'closed_at': now.isoformat(), 'settlement_ledger_id': 'fixture_settlement', 'execution_id': eid,
            'native_stop': {'processes_stopped': True, 'execution_id': eid}, 'task_snapshot': snapshot,
            'manifest': {'task_id': d['plan_task_id'], 'actions': ['editor.edit', 'terminal.execute'],
                         'coding_verification': {'operation': 'workspace_edit_v1', 'provider': 'codex',
                                                 'offline_profile': 'static_html_v1'}}}
        session.add(VentureDocument(id=oid, venture_id=d['vid'], kind=device.KIND, title='Settled source fixture',
            provenance_json=json.dumps(native), created_at=now, updated_at=now))
        receipt = store.persist_coding_source(blob, {'workspace_id': d['wid'], 'venture_id': d['vid'],
            'app_id': d['app_id'], 'order_id': oid}, sha256=sha, manifest_digest=md)
        session.add(CodingSourceReceipt(order_id=oid, workspace_id=d['wid'], venture_id=d['vid'], app_id=d['app_id'],
            actor_id=d['actor'], device_id=caller.device_id, plan_task_id=d['plan_task_id'], execution_id=eid,
            provider='codex', state='stored', sha256=sha, manifest_sha256=md, size_bytes=len(blob),
            receipt_json=json.dumps(receipt), created_at=now, updated_at=now))
        session.commit()
    return {**d, 'caller': caller, 'order_id': oid, 'store': store, 's3': s3, 'fixture': fixture}


def test_concurrent_source_admission_has_only_one_qa_hold_and_no_builder(source_ready):
    from app.build import subscription_static_admission as admission
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import BuildRun, BuildModelCall
    d = source_ready
    quoted = admission.estimate(d['vid'], d['order_id'], d['caller'])
    results = concurrent(lambda: admission.admit(d['vid'], d['order_id'], d['caller'],
        quote_id=quoted['quote_id'], quote_digest=quoted['quote_digest']))
    assert len({row['id'] for row in results}) == 1
    assert all(row['verified_execution'] is False for row in results)
    with d['factory']() as session:
        rows = session.scalars(select(BuildRun).where(BuildRun.app_id == d['app_id'])).all()
        assert len(rows) == 1
        run = rows[0]
        spec = json.loads(run.spec)
        jobs = session.scalars(select(Job).where(Job.id == spec['watch_job_id'])).all()
        assert len(jobs) == 1 and jobs[0].kind == 'build.watch'
        children = session.scalars(select(CreditReservation).where(CreditReservation.parent_id == run.reservation_id)).all()
        assert len(children) == 1 and children[0].purpose == 'build.compute.qa'
        assert not session.scalar(select(BuildModelCall.id).where(BuildModelCall.build_run_id == run.id))
        assert spec['runtime']['model_ceiling_micro'] == spec['runtime']['builder_compute_ceiling_micro'] == 0
    assert admission.estimate(d['vid'], d['order_id'], d['caller'])['existing_run']['id'] == run.id


def test_quote_contract_drift_cannot_reserve(source_ready):
    from app.build import subscription_static_admission as admission
    from app.db.supymem_models import ExecutionPlan, BuildRun
    d = source_ready
    quoted = admission.estimate(d['vid'], d['order_id'], d['caller'])
    with d['factory']() as session:
        plan = session.get(ExecutionPlan, d['plan_id'])
        doc = json.loads(plan.structured)
        doc['proposal']['milestones'][0]['tasks'][0]['landing_expectations']['contract']['product_name'] = 'Changed offer'
        plan.structured = json.dumps(doc)
        session.commit()
    with pytest.raises(Exception, match='^subscription_static_approved_inputs_changed$'):
        admission.admit(d['vid'], d['order_id'], d['caller'], quote_id=quoted['quote_id'], quote_digest=quoted['quote_digest'])
    with d['factory']() as session:
        assert not session.scalar(select(BuildRun.id).where(BuildRun.app_id == d['app_id']))


def test_contract_mutation_during_storage_never_verifies_import_or_launches_qa(source_ready, monkeypatch):
    from app.build import subscription_static_admission as admission
    from app.db.supymem_models import ExecutionPlan, BuildRun, BuildArtifact, QaRun
    from tests.integration.test_postgres_fargate_lifecycle import claim
    d = source_ready
    quote = admission.estimate(d['vid'], d['order_id'], d['caller'])
    run = admission.admit(d['vid'], d['order_id'], d['caller'], quote_id=quote['quote_id'], quote_digest=quote['quote_digest'])
    _, payload = claim({**d, 'build_run_id': run['id']}, 'build.watch')
    original = d['store'].persist
    def mutate(*args, **kwargs):
        stored = original(*args, **kwargs)
        with d['factory']() as session:
            plan = session.get(ExecutionPlan, d['plan_id'])
            doc = json.loads(plan.structured)
            doc['proposal']['milestones'][0]['tasks'][0]['landing_expectations']['contract']['product_name'] = 'Mutated during upload'
            plan.structured = json.dumps(doc)
            session.commit()
        return stored
    monkeypatch.setattr(d['store'], 'persist', mutate)
    from app.builder import orchestration
    monkeypatch.setattr('app.builder.qa_lifecycle.watch', lambda *_: pytest.fail('QA must not launch after drift'))
    orchestration.watch(payload)
    with d['factory']() as session:
        row = session.get(BuildRun, run['id'])
        spec = json.loads(row.spec)
        assert row.state == 'failed' and row.closed_at
        assert spec['source_import_verified'] is False and spec['execution_verified'] is False
        assert not session.scalar(select(BuildArtifact.id).where(BuildArtifact.build_run_id == row.id))
        assert not session.scalar(select(QaRun.id).where(QaRun.build_run_id == row.id))
        assert spec['lifecycle']['completion']['verified_source_import'] is False


@pytest.fixture()
def source_cloud(source_ready, monkeypatch):
    from app.build import subscription_static_admission as admission
    from app.builder import qa_lifecycle as q
    from app.infra.fargate_qa import FargateQaProvider, QaProfile
    from tests.fargate_fixtures import FakeAWS
    from tests.lifecycle_fixtures import qa_definition
    from tests.qa_storage_fixtures import report_fixture
    d = source_ready
    quote = admission.estimate(d['vid'], d['order_id'], d['caller'])
    accepted = admission.admit(d['vid'], d['order_id'], d['caller'], quote_id=quote['quote_id'], quote_digest=quote['quote_digest'])
    profile = QaProfile.from_dict(d['fixture']['qa_profile'])
    aws = FakeAWS(profile)
    aws.definition = qa_definition(profile)
    run_task = aws.run_task
    def launch(**body):
        try:
            return run_task(**body)
        finally:
            for task in aws.tasks.values():
                task['containers'][0]['name'] = 'static-qa'
    aws.run_task = launch
    monkeypatch.setattr(q, 'configured_provider', lambda _: FargateQaProvider(profile, ecs=aws, ec2=aws))
    state = {'phase': 'running'}
    def progress(binding, request, reader, *, permit_execution):
        permit_execution()
        assert reader()
        state.update(input=request, id=binding['qa_id'])
        return {'phase': state['phase']}
    monkeypatch.setattr(q.qa_transport, 'progress', progress)
    monkeypatch.setattr(q.qa_transport, 'result', lambda binding, _: report_fixture(state, binding['build_id']))
    return {**d, 'build_run_id': accepted['id'], 'qa': aws, 'qa_state': state}


def source_step(d):
    from tests.integration.test_postgres_fargate_lifecycle import claim, saved
    from app.builder import orchestration
    _, payload = claim(d, 'build.watch')
    orchestration.watch(payload)
    return saved(d)


def test_actual_qa_only_lifecycle_closes_before_source_review(source_cloud):
    from app.builder.execution import read, review
    from app.db.supymem_models import BuildModelCall
    d = source_cloud
    value = source_step(d)
    assert len(d['qa'].launches) == 1
    assert value['spec']['source_import_verified'] is True
    d['qa_state']['phase'] = 'completed'
    source_step(d)
    result = source_step(d)
    assert result['closed'] and result['reserved'] == 0 and result['state'] == 'succeeded', result
    assert result['spent'] > 0
    evidence = read(d['build_run_id'], actor=d['actor'])
    assert evidence['verified_execution'] is False and evidence['verified_source_import'] is True
    assert evidence['review']['status'] == 'pending'
    with d['factory']() as session:
        assert not session.scalar(select(BuildModelCall.id).where(BuildModelCall.build_run_id == d['build_run_id']))
    body = dict(actor=d['actor'], source_artifact_id=evidence['artifact_id'],
                report_digest=evidence['review']['report_digest'], decision='accept')
    assert review(d['build_run_id'], **body) == review(d['build_run_id'], **body)
    assert read(d['build_run_id'], actor=d['actor'])['review']['review_origin'] == 'founder'
    with pytest.raises(Exception, match='^build_review_conflict$'):
        review(d['build_run_id'], **{**body, 'decision': 'reject', 'feedback': 'Correct the agreed content'})
    from app.hosting.service import _accepted_source
    from app.db.supymem_models import FounderApp
    with d['factory']() as session:
        selected = _accepted_source(session, session.get(FounderApp, d['app_id']), d['build_run_id'])
        assert selected[0].id == d['build_run_id']


def test_cancel_before_storage_releases_only_qa_hold(source_ready):
    from app.build import subscription_static_admission as admission
    from app.builder.execution import cancel
    d = source_ready
    quote = admission.estimate(d['vid'], d['order_id'], d['caller'])
    run = admission.admit(d['vid'], d['order_id'], d['caller'], quote_id=quote['quote_id'], quote_digest=quote['quote_digest'])
    cancel(run['id'], actor=d['actor'])
    result = source_step({**d, 'build_run_id': run['id']})
    assert result['state'] == 'cancelled' and result['closed'] and result['reserved'] == 0
    assert result['spent'] == 0 and result['spec']['source_import_verified'] is False


@pytest.mark.parametrize('first_stop', ['cancel', 'deadline'])
def test_fresh_reviewed_order_recaptures_same_source_after_closed_qa_failure(source_cloud, first_stop):
    from app.build import device_orders, subscription_static_admission as admission
    from app.builder.execution import cancel, read, review
    from app.db.models import VentureDocument
    from app.db.supymem_models import CodingSourceReceipt
    d = source_cloud
    old_id = d['build_run_id']
    with d['factory']() as session:
        with pytest.raises(ValueError, match='^device_task_not_ready$'):
            device_orders._task_snapshot(session, d['wid'], d['vid'], d['plan_task_id'], allow_failed_coding=True)
    if first_stop == 'cancel':
        cancel(old_id, actor=d['actor'])
    else:
        from datetime import timedelta
        from app.credits._txn import naive_utc
        from app.db.supymem_models import BuildRun
        with d['factory']() as session:
            original = session.get(BuildRun, old_id)
            spec = json.loads(original.spec)
            spec['lifecycle']['queue_deadline'] = (naive_utc() - timedelta(seconds=1)).isoformat()
            original.spec = json.dumps(spec)
            session.commit()
    closed = source_step(d)
    assert closed['closed'] and closed['reserved'] == 0 and not d['qa'].launches
    # Same intent remains immutable even after cleanup; a changed quote/key is
    # not authority to relaunch it.
    assert admission.estimate(d['vid'], d['order_id'], d['caller'])['existing_run']['id'] == old_id
    oid, eid = 'dwo_retry_' + uuid.uuid4().hex, 'dex_retry_' + uuid.uuid4().hex
    with d['factory']() as session:
        old = json.loads(session.get(VentureDocument, d['order_id']).provenance_json)
        snapshot = device_orders._coding_snapshot_profile(session, device_orders._task_snapshot(
            session, d['wid'], d['vid'], d['plan_task_id'], allow_failed_coding=True))
        assert snapshot['closed_failed_build_run_id'] == old_id
        native = {**old, 'id': oid, 'execution_id': eid, 'task_snapshot': snapshot,
                  'native_stop': {'processes_stopped': True, 'execution_id': eid}}
        old_row_time = session.get(VentureDocument, d['order_id']).created_at
        session.add(VentureDocument(id=oid, venture_id=d['vid'], kind=device_orders.KIND,
            title='Fresh reviewed recapture fixture', provenance_json=json.dumps(native),
            created_at=old_row_time,
            updated_at=old_row_time))
        source = session.get(CodingSourceReceipt, d['order_id'])
        scope = {key: old[key] for key in ('workspace_id', 'venture_id')}
        scope.update(app_id=d['app_id'], order_id=d['order_id'])
        blob = d['store'].read_coding_source(json.loads(source.receipt_json), scope,
            sha256=source.sha256, manifest_digest=source.manifest_sha256)
        receipt = d['store'].persist_coding_source(blob, {**scope, 'order_id': oid},
            sha256=source.sha256, manifest_digest=source.manifest_sha256)
        session.add(CodingSourceReceipt(order_id=oid, workspace_id=d['wid'], venture_id=d['vid'],
            app_id=d['app_id'], actor_id=d['actor'], device_id=d['caller'].device_id,
            plan_task_id=d['plan_task_id'], execution_id=eid, provider='codex', state='stored',
            sha256=source.sha256, manifest_sha256=source.manifest_sha256, size_bytes=source.size_bytes,
            receipt_json=json.dumps(receipt), created_at=old_row_time, updated_at=old_row_time))
        session.commit()
    quote = admission.estimate(d['vid'], oid, d['caller'])
    new = admission.admit(d['vid'], oid, d['caller'], quote_id=quote['quote_id'], quote_digest=quote['quote_digest'])
    assert new['id'] != old_id
    retry = {**d, 'order_id': oid, 'build_run_id': new['id']}
    imported = source_step(retry)
    assert imported['spec']['source_import_verified'] is True and len(d['qa'].launches) == 1
    d['qa_state']['phase'] = 'completed'
    source_step(retry)
    assert source_step(retry)['state'] == 'succeeded'
    evidence = read(new['id'], actor=d['actor'])
    review(new['id'], actor=d['actor'], source_artifact_id=evidence['artifact_id'],
          report_digest=evidence['review']['report_digest'], decision='accept')


def test_source_import_rejects_builder_and_model_admission(source_ready):
    from app.build import subscription_static_admission as admission
    from app.builder import orchestration, model_proxy
    d = source_ready
    quote = admission.estimate(d['vid'], d['order_id'], d['caller'])
    run = admission.admit(d['vid'], d['order_id'], d['caller'], quote_id=quote['quote_id'], quote_digest=quote['quote_digest'])
    with pytest.raises(Exception, match='^subscription_static_builder_forbidden$'):
        orchestration.run({'build_run_id': run['id']})
    with pytest.raises(Exception, match='^subscription_static_model_calls_forbidden$'):
        model_proxy.register(run['id'], invocation_key='forbidden-model', input_event_id=None,
            request={'messages': [{'role': 'user', 'content': 'Forbidden test request'}], 'max_tokens': 32},
            generation=1, controller_id='forbidden', attempt=1)


def test_changed_contract_blocks_acceptance_and_publication(source_cloud):
    from app.builder.execution import read, review
    from app.hosting.service import _accepted_source
    from app.db.supymem_models import ExecutionPlan, FounderApp
    d = source_cloud
    source_step(d)
    d['qa_state']['phase'] = 'completed'
    source_step(d)
    assert source_step(d)['state'] == 'succeeded'
    evidence = read(d['build_run_id'], actor=d['actor'])
    body = dict(actor=d['actor'], source_artifact_id=evidence['artifact_id'],
                report_digest=evidence['review']['report_digest'], decision='accept')
    with d['factory']() as session:
        plan = session.get(ExecutionPlan, d['plan_id'])
        doc = json.loads(plan.structured)
        doc['proposal']['milestones'][0]['tasks'][0]['landing_expectations']['contract']['product_name'] = 'Unreviewed change'
        plan.structured = json.dumps(doc)
        session.commit()
    with pytest.raises(Exception, match='^build_review_not_ready$'):
        review(d['build_run_id'], **body)
    with d['factory']() as session:
        with pytest.raises(Exception, match='^hosting_build_not_accepted$'):
            _accepted_source(session, session.get(FounderApp, d['app_id']), d['build_run_id'])


@pytest.mark.parametrize('status', ['failed', 'unsupported'])
def test_failed_or_unsupported_qa_never_opens_review(source_cloud, monkeypatch, status):
    from app.builder import qa_lifecycle as q
    from app.builder.execution import read, review
    from tests.qa_storage_fixtures import report_fixture
    from core.artifact_snapshot import canonical
    d = source_cloud
    def report(binding, _):
        raw, images = report_fixture(d['qa_state'], binding['build_id'])
        doc = json.loads(raw)
        doc['outcome'] = 'failed'
        doc['cases'][0]['status'] = status
        doc['coverage']['unsupported_cases'] = int(status == 'unsupported')
        return canonical(doc), images
    monkeypatch.setattr(q.qa_transport, 'result', report)
    source_step(d)
    d['qa_state']['phase'] = 'completed'
    source_step(d)
    result = source_step(d)
    assert result['closed'] and result['reserved'] == 0 and result['state'] == 'failed', result
    evidence = read(d['build_run_id'], actor=d['actor'])
    assert evidence['verified_execution'] is False and evidence['verified_source_import'] is False
    with pytest.raises(Exception, match='^build_review_not_ready$'):
        review(d['build_run_id'], actor=d['actor'], source_artifact_id=evidence['artifact_id'],
               report_digest='unaccepted', decision='accept')


def test_uncertain_source_qa_launch_recovers_by_discovery_without_relaunch(source_cloud):
    from app.db.supymem_models import QaRun
    d = source_cloud
    d['qa'].lose_launch_response = True
    first = source_step(d)
    assert not first['closed'] and len(d['qa'].tasks) == len(d['qa'].launches) == 1
    with d['factory']() as session:
        row = session.scalar(select(QaRun).where(QaRun.build_run_id == d['build_run_id']))
        launch = json.loads(row.receipt_json)['lifecycle']
        assert launch['phase'] == 'uncertain' and launch['discovery_only'] is True
    source_step(d)
    assert len(d['qa'].tasks) == len(d['qa'].launches) == 1
    d['qa_state']['phase'] = 'completed'
    source_step(d)
    assert source_step(d)['closed']


def test_changed_qa_budget_approval_cannot_reserve(source_ready):
    from app.build import subscription_static_admission as admission
    from app.db.supymem_models import BuildRun
    d = source_ready
    quote = admission.estimate(d['vid'], d['order_id'], d['caller'])
    with pytest.raises(Exception, match='^subscription_static_budget_approval_changed$'):
        admission.admit(d['vid'], d['order_id'], d['caller'], quote_id=quote['quote_id'],
            quote_digest=quote['quote_digest'], approve_qa_ceiling_micro=quote['qa_ceiling_micro'] + 1)
    with d['factory']() as session:
        assert not session.scalar(select(BuildRun.id).where(BuildRun.app_id == d['app_id']))


def test_signed_static_source_qa_api_quotes_and_admits_exact_ceiling(source_ready, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.device_orders_api import router
    from core.assertion import sign_assertion
    d = source_ready
    secret = 'source-static-route-test-at-least-thirty-two'
    monkeypatch.setenv('COS_INTERNAL_ASSERTION_SECRET', secret)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    base = f"/ventures/{d['vid']}/device-work-orders/{d['order_id']}"
    def signed(path):
        return {'X-COS-Assertion': sign_assertion(secret, sub=d['actor'], email='', role='user',
            path=path, method='POST', device_id=d['caller'].device_id)}
    quote_path, admit_path = base + '/static-qa-quote', base + '/static-qa'
    response = client.post(quote_path, headers=signed(quote_path))
    assert response.status_code == 200, response.text
    quote = response.json()
    assert quote['builder_compute_ceiling_micro'] == quote['model_ceiling_micro'] == 0
    assert quote['expires_at'].endswith('Z')
    body = {k: quote[k] for k in ('quote_id', 'quote_digest')}
    body['approve_qa_ceiling_micro'] = quote['qa_ceiling_micro']
    admitted = client.post(admit_path, json=body, headers=signed(admit_path))
    assert admitted.status_code == 200, admitted.text
    assert admitted.json()['mode'] == 'subscription_static_source_qa'
    assert admitted.json()['verified_execution'] is False
    assert client.post(quote_path, headers=signed(quote_path)).json()['existing_run']['id'] == admitted.json()['id']


@pytest.fixture()
def accepted_native_publication(source_cloud):
    """Actual sealed QA protocol with expressly synthetic cloud/browser transport."""
    from app.builder.execution import read, review
    from app.db.supymem_models import Deployment, FounderApp
    from app.credits._txn import naive_utc
    d = source_cloud
    source_step(d)
    d['qa_state']['phase'] = 'completed'
    source_step(d)
    assert source_step(d)['state'] == 'succeeded'
    evidence = read(d['build_run_id'], actor=d['actor'])
    review(d['build_run_id'], actor=d['actor'], source_artifact_id=evidence['artifact_id'],
           report_digest=evidence['review']['report_digest'], decision='accept')
    deployment_id = 'dep_' + uuid.uuid4().hex
    with d['factory']() as session:
        app = session.get(FounderApp, d['app_id'])
        deployment = Deployment(id=deployment_id, app_id=app.id,
            workspace_id=d['wid'], venture_id=d['vid'], build_artifact_id=evidence['artifact_id'],
            target='supymem_static', state='live', version=1,
            url=f'https://{app.slug}.supymem.com/', live_at=naive_utc())
        session.add(deployment)
        session.commit()
    return {**d, 'published_artifact_id': evidence['artifact_id'], 'deployment_id': deployment_id}


def test_published_native_predecessor_remains_historical_after_plan_drift(accepted_native_publication):
    from app.build import subscription_static_baseline as baseline
    from app.hosting.service import _accepted_source
    from app.db.supymem_models import ExecutionPlan, FounderApp
    d = accepted_native_publication
    with d['factory']() as session:
        app = session.get(FounderApp, d['app_id'])
        # Current scope is required for new acceptance, not historical custody.
        plan = session.get(ExecutionPlan, d['plan_id'])
        doc = json.loads(plan.structured)
        doc['proposal']['milestones'][0]['tasks'][0]['landing_expectations']['contract']['product_name'] = 'New proposed idea'
        plan.structured = json.dumps(doc)
        session.commit()
        with pytest.raises(Exception, match='^hosting_build_not_accepted$'):
            _accepted_source(session, app, d['build_run_id'])
        chosen = baseline.reviewed(session, d['wid'], d['vid'])
        assert chosen['deployment_id'] == d['deployment_id']
        assert chosen['source_artifact_id'] == d['published_artifact_id']
        assert _accepted_source(session, app, d['build_run_id'], historical=True)[1].id == chosen['source_artifact_id']


def test_native_review_binds_unique_static_app_among_legacy_apps(accepted_native_publication):
    from app.build import subscription_static_baseline as baseline, device_orders, coding_source_intake
    from app.db.supymem_models import FounderApp, ExecutionPlan
    from app.db.models import VentureDocument
    from app.credits._txn import naive_utc
    d = accepted_native_publication
    with d['factory']() as session:
        session.add(FounderApp(id='legacy_native_fixture', workspace_id=d['wid'], venture_id=d['vid'],
            slug='legacy-native-' + uuid.uuid4().hex, name='Legacy app', kind='service', state='live',
            created_at=naive_utc(), updated_at=naive_utc()))
        session.flush()
        old = session.get(VentureDocument, d['order_id'])
        order = json.loads(old.provenance_json)
        # Selection itself never changes task/plan or chooses the first app.
        snapshot = device_orders._coding_snapshot_profile(session, order['task_snapshot'])
        assert snapshot['app_id'] == d['app_id']
        assert snapshot['publication_baseline']['deployment_id'] == d['deployment_id']
        plan = session.get(ExecutionPlan, d['plan_id'])
        assert snapshot['publication_baseline'] == baseline.reviewed(session, plan.workspace_id, plan.venture_id)
        order['task_snapshot'] = snapshot
        old.provenance_json = json.dumps(order)
        session.flush()
        assert coding_source_intake._scope(session, d['vid'], d['order_id'], d['caller'])[1] == d['app_id']


def test_existing_publication_change_rejects_bound_baseline(accepted_native_publication):
    from app.build import subscription_static_baseline as baseline
    from app.db.supymem_models import Deployment, FounderApp
    d = accepted_native_publication
    with d['factory']() as session:
        app = session.get(FounderApp, d['app_id'])
        bound = baseline.reviewed(session, d['wid'], d['vid'])
        baseline.assert_current(session, app, bound)
        session.get(Deployment, d['deployment_id']).version = 2
        session.flush()
        with pytest.raises(ValueError, match='^subscription_static_publication_baseline_changed$'):
            baseline.assert_current(session, app, bound)


def test_quote_then_new_publication_rejects_admission_without_a_hold(source_ready):
    from app.build import subscription_static_admission as admission
    from app.db.supymem_models import Deployment, FounderApp, BuildRun
    from app.credits._txn import naive_utc
    d = source_ready
    quote = admission.estimate(d['vid'], d['order_id'], d['caller'])
    with d['factory']() as session:
        app = session.get(FounderApp, d['app_id'])
        session.add(Deployment(id='dep_concurrent_fixture', app_id=app.id, workspace_id=d['wid'],
            venture_id=d['vid'], target='supymem_static', state='live', version=1,
            url=f'https://{app.slug}.supymem.com/', live_at=naive_utc()))
        session.commit()
    with pytest.raises(Exception, match='^subscription_static_publication_baseline_unverified$'):
        admission.admit(d['vid'], d['order_id'], d['caller'], quote_id=quote['quote_id'], quote_digest=quote['quote_digest'])
    with d['factory']() as session:
        assert not session.scalar(select(BuildRun.id).where(BuildRun.app_id == d['app_id']))


def test_publication_change_during_storage_stops_before_qa(source_ready, monkeypatch):
    from app.build import subscription_static_admission as admission
    from app.db.supymem_models import Deployment, FounderApp, QaRun
    from app.credits._txn import naive_utc
    d = source_ready
    quote = admission.estimate(d['vid'], d['order_id'], d['caller'])
    run = admission.admit(d['vid'], d['order_id'], d['caller'], quote_id=quote['quote_id'], quote_digest=quote['quote_digest'])
    original = d['store'].persist
    def changed(*args, **kwargs):
        stored = original(*args, **kwargs)
        with d['factory']() as session:
            app = session.get(FounderApp, d['app_id'])
            session.add(Deployment(id='dep_upload_' + uuid.uuid4().hex, app_id=app.id,
                workspace_id=d['wid'], venture_id=d['vid'], target='supymem_static', state='live',
                version=1, url=f'https://{app.slug}.supymem.com/', live_at=naive_utc()))
            session.commit()
        return stored
    monkeypatch.setattr(d['store'], 'persist', changed)
    monkeypatch.setattr('app.builder.qa_lifecycle.watch', lambda *_: pytest.fail('QA must not launch after publication drift'))
    value = source_step({**d, 'build_run_id': run['id']})
    assert value['closed'] and value['state'] == 'failed' and value['reserved'] == 0
    assert value['spec']['source_import_verified'] is False
    with d['factory']() as session:
        assert not session.scalar(select(QaRun.id).where(QaRun.build_run_id == run['id']))


@pytest.mark.parametrize('stop_before_import', [None, 'cancel', 'deadline'])
def test_registered_source_watch_uses_production_expiring_sessions(source_cloud, stop_before_import):
    import asyncio
    from sqlalchemy.orm import sessionmaker
    from app.state import set_factory
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    from app.builder.execution import cancel
    from app.db.models import Job
    from tests.integration.test_postgres_fargate_lifecycle import claim, saved
    d = source_cloud
    factory = sessionmaker(d['engine'], expire_on_commit=True)
    set_factory(factory)
    d = {**d, 'factory': factory}
    install_default_handlers()
    if stop_before_import == 'cancel':
        cancel(d['build_run_id'], actor=d['actor'])
    elif stop_before_import == 'deadline':
        from datetime import timedelta
        from app.credits._txn import naive_utc
        from app.db.supymem_models import BuildRun
        with factory() as session:
            run = session.get(BuildRun, d['build_run_id'])
            spec = json.loads(run.spec)
            spec['lifecycle']['queue_deadline'] = (naive_utc() - timedelta(seconds=1)).isoformat()
            run.spec = json.dumps(spec)
            session.commit()
    for step in range(1 if stop_before_import else 3):
        job, _ = claim(d, 'build.watch')
        asyncio.run(_run_one(factory, job, {job.id}))
        with factory() as session:
            stored = session.get(Job, job.id)
            assert stored.status == 'succeeded', stored.last_error
        if not stop_before_import:
            d['qa_state']['phase'] = 'completed'
    result = saved(d)
    if stop_before_import:
        assert result['state'] == ('cancelled' if stop_before_import == 'cancel' else 'failed') and result['closed'] and result['reserved'] == 0
        assert len(d['qa'].launches) == 0 and result['spent'] == 0
    else:
        assert result['state'] == 'succeeded' and result['closed'] and result['reserved'] == 0
        assert result['spec']['source_import_verified'] is True and len(d['qa'].launches) == 1


@pytest.mark.parametrize('stop_kind', ['startup_failure', 'cancel'])
def test_stopped_reportless_qa_child_closes_only_after_confirmed_cleanup(source_cloud, monkeypatch, stop_kind):
    from app.builder.execution import BuildError, cancel, read, review
    from app.builder import qa_receipts
    from app.db.supymem_models import QaRun
    d = source_cloud
    source_step(d)
    assert len(d['qa'].launches) == 1
    if stop_kind == 'cancel':
        cancel(d['build_run_id'], actor=d['actor'])
    else:
        task = next(iter(d['qa'].tasks.values()))
        from datetime import UTC, datetime
        task.update(lastStatus='STOPPED', stoppedAt=datetime.now(UTC), stopCode='EssentialContainerExited')
    describe = d['qa'].describe_tasks
    def unavailable(**kwargs):
        raise TimeoutError('synthetic observation unavailable')
    monkeypatch.setattr(d['qa'], 'describe_tasks', unavailable)
    pending = source_step(d)
    assert not pending['closed']
    with d['factory']() as session:
        qa = session.scalar(select(QaRun).where(QaRun.build_run_id == d['build_run_id']))
        assert qa.state in {'queued', 'running'} and qa.finished_at is None
    monkeypatch.setattr(d['qa'], 'describe_tasks', describe)
    result = source_step(d)
    expected = 'cancelled' if stop_kind == 'cancel' else 'failed'
    assert result['closed'] and result['state'] == expected and result['reserved'] == 0
    assert result['spec']['qa_state'] == expected
    assert result['spec']['lifecycle']['completion']['verified_report'] is False
    evidence = read(d['build_run_id'], actor=d['actor'])
    assert evidence['verified_source_import'] is False and evidence['review']['status'] != 'pending'
    with d['factory']() as session:
        qa = session.scalar(select(QaRun).where(QaRun.build_run_id == d['build_run_id']))
        assert qa.state == expected and qa.finished_at is not None
        assert qa.report_digest is None and qa.report_s3_key is None
        assert qa.passed == qa.failed == qa.unsupported == 0
        assert qa_receipts.serialize(qa)['verified_report'] is False
        assert 'completion' not in json.loads(qa.receipt_json)
    with pytest.raises(BuildError, match="build_review_not_ready"):
        review(d['build_run_id'], actor=d['actor'], source_artifact_id=evidence['artifact_id'],
               report_digest='0' * 64, decision='accept')
    assert len(d['qa'].launches) == 1


def test_qa_startup_exit_closes_queued_child_without_report(source_cloud, monkeypatch):
    from datetime import UTC, datetime
    from app.db.supymem_models import QaRun
    from app.builder import qa_receipts
    d = source_cloud
    launch = d['qa'].run_task
    def startup_exit(**body):
        response = launch(**body)
        for task in d['qa'].tasks.values():
            task.update(lastStatus='STOPPED', stoppedAt=datetime.now(UTC), stopCode='EssentialContainerExited')
        return response
    monkeypatch.setattr(d['qa'], 'run_task', startup_exit)
    result = source_step(d)
    assert result['closed'] and result['state'] == 'failed' and result['reserved'] == 0
    assert result['spec']['qa_state'] == 'failed'
    with d['factory']() as session:
        qa = session.scalar(select(QaRun).where(QaRun.build_run_id == d['build_run_id']))
        assert qa.state == 'failed' and qa.finished_at
        assert qa.report_digest is None and qa.report_s3_key is None
        assert qa_receipts.serialize(qa)['verified_report'] is False
        assert 'trusted_task' not in json.loads(qa.receipt_json)
    assert len(d['qa'].launches) == 1


def test_native_static_review_binds_owned_beacon_before_approval(source_ready):
    from app.build import device_orders
    from app.db.supymem_models import AppApiKey, BuildRun
    from app.product_analytics.beacon import script_tag
    from app.db.models import CreditReservation
    d = source_ready
    body = device_orders.prepare_coding_task_from_chat(d['vid'], d['caller'],
        'Use Codex to implement the current approved static landing task. Prepare the exact coding card before execution.')
    assert body['state'] == 'pending' and not body.get('claimed_at')
    assert body['task_snapshot']['publication_baseline']['app_id'] == d['app_id']
    instruction = body['manifest']['instruction']
    with d['factory']() as session:
        keys = list(session.scalars(select(AppApiKey).where(AppApiKey.app_id == d['app_id'])))
        public = [key for key in keys if key.prefix.startswith('pk_app_') and key.state == 'active']
        assert len(public) == 1 and json.loads(public[0].scopes) == ['ingest']
        assert instruction.count(script_tag(key=public[0].prefix).strip()) == 1
        assert not session.scalar(select(BuildRun.id).where(BuildRun.app_id == d['app_id']))
        assert not session.scalar(select(CreditReservation.id).where(CreditReservation.purpose == 'device.work_order', CreditReservation.workspace_id == d['wid']))
    assert 'target="_blank"' in instruction and 'rel="noopener noreferrer"' in instruction
    assert body['manifest']['origins'] == [] and body['manifest']['credit_ceiling_micro'] == 0
    assert len(instruction) <= 12000
    from app.db.models import VentureDocument
    with d['factory']() as session:
        row = session.get(VentureDocument, body['id'])
        altered = json.loads(row.provenance_json)
        altered['manifest']['instruction'] = altered['manifest']['instruction'].replace('data-supymem-analytics="1"', 'data-supymem-analytics="2"')
        row.provenance_json = json.dumps(altered)
        session.commit()
    with pytest.raises(ValueError, match='device_order_review_changed'):
        device_orders.transition(d['vid'], d['caller'], body['id'], body['review_digest'], 'approve')


def test_static_qa_pass_does_not_impersonate_founder_content_review(source_cloud):
    from app.autopilot.service import _accept_passed_builds
    from app.builder.execution import read
    from app.db.supymem_models import BuildEvent
    d = source_cloud
    source_step(d)
    d['qa_state']['phase'] = 'completed'
    source_step(d)
    source_step(d)
    before = read(d['build_run_id'], actor=d['actor'])
    assert before['review']['status'] == 'pending'
    from app.autopilot.service import update_settings
    update_settings(d['vid'], god_mode=True)
    done = []
    _accept_passed_builds(d['vid'], d['actor'], done)
    after = read(d['build_run_id'], actor=d['actor'])
    assert after['review']['status'] == 'pending'
    assert not done
    with d['factory']() as session:
        assert not session.scalar(select(BuildEvent.id).where(
            BuildEvent.build_run_id == d['build_run_id'], BuildEvent.kind == 'founder_review'))


@pytest.fixture()
def private_source_cloud(journey_db, monkeypatch, request):
    # This test covers a private artifact without a typed publication contract.
    # Select that resource boundary before plan generation and source capture;
    # never remove a public obligation from an already approved build.
    from app.planning import resources
    monkeypatch.setattr(resources, 'publication_contract', lambda _venture_id: {})
    return request.getfixturevalue('source_cloud')


def test_private_accepted_correction_saves_exact_feedback_without_rewriting_task(private_source_cloud, monkeypatch):
    from app.builder import private_corrections as corrections, budgets
    from app.builder.execution import read, review, start
    from app.db.supymem_models import BuildRun, PlanTask, FounderRequest
    d = private_source_cloud
    source_step(d)
    d['qa_state']['phase'] = 'completed'
    source_step(d)
    source_step(d)
    evidence = read(d['build_run_id'], actor=d['actor'])
    assert evidence['publication_required'] is False
    review(d['build_run_id'], actor=d['actor'], source_artifact_id=evidence['artifact_id'],
           report_digest=evidence['review']['report_digest'], decision='accept')
    # Historical missing attribution is not itself a refusal. Actual exact
    # accepted source/QA/current-plan evidence remains the authority.
    with d['factory']() as session:
        qualified = session.get(BuildRun, d['build_run_id'])
        qualified_spec = json.loads(qualified.spec)
        qualified_spec['lifecycle']['review'].pop('review_origin', None)
        qualified.spec = json.dumps(qualified_spec)
        session.commit()
    eligible = corrections.availability(d['build_run_id'], actor=d['actor'])
    assert eligible == {'build_id': d['build_run_id'], 'available': True, 'reason': '',
        'source_artifact_id': evidence['artifact_id'], 'report_digest': evidence['review']['report_digest']}
    with d['factory']() as session:
        task = session.get(PlanTask, d['plan_task_id'])
        old_task = (task.state, task.build_run_id)
        assert old_task == ('done', d['build_run_id'])
    body = dict(actor=d['actor'], source_artifact_id=evidence['artifact_id'],
                report_digest=evidence['review']['report_digest'], instruction='Make the headline describe the reviewed customer problem.')
    saved = corrections.request(d['build_run_id'], **body)
    assert corrections.request(d['build_run_id'], **body) == saved
    assert saved['state'] == 'pending' and saved['next_gate'] == 'review_correction_estimate'
    with pytest.raises(Exception, match='private_correction_baseline_changed'):
        corrections.request(d['build_run_id'], **{**body, 'source_artifact_id': 'foreign'})
    with pytest.raises((PermissionError, KeyError)):
        corrections.request(d['build_run_id'], **{**body, 'actor': 'foreign-owner'})
    from tests.lifecycle_fixtures import runtime
    priced = runtime()
    monkeypatch.setattr(budgets, 'runtime_quote', lambda *args, **kwargs: priced)
    quoted = corrections.estimate(d['build_run_id'], actor=d['actor'], request_id=saved['id'])
    assert quoted['ready'], quoted
    from app.db.supymem_models import ExecutionPlan
    with d['factory']() as session:
        plan = session.get(ExecutionPlan, d['plan_id'])
        original_plan = plan.structured
        changed = json.loads(original_plan)
        changed['proposal']['milestones'][0]['tasks'][0]['description'] += ' Changed after estimate.'
        plan.structured = json.dumps(changed)
        session.commit()
    assert corrections.availability(d['build_run_id'], actor=d['actor']) == {
        'build_id': d['build_run_id'], 'available': False, 'reason': 'hosting_build_not_accepted'}
    with pytest.raises(Exception, match='private_correction_baseline_changed|hosting_build_not_accepted|private_correction_plan_changed'):
        start(d['app_id'], actor=d['actor'], quote_id=quoted['quote_id'], input_digest=quoted['input_digest'],
            approve_budget_micro=quoted['ceiling_micro'], idempotency_key='private-stale-denied')
    with d['factory']() as session:
        assert session.query(BuildRun).filter(BuildRun.app_id == d['app_id']).count() == 1
        session.get(ExecutionPlan, d['plan_id']).structured = original_plan
        session.commit()
    with pytest.raises(Exception, match='build_budget_approval_mismatch'):
        start(d['app_id'], actor=d['actor'], quote_id=quoted['quote_id'], input_digest=quoted['input_digest'],
            approve_budget_micro=quoted['ceiling_micro'] + 1, idempotency_key='private-wrong-budget')
    admitted = start(d['app_id'], actor=d['actor'], quote_id=quoted['quote_id'], input_digest=quoted['input_digest'],
        approve_budget_micro=quoted['ceiling_micro'], idempotency_key='private-reviewed-correction')
    assert admitted['state'] == 'queued'
    correction_view = read(admitted['build_run_id'], actor=d['actor'])
    assert correction_view['quote_id'] == quoted['quote_id']
    assert correction_view['correction_request_id'] == saved['id']
    assert start(d['app_id'], actor=d['actor'], quote_id=quoted['quote_id'], input_digest=quoted['input_digest'],
        approve_budget_micro=quoted['ceiling_micro'], idempotency_key='private-reviewed-correction') == admitted
    with pytest.raises(Exception, match='build_idempotency_conflict'):
        start(d['app_id'], actor=d['actor'], quote_id=quoted['quote_id'], input_digest='0' * 64,
            approve_budget_micro=quoted['ceiling_micro'], idempotency_key='private-reviewed-correction')
    with d['factory']() as session:
        task = session.get(PlanTask, d['plan_task_id'])
        assert (task.state, task.build_run_id) == old_task
        correction_run = session.get(BuildRun, admitted['build_run_id'])
        assert json.loads(correction_run.spec)['inputs']['revision']['request_id'] == saved['id']
        assert session.get(FounderRequest, saved['id']).outcome == 'pending'
    assert read(d['build_run_id'], actor=d['actor'])['review']['status'] == 'accepted'
    from app.ventures.lifecycle import pause
    pause(d['vid'], actor_id=d['actor'], reason='Stop correction work')
    with pytest.raises(Exception, match='private_correction_venture_paused'):
        corrections.request(d['build_run_id'], **{**body, 'instruction': 'A different correction after Stop was requested.'})


def test_public_source_acceptance_keeps_its_task_pending_and_refuses_private_correction(source_cloud):
    from app.builder import private_corrections as corrections
    from app.builder.execution import BuildError, read, review
    from app.db.supymem_models import BuildRun, FounderRequest, PlanTask
    d = source_cloud
    source_step(d)
    d['qa_state']['phase'] = 'completed'
    source_step(d)
    source_step(d)
    before = read(d['build_run_id'], actor=d['actor'])
    assert before['publication_required'] is True
    review(d['build_run_id'], actor=d['actor'], source_artifact_id=before['artifact_id'],
           report_digest=before['review']['report_digest'], decision='accept')
    accepted = read(d['build_run_id'], actor=d['actor'])
    assert accepted['review']['status'] == 'accepted'
    assert accepted['task_completion']['state'] == 'pending_publication'
    with d['factory']() as session:
        requests_before = set(session.scalars(select(FounderRequest.id).where(FounderRequest.related_id == d['app_id'])))
    assert corrections.availability(d['build_run_id'], actor=d['actor']) == {
        'build_id': d['build_run_id'], 'available': False, 'reason': 'private_correction_plan_changed'}
    with pytest.raises(BuildError, match='private_correction_plan_changed'):
        corrections.request(d['build_run_id'], actor=d['actor'], source_artifact_id=before['artifact_id'],
            report_digest=before['review']['report_digest'], instruction='Do not silently replace this public source.')
    with d['factory']() as session:
        task = session.get(PlanTask, d['plan_task_id'])
        assert (task.state, task.build_run_id, task.completed_at) == ('waiting_for_founder', d['build_run_id'], None)
        assert session.query(BuildRun).filter(BuildRun.app_id == d['app_id']).count() == 1
        assert set(session.scalars(select(FounderRequest.id).where(FounderRequest.related_id == d['app_id']))) == requests_before
        assert not session.scalar(select(FounderRequest.id).where(
            FounderRequest.related_id == d['app_id'], FounderRequest.source == corrections.KIND))
    after = read(d['build_run_id'], actor=d['actor'])
    assert after['task_completion'] == accepted['task_completion']
    assert after['artifact_id'] == before['artifact_id']
    assert after['usage'] == accepted['usage']
