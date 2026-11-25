"""Source-import controller followed only by independently attested static QA."""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import select

from app.build import subscription_static_inputs as inputs, subscription_static_policy as policy
from app.build.subscription_static_export import export
from app.builder import lifecycle, qa_receipts
from app.builder.artifact_store import S3ArtifactStore
from app.builder.model_proxy import BuildModelError, digest
from app.core.fsm import advance
from app.credits import pools
from app.credits._txn import naive_utc
from app.db.models import CreditReservation
from app.db.supymem_models import BuildArtifact, BuildModelCall, PlanTask, PlanMilestone, QaRun
from app.state import db


def deadline_reason(spec):
    now = naive_utc()
    if now >= datetime.fromisoformat(spec['lifecycle']['total_deadline']):
        return 'subscription_static_qa_deadline'
    if (spec['source_import']['state'] != 'stored'
            and now >= datetime.fromisoformat(spec['lifecycle']['queue_deadline'])):
        return 'subscription_static_import_deadline'
    return None


def recheck(session, ctx, spec, *, check_rate=True):
    if ctx.run.trigger == 'pagebuilder_source_qa':
        from app.pagebuilder_admission.inputs import recheck as server_recheck
        return server_recheck(session, ctx, spec, check_rate=check_rate)
    if check_rate:
        policy.validate(spec['runtime'])
    scope = spec['source_import']['identity']['scope']
    caller = SimpleNamespace(sub=scope['caller_subject'], role='user', legacy=False,
                             device_id=scope['device_id'])
    envelope, _app = inputs.current(session, ctx.run.venture_id, scope['order_id'], caller,
                                    active_run_id=ctx.run.id)
    reason = ('build_review_source_identity_changed' if envelope['identity'] != spec['source_import']['identity'] else
              'build_review_request_fingerprint_changed' if envelope['fingerprint'] != ctx.run.request_digest else
              'build_review_task_inputs_changed' if envelope['inputs'] != spec['inputs'] else None)
    if reason:
        raise BuildModelError('subscription_static_approved_inputs_changed', diagnostics={'review_reason': reason})


