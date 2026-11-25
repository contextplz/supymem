"""Coding continuation quotes immutable failed QA, with live task custody."""
import json

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_static_qa_guidance import (  # noqa: F401
    pg_database, journey_db, planning_standard, planning, ready_plan,
    source_ready, source_cloud, failed_source,
)


def _prepare(d):
    from app.build import device_orders
    from app.admin.authn import CallerContext
    caller = CallerContext(d['actor'], '', 'user', '/test', 'POST', 9999999999,
                           'test', device_id=d['caller'].device_id)
    with d['factory']() as session:
        from app.db.supymem_models import PlanTask
        title = session.get(PlanTask, d['plan_task_id']).title
    return device_orders.prepare_coding_task_from_chat(d['vid'], caller,
        f'Use Codex to edit {title}. Correct the saved browser QA failures only; do not publish.'), caller


def test_reviewed_continuation_carries_exact_failed_report_without_effects(failed_source):  # noqa: F811
    from app.db.models import Job, CreditLedgerEntry, CreditReservation
    from app.db.supymem_models import BuildRun, QaRun, PlanTask
    d = failed_source
    with d['factory']() as session:
        before = tuple(list(session.scalars(select(m.id))) for m in (Job, CreditLedgerEntry, CreditReservation, BuildRun))
        old = session.get(BuildRun, d['build_run_id'])
        old_state = (old.state, old.spec, old.credits_used, old.closed_at)
        qa = session.scalar(select(QaRun).where(QaRun.build_run_id == old.id))
        expected_counts = {'passed': qa.passed, 'failed': qa.failed, 'unsupported': qa.unsupported}
        digest, sha = qa.report_digest, qa.source_sha256
    order, _caller = _prepare(d)
    assert order['state'] == 'pending' and not order.get('claimed_at')
    assert order['manifest']['credit_ceiling_micro'] == 0
    feedback = order['task_snapshot']['coding_qa_feedback']
    assert feedback['build_run_id'] == d['build_run_id']
    assert feedback['report_digest'] == digest and feedback['source_sha256'] == sha
    assert feedback['counts'] == expected_counts
    assert feedback['cases'][0]['status'] == 'failed'
    observed = 'Synthetic missing approved text observation: 2. A reminder is sent to the client.'
    assert feedback['cases'][0]['observed'] == observed
    assert observed in order['manifest']['instruction']
    assert '2. A reminder is sent to the client.' in order['manifest']['instruction']
    assert 'fresh independent QA, founder source acceptance and publication approvals remain separate' in order['manifest']['instruction']
    with d['factory']() as session:
        old = session.get(BuildRun, d['build_run_id'])
        assert (old.state, old.spec, old.credits_used, old.closed_at) == old_state
        assert session.get(PlanTask, d['plan_task_id']).build_run_id == old.id
        assert tuple(list(session.scalars(select(m.id))) for m in (Job, CreditLedgerEntry, CreditReservation, BuildRun)) == before


@pytest.mark.parametrize('change', [
    'foreign_qa', 'foreign_source', 'changed_contract', 'superseded_plan',
    'unverified_receipt', 'report_tamper', 'changed_report_digest', 'changed_counts',
])
def test_stale_foreign_or_tampered_qa_never_becomes_coding_feedback(failed_source, change, monkeypatch):  # noqa: F811
    from app.db.models import VentureDocument
    from app.db.supymem_models import BuildRun, QaRun, ExecutionPlan
    from app.builder.artifact_store import S3ArtifactStore
    d = failed_source
    if change == 'report_tamper':
        original = d['store'].qa
        monkeypatch.setattr(S3ArtifactStore, 'qa', lambda _self, *args, **kwargs:
                            original(*args, **kwargs) + b' ')
    else:
        with d['factory']() as session:
            run = session.get(BuildRun, d['build_run_id'])
            qa = session.scalar(select(QaRun).where(QaRun.build_run_id == run.id))
            if change == 'foreign_qa':
                receipt = json.loads(qa.receipt_json)
                receipt['scope']['venture_id'] = 'ven_foreign'
                qa.receipt_json = json.dumps(receipt)
            elif change == 'foreign_source':
                # Migrated source-scope FKs already prevent rebinding the row.
                # Reject a receipt that instead quotes an unrelated source.
                receipt = json.loads(qa.receipt_json)
                receipt['input']['source_artifact_id'] = 'bsa_foreign'
                qa.receipt_json = json.dumps(receipt)
            elif change == 'changed_report_digest':
                qa.report_digest = '0' * 64
            elif change == 'changed_counts':
                qa.failed += 1
            elif change == 'unverified_receipt':
                receipt = json.loads(qa.receipt_json)
                receipt['verified_report'] = False
                qa.receipt_json = json.dumps(receipt)
            else:
                plan = session.get(ExecutionPlan, d['plan_id'])
                if change == 'superseded_plan':
                    plan.superseded_by = 'pln_newer'
                else:
                    doc = json.loads(plan.structured)
                    doc['proposal']['milestones'][0]['tasks'][0]['landing_expectations']['contract']['required_text'][0] = 'Changed approved scope'
                    plan.structured = json.dumps(doc)
            session.commit()
    with d['factory']() as session:
        before = list(session.scalars(select(VentureDocument.id)))
    with pytest.raises(ValueError):
        _prepare(d)
    with d['factory']() as session:
        assert list(session.scalars(select(VentureDocument.id))) == before


