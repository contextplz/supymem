"""No-model adult discovery after a rejected review; no validation authority."""
from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import select

from app.db.models import Job
from app.db.supymem_models import IdeaRevision, IdeaSession, ValidationRun
from app.state import db
from core.clock import utcnow

CODE = 'research_invalid_output'
NOTE_CAP = 1500


def rejected_request(venture_id, workspace_id, payload):
    # Error labels in a client action are hints, never the authority. Removing
    # or changing the label cannot escape the bound rejected-job recovery path.
    if payload.get("error_code") == CODE:
        return True
    job_id = payload.get("job_id")
    with db()() as session:
        job = session.get(Job, job_id) if isinstance(job_id, str) and job_id else None
        return bool(job and job.kind == "idea.validate" and job.last_error == CODE)


def binding(session, venture_id, workspace_id, job_id):
    job = session.get(Job, job_id) if isinstance(job_id, str) and job_id else None
    if (job is None or job.kind != 'idea.validate' or job.status not in {'failed', 'failed_dead', 'poison'}
            or job.last_error != CODE or (job.venture_id, job.workspace_id) != (venture_id, workspace_id)):
        raise ValueError('validation_recovery_scope_unavailable')
    payload = json.loads(job.payload_json)
    idea = session.get(IdeaSession, payload.get('session_id'), with_for_update=True)
    revision = session.get(IdeaRevision, payload.get('revision_id'))
    if (idea is None or revision is None or idea.state in {'confirmed', 'abandoned'}
            or (idea.venture_id, idea.workspace_id) != (venture_id, workspace_id)
            or revision.session_id != idea.id or (revision.venture_id, revision.workspace_id) != (venture_id, workspace_id)
            or idea.current_revision_id != revision.id):
        raise ValueError('validation_recovery_revision_changed')
    return idea, revision, job


def guide(venture_id, workspace_id, job_id):
    with db()() as session:
        idea, revision, _job = binding(session, venture_id, workspace_id, job_id)
        return {'session_id': idea.id, 'revision_id': revision.id,
            'body': ('The model review was rejected; it is not a usable validation result. This guide makes no model call '
                     'without another model call. Earlier attempts stay recorded; no refund or success is implied.\n\n'
                     '1. Choose one consenting adult prospective user/provider through their own published business contact. '
                     'Ask for a voluntary 15-minute, non-sales conversation; do not claim a working product or customers.\n'
                     '2. Ask: "Describe your general workflow anonymously. Which recurring administrative task is frustrating, '
                     'and what workaround do you use?" Discuss time or effort as their report, not measured proof. '
                     'Use anonymized verbal descriptions only: no children, pickup observation, parent conversations, child records, '
                     'photos, medical information, private client chats, screenshots or customer/family contact lists.\n'
                     '3. Bring back a short anonymized note: interview not yet conducted OR adult interview conducted; '
                     'what they reported, what remains unknown, and one proposed next test. No names, email addresses or phone numbers.\n\n'
                     'Submit my result saves your note on this exact idea revision as founder-provided and unverified. '
                     'It does not pass or accept the rejected review, confirm the idea, start research, or claim users/payment. '
                     'A later explicitly approved validation retry is metered and will receive this note as attributed input.')}


def save(venture_id, workspace_id, job_id, text):
    if not isinstance(text, str) or not text.strip() or len(text) > NOTE_CAP:
        raise ValueError('validation_recovery_note_invalid')
    if (re.search(r'\b[^\s@]+@[^\s@]+\.[^\s@]+\b|(?:\d[\s()+.-]*){7,}', text)
            or re.search(r"\b(?:child(?:'s)?\s+name|date\s+of\s+birth|medical\s+record|client\s+phone)\s*(?::|=|is\b)", text, re.I)):
        raise ValueError('validation_recovery_private_details')
    with db()() as session:
        idea, revision, job = binding(session, venture_id, workspace_id, job_id)
        from app.idea.discovery_privacy import unsafe_discovery_request
        if unsafe_discovery_request({"note": text}, revision.structured):
            raise ValueError("validation_recovery_private_details")
        rid = 'vlr_' + hashlib.sha256((job.id + ':' + revision.id + ':' + text).encode()).hexdigest()[:32]
        if session.get(ValidationRun, rid) is None:
            report = {'provenance': 'founder_provided', 'recovery_kind': 'adult_discovery_note_v1',
                'founder_answers': text, 'verified': False, 'failed_job_id': job.id,
                'strengths': [], 'proposed_tests': [], 'gaps': ['Founder-reported observations or proposed steps; no AI or market validation was performed.'],
                'usage': {'source': 'founder_provided', 'credits_micro': 0}}
            session.add(ValidationRun(id=rid, session_id=idea.id, revision_id=revision.id,
                workspace_id=workspace_id, venture_id=venture_id, method='founder_provided', state='complete',
                verdict='needs_evidence', report=json.dumps(report), created_at=utcnow(), completed_at=utcnow()))
            session.commit()
        return rid


def notes(session, idea, revision_id):
    rows = session.scalars(select(ValidationRun).where(ValidationRun.session_id == idea.id,
        ValidationRun.revision_id == revision_id, ValidationRun.workspace_id == idea.workspace_id,
        ValidationRun.venture_id == idea.venture_id, ValidationRun.method == 'founder_provided',
        ValidationRun.state == 'complete').order_by(ValidationRun.created_at.desc(), ValidationRun.id).limit(5)).all()
    for row in rows:
        report = json.loads(row.report or '{}')
        if report.get('provenance') == 'founder_provided' and report.get('recovery_kind') == 'adult_discovery_note_v1':
            return {'source': 'founder_provided', 'verified': False, 'validation_run_id': row.id,
                    'revision_id': revision_id, 'failed_job_id': report['failed_job_id'],
                    'text': report['founder_answers'][:NOTE_CAP]}
    return None