def _import(payload):
    from app.db.supymem_models import BuildRun
    with db()() as session:
        selected = session.get(BuildRun, payload.get('build_run_id'))
        server_source = selected is not None and selected.trigger == 'pagebuilder_source_qa'
    if server_source:
        from app.pagebuilder_admission.controller import import_source
        return import_source(payload)
    from app.builder.orchestration import _begin, _save, _commit, _followup, _revoke_if_needed
    with db()() as session:
        ctx, spec = _begin(session, payload)
        spec = _revoke_if_needed(session, ctx, spec)
        if spec['source_import']['state'] == 'stored':
            return True
        if spec.get('credentials_revoked'):
            lifecycle.release_unlaunched_qa(session, ctx, spec)
            finish(session, ctx, spec)
            _commit(session, ctx, payload)
            return False
        try:
            recheck(session, ctx, spec)
        except Exception:
            pools.mark_reconciliation(session, ctx, 'subscription_static_approved_inputs_changed')
            spec = json.loads(ctx.run.spec)
            lifecycle.release_unlaunched_qa(session, ctx, spec)
            finish(session, ctx, spec)
            _commit(session, ctx, payload)
            return False
        frozen = spec['source_import']['identity']
        spec['source_import']['state'] = 'verifying_storage'
        _save(ctx, spec)
        _commit(session, ctx, payload)
    try:
        store = S3ArtifactStore.configured()
        scope = frozen['scope']
        original = frozen['source']
        coding_scope = {key: scope[key] for key in ('workspace_id', 'venture_id', 'app_id', 'order_id')}
        blob = store.read_coding_source(original['storage_receipt'], coding_scope,
                                        sha256=original['sha256'], manifest_digest=original['manifest_digest'])
        public_blob, projection = export(blob, source_sha256=original['sha256'],
            source_manifest_digest=original['manifest_digest'], venture_id=scope['venture_id'])
        artifact_scope = {key: scope[key] for key in ('workspace_id', 'venture_id', 'app_id')}
        artifact_scope.update(build_id=payload['build_run_id'], generation=spec['generation'])
        stored = store.persist(public_blob, projection, artifact_scope)
        if store.source(stored, artifact_scope) != public_blob:
            raise BuildModelError('subscription_static_storage_readback_mismatch')
    except Exception as error:
        with db()() as session:
            ctx, spec = _begin(session, payload)
            # The existing store uses IfNoneMatch and exact checksum/version
            # readback. A lost ACK retries only the same deterministic key.
            uncertain = str(error) in {'artifact_store_outcome_uncertain', 'artifact_store_read_failed',
                'coding_store_read_failed', 'coding_store_outcome_uncertain'}
            if uncertain and not deadline_reason(spec):
                spec['phase'] = 'subscription_static_storage_reconciliation'
                _followup(session, ctx, spec)
            else:
                pools.mark_reconciliation(session, ctx, 'subscription_static_source_import_failed')
                spec = json.loads(ctx.run.spec)
                lifecycle.release_unlaunched_qa(session, ctx, spec)
                finish(session, ctx, spec)
            _save(ctx, spec)
            _commit(session, ctx, payload)
        return False
    with db()() as session:
        ctx, spec = _begin(session, payload)
        spec = _revoke_if_needed(session, ctx, spec)
        try:
            recheck(session, ctx, spec)
        except Exception:
            pools.mark_reconciliation(session, ctx, 'subscription_static_approved_inputs_changed')
            spec = json.loads(ctx.run.spec)
            lifecycle.release_unlaunched_qa(session, ctx, spec)
            finish(session, ctx, spec)
            _commit(session, ctx, payload)
            return False
        if spec.get('credentials_revoked'):
            lifecycle.release_unlaunched_qa(session, ctx, spec)
            finish(session, ctx, spec)
            _commit(session, ctx, payload)
            return False
        artifact_id = 'bsa_' + digest({'run': ctx.run.id, 'projection': projection})[:48]
        artifact = session.get(BuildArtifact, artifact_id)
        if artifact is None:
            artifact = BuildArtifact(id=artifact_id, build_run_id=ctx.run.id, app_id=ctx.run.app_id,
                workspace_id=ctx.run.workspace_id, kind='source_zip', s3_key=stored['key'],
                sha256=projection['archive_sha256'], size_bytes=len(public_blob), created_at=naive_utc())
            session.add(artifact)
        elif (artifact.build_run_id, artifact.sha256, artifact.s3_key, artifact.size_bytes) != (
                ctx.run.id, projection['archive_sha256'], stored['key'], len(public_blob)):
            raise BuildModelError('subscription_static_source_artifact_conflict')
        ctx.run.artifact_id = artifact_id
        spec['artifact_capture'] = {**stored, 'state': 'stored', 'artifact_id': artifact_id,
                                   'manifest': projection['manifest']}
        spec.update(artifact_verified=True, source_import_verified=True, phase='qa_ready')
        spec['source_import'].update(state='stored', projection=projection,
                                     artifact_receipt_digest=digest(spec['artifact_capture']))
        spec['lifecycle']['phase'] = 'qa_ready'
        _save(ctx, spec)
        from app.builder.execution import _event
        _event(session, ctx.run, 'subscription_source_import_verified', {
            'original_source_sha256': frozen['source']['sha256'],
            'public_source_sha256': projection['archive_sha256'], 'artifact_id': artifact_id,
            'omitted_paths': projection['omitted_paths']})
        _commit(session, ctx, payload)
        return True


def watch(payload):
    if _import(payload):
        from app.builder.qa_lifecycle import watch as qa_watch
        return qa_watch(payload)