def test_claim_rechecks_report_custody_after_the_review_was_prepared(failed_source):  # noqa: F811
    from app.build import device_orders
    from app.db.supymem_models import QaRun
    d = failed_source
    order, caller = _prepare(d)
    args = (d['vid'], caller, order['id'], order['review_digest'])
    device_orders.transition(*args, 'approve')
    with d['factory']() as session:
        qa = session.scalar(select(QaRun).where(QaRun.build_run_id == d['build_run_id']))
        qa.report_digest = '0' * 64
        session.commit()
    with pytest.raises(ValueError, match='device_coding_qa_feedback_unavailable'):
        device_orders.transition(*args, 'claim')
    assert device_orders.retrieve(d['vid'], caller, order['id'])['state'] == 'approved'


def test_second_failed_repair_uses_its_own_report_and_preserves_prior_feedback(failed_source):  # noqa: F811
    from app.build import device_orders, device_stop, subscription_static_admission as admission
    from app.db.supymem_models import CodingSourceReceipt
    from app.chat.static_qa_guidance import answer, VerifiedStaticQaEvidence
    from app.credits._txn import naive_utc
    from tests.integration.test_postgres_subscription_static import source_step
    d = failed_source
    order, caller = _prepare(d)
    args = (d['vid'], caller, order['id'], order['review_digest'])
    device_orders.transition(*args, 'approve')
    claimed = device_orders.transition(*args, 'claim')
    device_stop.record(d['vid'], caller, order['id'], execution_id=claimed['execution_id'],
        evidence_digest='b' * 64, processes_stopped=True, task_verified=False)
    # Model/local source capture is a fixture; the reviewed order, migrated QA
    # admission, immutable report capture and closed-run transitions are real.
    with d['factory']() as session:
        source = session.get(CodingSourceReceipt, d['order_id'])
        original_scope = {'workspace_id': d['wid'], 'venture_id': d['vid'],
                          'app_id': d['app_id'], 'order_id': d['order_id']}
        blob = d['store'].read_coding_source(json.loads(source.receipt_json), original_scope,
            sha256=source.sha256, manifest_digest=source.manifest_sha256)
        stored = d['store'].persist_coding_source(blob, {**original_scope, 'order_id': order['id']},
            sha256=source.sha256, manifest_digest=source.manifest_sha256)
        session.add(CodingSourceReceipt(order_id=order['id'], workspace_id=d['wid'], venture_id=d['vid'],
            app_id=d['app_id'], actor_id=d['actor'], device_id=caller.device_id,
            plan_task_id=d['plan_task_id'], execution_id=claimed['execution_id'], provider='codex',
            state='stored', sha256=source.sha256, manifest_sha256=source.manifest_sha256,
            size_bytes=source.size_bytes, receipt_json=json.dumps(stored),
            created_at=naive_utc(), updated_at=naive_utc()))
        session.commit()
    quote = admission.estimate(d['vid'], order['id'], caller)
    run = admission.admit(d['vid'], order['id'], caller, quote_id=quote['quote_id'], quote_digest=quote['quote_digest'])
    assert run['id'] != d['build_run_id']
    next_run = {**d, 'order_id': order['id'], 'build_run_id': run['id']}
    d['qa_state']['phase'] = 'running'
    source_step(next_run)
    d['qa_state']['phase'] = 'completed'
    source_step(next_run)
    assert source_step(next_run)['state'] == 'failed'
    advice = answer(d['vid'], d['wid'], run['id'])
    assert isinstance(advice, VerifiedStaticQaEvidence)
    assert f"Run {run['id']} remains failed" in advice['body_md']
    fresh, _ = _prepare(next_run)
    feedback = fresh['task_snapshot']['coding_qa_feedback']
    assert feedback['build_run_id'] == run['id']
    assert feedback['report_digest'] != order['task_snapshot']['coding_qa_feedback']['report_digest']


def test_feedback_keeps_whole_unsupported_observations_and_discloses_omissions(source_cloud, monkeypatch):  # noqa: F811
    from app.builder import qa_lifecycle
    from tests.qa_storage_fixtures import report_fixture
    from core.artifact_snapshot import canonical
    from tests.integration.test_postgres_subscription_static import source_step
    d = source_cloud
    observation = 'Source-owned unsupported diagnostic: ' + 'x' * 550
    def result(binding, _reader):
        blob, images = report_fixture(d['qa_state'], binding['build_id'])
        report = json.loads(blob)
        report['outcome'] = 'failed'
        for index, case in enumerate(report['cases']):
            case['status'] = 'failed' if index == 0 else 'unsupported'
            case['observed'] = observation
        report['coverage']['unsupported_cases'] = len(report['cases']) - 1
        return canonical(report), images
    monkeypatch.setattr(qa_lifecycle.qa_transport, 'result', result)
    source_step(d)
    d['qa_state']['phase'] = 'completed'
    source_step(d)
    assert source_step(d)['state'] == 'failed'
    order, _caller = _prepare(d)
    feedback = order['task_snapshot']['coding_qa_feedback']
    assert feedback['counts']['failed'] == 1 and feedback['counts']['unsupported'] > 1
    assert feedback['cases'][0]['status'] == 'failed'
    assert any(case['status'] == 'unsupported' for case in feedback['cases'])
    assert all(case['observed'] == observation for case in feedback['cases'])
    assert feedback['omitted_cases'] > 0 and len(canonical(feedback)) <= 4000
    assert len(feedback['cases']) + feedback['omitted_cases'] == sum(
        feedback['counts'][status] for status in ('failed', 'unsupported'))
    assert 'unsupported check is not a pass' in order['manifest']['instruction']
