"""Read-only review diagnostics shared with locked acceptance admission.

This is a momentary check, not a reservation, approval or publication receipt.
Only closed codes leave this module. Saved QA evidence is never rewritten.
"""
from __future__ import annotations

import json

from sqlalchemy import select

from app.db.supymem_models import ExecutionPlan, FounderApp, PlanTask, QaRun

_UNSET = object()


def plan_input_reason(saved, live):
    """Name a changed source surface without exporting its content or IDs."""
    previous = saved.get('_runtime', {}).get('context', {})
    current = live.get('context', {})
    for field, code in (
        ('assets', 'build_review_resource_inputs_changed'),
        ('sources', 'build_review_research_inputs_changed'),
        ('idea', 'build_review_idea_inputs_changed'),
        ('institutional_memory', 'build_review_memory_inputs_changed'),
        ('managed_publication', 'build_review_publication_inputs_changed'),
        ('managed_conversion', 'build_review_publication_inputs_changed'),
    ):
        old, new = previous.get(field), current.get(field)
        if field == 'institutional_memory':
            old = old if old and (old.get('statements') or old.get('omitted_count')) else None
            new = new if new and (new.get('statements') or new.get('omitted_count')) else None
        if old != new:
            return code
    return 'build_plan_inputs_changed'


def result(code=None, *, status=None, error_code='build_review_not_ready'):
    return {'schema_version': 1, 'status': status or (
        'ready' if code is None else 'check_unavailable' if code == 'build_review_check_unavailable' else 'blocked'),
        'code': code, 'error_code': error_code if code else None}


def evaluate(session, run, spec, *, actor, source_artifact_id=_UNSET, report_digest=_UNSET, repeat=False, read_only=False):
    """No locks, writes, provider calls, queues or policy configuration changes."""
    from app.builder import execution
    from app.build.subscription_static_controller import source_review_code
    from app.builder.model_proxy import digest
    from app.planning.compiler import _current

    reviewed = spec.get('lifecycle', {}).get('review', {})
    if (run.runtime_version != 1 or spec.get('lifecycle_version') not in {2, 3}
            or run.trigger == 'subscription_product_diagnostic'):
        return result('build_review_not_applicable', status='not_applicable')
    if reviewed.get('decision'):
        if not repeat:
            return result('build_review_already_recorded', status='not_applicable')
        # Preserve existing idempotent accepted semantics, including the current
        # source check; the already completed task need not be waiting again.
        if reviewed.get('decision') == 'accept' and spec.get('lifecycle_version') == 3:
            return result(source_review_code(session, run, spec))
        return result()
    try:
        execution._app(session, run.app_id, actor, mutate=True)
    except PermissionError as error:
        return result(error.args[0] if error.args == ('build_manage_apps_required',)
                      else 'build_review_scope_unavailable')
    except KeyError:
        return result('build_review_scope_unavailable')
    if run.state != 'succeeded' or not run.closed_at or reviewed.get('status') != 'pending':
        return result('build_review_deliverable_not_closed')
    if spec.get('lifecycle_version') == 3:
        code = source_review_code(session, run, spec)
        if code:
            return result(code)
    elif not spec.get('execution_verified'):
        return result('build_review_execution_unverified')
    artifact = run.artifact_id if source_artifact_id is _UNSET else source_artifact_id
    report = reviewed.get('report_digest') if report_digest is _UNSET else report_digest
    qa = session.get(QaRun, reviewed.get('qa_id')) if reviewed.get('qa_id') else None
    if ((artifact, report) != (run.artifact_id, reviewed.get('report_digest'))
            or not qa or qa.report_digest != report or qa.source_artifact_id != artifact):
        return result('build_review_receipt_mismatch')
    from app.pagebuilder_admission.inputs import no_plan
    if no_plan(run, spec):
        return result() if actor == spec.get('actor') else result('build_review_scope_unavailable')
    try:
        task = session.get(PlanTask, run.plan_task_id)
        if spec['inputs'].get('revision'):
            current = execution._recheck_inputs(session, session.get(FounderApp, run.app_id),
                                                spec['inputs'], actor=actor, read_only=read_only)
            if digest(current) != digest(spec['inputs']):
                return result('hosting_revision_baseline_changed', error_code='hosting_revision_baseline_changed')
            if not task or (task.workspace_id, task.venture_id) != (run.workspace_id, run.venture_id):
                return result('build_review_task_changed', error_code='build_review_task_changed')
        else:
            plan = session.get(ExecutionPlan, spec['inputs']['plan_id'])
            if not plan or (plan.workspace_id, plan.venture_id) != (run.workspace_id, run.venture_id):
                return result('build_plan_inputs_changed', error_code='build_plan_inputs_changed')
            live, stale = _current(plan, json.loads(plan.structured), session,
                                   include_builder_runtime=spec.get('lifecycle_version') != 3)
            if live is None:
                return result('build_review_check_unavailable')
            if stale or session.scalar(select(ExecutionPlan.id).where(
                    ExecutionPlan.venture_id == run.venture_id, ExecutionPlan.version > plan.version,
                    ExecutionPlan.state.in_(['approved', 'founder_review'])).limit(1)):
                return result(plan_input_reason(json.loads(plan.structured), live) if stale else 'build_plan_superseded',
                              error_code='build_plan_inputs_changed')
            if not task or task.build_run_id != run.id or task.state != 'waiting_for_founder':
                return result('build_review_task_changed', error_code='build_review_task_changed')
    except execution.BuildError as error:
        if error.code in {
            'hosting_revision_request_not_found', 'hosting_revision_baseline_changed',
            'hosting_revision_source_invalid', 'hosting_revision_instruction_invalid',
            'hosting_live_static_site_required', 'hosting_live_source_unverified',
            'hosting_build_not_accepted', 'private_correction_baseline_changed',
            'private_correction_plan_changed', 'private_correction_venture_paused',
            'private_correction_use_published_revision', 'private_correction_static_source_required',
        }:
            return result(error.code, error_code=error.code)
        return result('build_review_check_unavailable')
    except Exception:
        return result('build_review_check_unavailable')
    return result()


def read(session, run, *, actor):
    # Even a malformed saved spec must not turn a GET into raw exception text or
    # allow an action. Suppress autoflush if used by another read transaction.
    try:
        with session.no_autoflush:
            return evaluate(session, run, json.loads(run.spec or '{}'), actor=actor, read_only=True)
    except Exception:
        return result('build_review_check_unavailable')


def require(session, run, spec, **kwargs):
    from app.builder.execution import BuildError
    readiness = evaluate(session, run, spec, **kwargs)
    if readiness['status'] != 'ready':
        raise BuildError(readiness['error_code'], reasons=[readiness['code']],
                         status=503 if readiness['status'] == 'check_unavailable' else 409)
    return readiness