def finish(session, ctx, spec):
    """Close QA-only costs and proof, never fabricate a builder execution."""
    if not lifecycle.cleanup_complete(spec):
        return False
    calls = session.scalars(select(BuildModelCall).where(BuildModelCall.build_run_id == ctx.run.id)).all()
    children = session.scalars(select(CreditReservation).where(CreditReservation.parent_id == ctx.pool.id)).all()
    if calls or any(child.purpose != 'build.compute.qa' for child in children):
        raise BuildModelError('subscription_static_forbidden_provider_usage')
    if any(child.status in pools.OPEN_CHILD for child in children):
        return False
    qa = session.get(QaRun, spec['lifecycle'].get('qa_id')) if spec['lifecycle'].get('qa_id') else None
    receipt = json.loads(qa.receipt_json) if qa else {}
    qa_verified = bool(qa and qa_receipts.serialize(qa)['verified_report'])
    if qa and (qa.build_run_id, qa.workspace_id, qa.venture_id, qa.app_id, qa.generation, qa.source_artifact_id) != (
            ctx.run.id, ctx.run.workspace_id, ctx.run.venture_id, ctx.run.app_id, spec['generation'], ctx.run.artifact_id):
        raise BuildModelError('qa_receipt_scope_invalid')
    success = bool(spec.get('source_import_verified') and spec.get('artifact_verified')
        and qa_verified and receipt.get('outcome') == 'passed'
        and spec['lifecycle']['qa'].get('settlement_receipt')
        and not spec.get('credentials_revoked') and not ctx.run.error_code)
    from app.pagebuilder_admission.inputs import no_plan
    task = session.get(PlanTask, ctx.run.plan_task_id) if ctx.run.plan_task_id else None
    if (task is None and not no_plan(ctx.run, spec)) or (task is not None and task.build_run_id != ctx.run.id):
        raise BuildModelError('build_plan_task_binding_invalid')
    if success:
        try:
            recheck(session, ctx, spec, check_rate=False)
        except Exception:
            pools.mark_reconciliation(session, ctx, 'subscription_static_approved_inputs_changed')
            spec = json.loads(ctx.run.spec)
            success = False
    if success:
        spec['lifecycle']['review'] = {'status': 'pending', 'source_artifact_id': ctx.run.artifact_id,
            'report_digest': qa.report_digest, 'qa_id': qa.id, 'contract_digest': qa.contract_digest}
        advance(session, ctx.run, 'succeeded', actor='source_qa', reason='Subscription source import and independent static QA verified')
        if task is not None and task.state == 'running':
            advance(session, task, 'qa', actor='source_qa')
            advance(session, task, 'waiting_for_founder', actor='source_qa')
        if task is not None:
            milestone = session.get(PlanMilestone, task.milestone_id)
            states = list(session.scalars(select(PlanTask.state).where(PlanTask.milestone_id == task.milestone_id)))
            if states and all(state in {'waiting_for_founder', 'done'} for state in states):
                if milestone.state == 'running':
                    advance(session, milestone, 'qa', actor='source_qa')
                if milestone.state == 'qa':
                    advance(session, milestone, 'review', actor='source_qa')
        spec.update(qa_state='passed', phase='awaiting_founder_review')
    else:
        target = 'cancelled' if spec.get('cancel_requested') or ctx.run.error_code == 'founder_cancelled' else 'failed'
        if ctx.run.state != target:
            advance(session, ctx.run, target, actor='source_qa')
        if task is not None and task.state in {'running', 'qa'}:
            advance(session, task, 'failed', actor='source_qa')
        spec.update(qa_state=receipt.get('outcome') if qa_verified else target if qa else 'not_run', phase=target)
    spec.update(cleanup_state='complete', execution_verified=False)
    spec['lifecycle']['phase'] = spec['phase']
    spec['lifecycle']['completion'] = {'source_artifact_id': ctx.run.artifact_id,
        'verified_source_import': bool(success), 'verified_execution': False,
        'verified_report': qa_verified, 'qa_id': qa.id if qa else None,
        'report_complete': receipt.get('report_complete'),
        'source_import_digest': digest(spec['source_import']), 'qa_receipt_digest': digest(spec['lifecycle']['qa'])}
    if qa and qa_verified:
        qa.state = 'passed' if success else 'failed'
        receipt['completion'] = {'build_run_id': ctx.run.id, 'source_artifact_id': ctx.run.artifact_id,
            'report_digest': qa.report_digest, 'verified_source_import': success,
            'verified_execution': False, 'generation': spec['generation']}
        qa.receipt_json = json.dumps(receipt, sort_keys=True)
    if qa:
        # Cleanup and settled child holds above prove this attempt has stopped.
        # A terminal attempt without a report is not a verified QA result.
        if not qa_verified:
            qa.state = spec['phase']
        qa.finished_at = qa.finished_at or naive_utc()
    ctx.run.finished_at = naive_utc()
    ctx.run.spec = json.dumps(spec, sort_keys=True)
    pools.close_pool(session, ctx)
    from app.builder.execution import _event
    _event(session, ctx.run, 'subscription_static_qa_closed', {'state': ctx.run.state,
        'verified_source_import': success, 'verified_report': qa_verified,
        'model_usage_micro': 0, 'builder_usage_micro': 0,
        'message': 'Independent static QA passed; founder review is ready.' if success else
                   'Static source QA stopped; review the recorded failure and usage.'})
    from app.pagebuilder_admission import owns
    if success and owns(ctx.run, spec):
        from app.pagebuilder_admission.publication import queue_if_requested
        queue_if_requested(session, ctx.run, json.loads(ctx.run.spec))
    return True


def verified_source_qa(session, run, spec):
    return source_review_code(session, run, spec) is None


# Only these domain refusals can be reported as a known current-scope failure.
# Arbitrary exception text, identifiers and storage/provider errors are private.
SOURCE_REVIEW_CODES = frozenset({
    'subscription_static_approved_inputs_changed', 'subscription_static_source_not_stored',
    'subscription_static_source_scope_mismatch', 'subscription_static_source_receipt_invalid',
    'subscription_static_publication_baseline_changed', 'subscription_static_publication_baseline_unverified',
    'subscription_static_publication_baseline_ambiguous', 'subscription_static_app_ambiguous',
    'coding_source_order_not_found', 'coding_source_order_not_settled', 'coding_source_app_ambiguous',
    'device_task_not_found', 'device_task_changed', 'device_task_not_approved', 'device_task_not_ready',
    'device_coding_qa_feedback_unavailable', 'build_plan_inputs_changed', 'build_plan_superseded',
    'build_plan_approval_required', 'build_plan_provenance_unverified', 'build_plan_task_binding_invalid',
    'build_task_not_executable', 'build_task_dependencies_incomplete', 'build_reuse_manifest_required',
    'build_source_artifact_unverified', 'build_scope_not_found', 'authenticated_device_required',
    'workspace_blocked', 'build_manage_apps_required',
    'build_review_source_identity_changed', 'build_review_request_fingerprint_changed',
    'build_review_task_inputs_changed', 'subscription_static_contract_changed',
    'subscription_static_task_snapshot_changed', 'subscription_static_profile_changed',
    'build_review_resource_inputs_changed', 'build_review_research_inputs_changed',
    'build_review_idea_inputs_changed', 'build_review_memory_inputs_changed',
    'build_review_publication_inputs_changed',
})


