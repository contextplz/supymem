"""Read-only failure guidance from one owned verified static QA receipt."""
from __future__ import annotations

import json
import re

from sqlalchemy import select


class VerifiedStaticQaEvidence(dict):
    """Server-produced owned/current receipt quotes, never a model/client marker."""


def request_run_id(text: str) -> str | None:
    ids = re.findall(r'\bbsi_[a-f0-9]{48}\b', text or '')
    if len(ids) != 1 or not re.search(r'\badvice only\b', text, re.I):
        return None
    if not (re.search(r'\bclosed failed\b', text, re.I) and re.search(r'\bQA\b', text, re.I)
            and re.search(r'\bnext steps\b', text, re.I) and '?' in text):
        return None
    # This seam admits advice, never a mixed positive command after constraints.
    positive = '. '.join(re.split(r'\b(?:do not|don[\'’]t|never)\b', sentence,
                                  maxsplit=1, flags=re.I)[0]
                         for sentence in re.split(r'[.!?;]\s*', text))
    if re.search(r'(?:^|[.!?;:]\s*)(?:(?:but|then|instead|also)\s+)?(?:please\s+)?'
                 r'(?:prepare|run|retry|change|accept|publish|send|search|build|create)\b', positive, re.I):
        return None
    return ids[0]


def answer(venture_id: str, workspace_id: str | None, run_id: str) -> dict:
    from app.state import db
    from app.build import device_orders
    from app.builder import execution, qa_receipts
    from app.db.supymem_models import BuildRun, BuildArtifact, QaRun, QaFinding, PlanTask
    from app.db.models import CreditReservation, CreditLedgerEntry
    from core.static_qa_contracts import Contract

    unavailable = {'kind': 'text', 'body_md': 'I cannot ground a source correction in a current owned, closed failed static QA report and approved contract. Open this venture’s Evidence → Builds → Review QA results to inspect the recorded failure. I have not prepared, retried, accepted or published anything.', 'actions': [], 'citations': []}
    with db()() as session:
        run = session.get(BuildRun, run_id)
        if (run is None or not workspace_id or (run.workspace_id, run.venture_id) != (workspace_id, venture_id)
                or run.trigger != 'coding_source_import' or run.runtime_version != 1
                or run.state != 'failed' or run.closed_at is None):
            return unavailable
        spec = json.loads(run.spec or '{}')
        if (spec.get('lifecycle_version') != 3 or spec.get('cleanup_state') != 'complete'
                or spec.get('source_import_verified') is not True or spec.get('artifact_verified') is not True):
            return unavailable
        view = execution._serialize(session, run)
        if view['usage']['reserved_micro'] != 0 or view['usage']['unresolved_micro'] != 0:
            return unavailable
        qa = session.get(QaRun, spec.get('lifecycle', {}).get('qa_id'))
        artifact = session.get(BuildArtifact, run.artifact_id)
        task = session.get(PlanTask, run.plan_task_id)
        if (not qa or not artifact or not task or task.build_run_id != run.id
                or (qa.build_run_id, qa.workspace_id, qa.venture_id, qa.app_id, qa.source_artifact_id)
                != (run.id, workspace_id, venture_id, run.app_id, artifact.id)
                or (artifact.build_run_id, artifact.workspace_id, artifact.app_id) != (run.id, workspace_id, run.app_id)
                or artifact.sha256 != qa.source_sha256 or not qa_receipts.serialize(qa)['verified_report']
                or qa_receipts.serialize(qa)['outcome'] != 'failed' or qa.failed < 1):
            return unavailable
        capture = spec.get('artifact_capture') or {}
        if (capture.get('state') != 'stored' or capture.get('artifact_id') != artifact.id
                or capture.get('sha256') != artifact.sha256
                or capture.get('manifest_digest') != qa.source_manifest_digest):
            return unavailable
        try:
            current = device_orders._coding_snapshot_profile(session, device_orders._task_snapshot(
                session, workspace_id, venture_id, run.plan_task_id, allow_failed_coding=True))
            saved = spec['source_import']['identity']['task_snapshot']
            if any(current.get(key) != value for key, value in saved.items()
                   if key not in {'closed_failed_build_run_id', 'coding_qa_feedback'}):
                return unavailable
            expected = current['landing_expectations']
            contract = Contract.model_validate(expected['contract'])
            if contract.digest != qa.contract_digest:
                return unavailable
        except (KeyError, TypeError, ValueError):
            return unavailable
        findings = list(session.scalars(select(QaFinding).where(
            QaFinding.qa_run_id == qa.id, QaFinding.workspace_id == workspace_id).order_by(QaFinding.id)))
        if not findings or len(findings) > 64:
            return unavailable
        try:
            details = [{'case': f.title, **json.loads(f.detail)} for f in findings]
        except (TypeError, ValueError):
            return unavailable
        if len(json.dumps(details)) > 20000:
            return unavailable
        pool = session.get(CreditReservation, run.reservation_id)
        children = list(session.scalars(select(CreditReservation).where(CreditReservation.parent_id == run.reservation_id)))
        if not pool or pool.status != 'closed' or any(c.status != 'settled' or c.purpose != 'build.compute.qa' for c in children):
            return unavailable
        spent = list(session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.reservation_id.in_([c.id for c in children]), CreditLedgerEntry.account_id == pool.account_id, CreditLedgerEntry.kind == 'spend')))
        if sum(c.settled_micro or 0 for c in children) != run.credits_used or -sum(e.amount_micro for e in spent) != run.credits_used:
            return unavailable
        summary = f'Run {run.id} remains failed and closed, with cleanup complete. Its verified browser report has {qa.passed} passed, {qa.failed} failed and {qa.unsupported} unsupported checks. No source is accepted or published by this answer.'
        canonical = json.dumps({'required_text': expected['contract']['required_text'],
            'visuals': expected['contract'].get('visuals', []),
            'actions': expected['contract'].get('actions', [])}, ensure_ascii=False, indent=2)
        observed = json.dumps(details, ensure_ascii=False, indent=2)
    body = (summary + '\n\n1. Keep this failed attempt and its charge. Supymem must carry the existing exact approved copy, visual names, action targets and failed cases into a fresh reviewed correction packet; you do not need to invent replacement copy.\n\n'
        '2. Resolve the saved contract/layout ambiguity before correction: current static QA requires all required copy and two substantial canonically named visual previews on initial load. If the approved criterion says the toggle reveals those steps or illustration, disclose that mismatch and review a layout or contract correction. Do not silently weaken QA, call hidden content absent, or describe the concept as working automation. A proposed layout can show the canonical steps/previews initially and let the toggle reveal additional explanation.\n\n'
        '3. Supymem must supply the exact app-bound public ingest-only platform analytics script in the immutable review packet. Platform analytics is mandatory here, not a choice of third-party provider. Include it once; preserve the existing managed waitlist target and target="_blank" with rel="noopener noreferrer". This read-only answer has not generated a key, prepared or run coding, retried QA, changed the plan, accepted source or published. Only a separately reviewed correction and a fresh independently priced QA attempt can follow.\n\n'
        'Current approved exact contract (including any literal ellipsis already saved; do not silently expand it):\n```json\n' + canonical + '\n```\n\nVerified failed cases:\n```json\n' + observed + '\n```')
    return VerifiedStaticQaEvidence(kind='text', body_md=body, actions=[], citations=[])