def source_review_code(session, run, spec):
    """Saved proof plus the same current admission; never a historical waiver."""
    if run.trigger == 'pagebuilder_source_qa':
        from app.pagebuilder_admission.controller import review_code
        return review_code(session, run, spec)
    try:
        if not _verified_source_qa(session, run, spec, current_scope=False):
            return 'build_review_source_proof_invalid'
        recheck(session, SimpleNamespace(run=run), spec, check_rate=False)
    except Exception as error:
        if getattr(error, 'reasons', None) == ['build_review_check_unavailable']:
            return 'build_review_check_unavailable'
        reasons = getattr(error, 'reasons', None)
        diagnostics = getattr(error, 'diagnostics', None)
        reason = (reasons[0] if isinstance(reasons, list) and len(reasons) == 1 else
                  diagnostics.get('review_reason') if isinstance(diagnostics, dict) else
                  getattr(error, 'review_reason', None))
        if isinstance(reason, str) and reason in SOURCE_REVIEW_CODES:
            return reason
        code = getattr(error, 'code', None)
        if code is None and isinstance(error, (ValueError, KeyError, PermissionError)) and len(error.args) == 1:
            code = error.args[0]
        return code if isinstance(code, str) and code in SOURCE_REVIEW_CODES else 'build_review_check_unavailable'
    return None


def verified_historical_source_qa(session, run, spec):
    """Immutable closed proof only, for an already-published predecessor."""
    if run.trigger == 'pagebuilder_source_qa':
        from app.pagebuilder_admission.controller import verified
        return verified(session, run, spec)
    return _verified_source_qa(session, run, spec, current_scope=False)


def _verified_source_qa(session, run, spec, *, current_scope, expected_trigger='coding_source_import'):
    """Independent import+QA proof for review/hosting, never builder flags."""
    if (run.trigger != expected_trigger or spec.get('lifecycle_version') != 3
            or run.state != 'succeeded' or not run.closed_at
            or spec.get('execution_verified') is not False
            or spec.get('source_import_verified') is not True):
        return False
    completion = spec.get('lifecycle', {}).get('completion', {})
    imported = spec.get('source_import', {})
    if (completion.get('verified_execution') is not False
            or completion.get('verified_source_import') is not True
            or completion.get('verified_report') is not True
            or completion.get('source_import_digest') != digest(imported)
            or imported.get('artifact_receipt_digest') != digest(spec.get('artifact_capture'))
            or completion.get('qa_receipt_digest') != digest(spec['lifecycle'].get('qa'))):
        return False
    qa = session.get(QaRun, completion.get('qa_id'))
    if (not qa or qa.state != 'passed' or not qa_receipts.serialize(qa)['verified_report']
            or (qa.build_run_id, qa.workspace_id, qa.venture_id, qa.app_id, qa.source_artifact_id)
                != (run.id, run.workspace_id, run.venture_id, run.app_id, run.artifact_id)
            or qa.generation != spec.get('generation')
            or qa.source_sha256 != spec.get('artifact_capture', {}).get('sha256')
            or qa.source_manifest_digest != spec.get('artifact_capture', {}).get('manifest_digest')
            or not qa_receipts.serialize(qa)['verified_source_import']
            or qa.failed != 0 or qa.unsupported != 0 or qa.passed <= 0):
        return False
    from core.static_qa_contracts import Contract
    if qa.contract_digest != Contract.model_validate(spec['inputs']['task']['landing_expectations']['contract']).digest:
        return False
    if session.scalar(select(BuildModelCall.id).where(BuildModelCall.build_run_id == run.id).limit(1)):
        return False
    children = session.scalars(select(CreditReservation).where(CreditReservation.parent_id == run.reservation_id)).all()
    if (not children or any(child.purpose != 'build.compute.qa' or child.status != 'settled' for child in children)
            or not spec['lifecycle']['qa'].get('settlement_receipt')):
        return False
    if current_scope:
        try:
            ctx = SimpleNamespace(run=run)
            recheck(session, ctx, spec, check_rate=False)
        except Exception:
            return False
    return True
