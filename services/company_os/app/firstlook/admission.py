"""Firstlook copy uses the original committed onboarding step, never a retry ID."""
from __future__ import annotations

import json
import re

from fastapi import HTTPException
from sqlalchemy import select, update

from app.credits.errors import CreditsUnavailable
from app.db.models import OnboardingRun, OnboardingStep, Person, Venture, Workspace
from app.onboarding.resume import idempotency_key
from app.onboarding.state import Ctx
from app.secrets.detect import looks_like_secret
from app.state import db
from app.tenancy import current_or_none

_STEP = "commit.lock_charter"
_INTENT = re.compile(r"onboarding:([A-Za-z0-9_-]{1,128}):([a-f0-9]{64})")
_SLOTS = {"first", "lint-regenerate"}


def _refuse():
    raise CreditsUnavailable("firstlook_generation_intent_unavailable")


def _current_step(intent_id: str, workspace_id: str, venture_id: str, *, session=None):
    if session is None:
        with db()() as owned_session:
            return _current_step(intent_id, workspace_id, venture_id, session=owned_session)
    match = _INTENT.fullmatch(intent_id) if type(intent_id) is str else None
    scope = current_or_none()
    from app.authz.workspace import subject_person_id
    from app.firstlook.versions import validate_binding
    from app.workspaces.resolve import LOCAL_PERSON_ID

    if (match is None or type(workspace_id) is not str or not workspace_id
            or type(venture_id) is not str or not venture_id or scope is None
            or scope.workspace_id != workspace_id or scope.venture_id != venture_id
            or type(scope.actor) is not str or scope.actor in {"system", "anonymous"}
            or subject_person_id(scope.actor) == LOCAL_PERSON_ID):
        _refuse()
    try:
        validate_binding(workspace_id, venture_id)
    except (PermissionError, HTTPException):
        _refuse()
    run_id, key = match.groups()
    run = session.get(OnboardingRun, run_id, with_for_update=True)
    venture = session.get(Venture, venture_id, with_for_update=True)
    workspace = session.get(Workspace, workspace_id, with_for_update=True)
    if (run is None or venture is None or workspace is None
            or session.get(Person, subject_person_id(scope.actor)) is None
            or run.venture_id != venture_id or run.workspace_id not in {None, workspace_id}
            or run.status != "running" or run.phase != "commit" or run.finished_at is not None
            or run.entry_mode != "idea" or run.mode not in {"guided", "auto"}
            or venture.workspace_id != workspace_id or venture.archived_at is not None
            or workspace.archived_at is not None):
        _refuse()
    steps = session.scalars(select(OnboardingStep).where(
        OnboardingStep.run_id == run_id, OnboardingStep.step_id == _STEP,
        OnboardingStep.status == "running").limit(2).with_for_update()).all()
    if (len(steps) != 1 or steps[0].phase != "commit" or steps[0].finished_at is not None
            or steps[0].idempotency_key != key or type(steps[0].attempt) is not int
            or steps[0].attempt < 1):
        _refuse()
    # A changed input key in the same step cannot mint another paid intent.
    if session.scalar(select(OnboardingStep.id).where(
            OnboardingStep.run_id == run_id, OnboardingStep.step_id == _STEP,
            OnboardingStep.idempotency_key != key).limit(1)) is not None:
        _refuse()
    fresh = Ctx.build(run)
    if idempotency_key(run_id, _STEP, fresh.input_digest(_STEP)) != key:
        _refuse()
    return steps[0].attempt


def validate_generation_intent(intent_id: str, *, workspace_id: str, venture_id: str,
                               for_dispatch: bool = True) -> str:
    """Recheck persisted scope; retries may look up original money, never quote anew."""
    if type(for_dispatch) is not bool:
        _refuse()
    attempt = _current_step(intent_id, workspace_id, venture_id)
    if for_dispatch and attempt != 1:
        _refuse()
    return intent_id


def capture_onboarding_intent(ctx: Ctx) -> str:
    """Forward the already committed step key without changing onboarding state."""
    if ctx.cancel.is_set():
        _refuse()
    key = idempotency_key(ctx.run.id, _STEP, ctx.input_digest(_STEP))
    return validate_generation_intent(f"onboarding:{ctx.run.id}:{key}",
        workspace_id=ctx.venture.workspace_id, venture_id=ctx.venture.id, for_dispatch=False)


def generation_call_id(intent_id: str, *, workspace_id: str, venture_id: str,
                       slot: str, for_dispatch: bool = True) -> str:
    """Only the original first call and existing honesty-lint repair are allowed."""
    identity = generation_identity(intent_id, slot=slot)
    validate_generation_intent(intent_id, workspace_id=workspace_id,
        venture_id=venture_id, for_dispatch=for_dispatch)
    return identity


def generation_identity(intent_id: str, *, slot: str) -> str:
    """A canonical lookup key only; this never grants quote, I/O or output authority."""
    if (type(intent_id) is not str or _INTENT.fullmatch(intent_id) is None
            or type(slot) is not str or slot not in _SLOTS):
        _refuse()
    return f"{intent_id}:firstlook:{slot}"


def _copy_output(output):
    """Only bounded normalized domain copy, never SDK response or billing fields."""
    if output is None:
        return None
    fields = {"headline", "subhead", "points", "how_it_works", "cta", "audience",
              "name_candidates", "assumptions", "fallback"}
    if type(output) is not dict or set(output) != fields:
        _refuse()
    def words(value, limit):
        return type(value) is str and len(value) <= limit and not looks_like_secret(value)
    if (not words(output["headline"], 90) or not words(output["subhead"], 140)
            or not words(output["audience"], 80) or type(output["fallback"]) is not bool):
        _refuse()
    points, how, cta = output["points"], output["how_it_works"], output["cta"]
    if (type(points) is not list or len(points) != 3
            or any(type(p) is not dict or set(p) != {"title", "body"}
                or not words(p["title"], 36) or not words(p["body"], 180) for p in points)
            or type(cta) is not dict or set(cta) != {"label", "kind"}
            or not words(cta["label"], 22) or type(cta["kind"]) is not str
            or cta["kind"] not in {"waitlist", "notify", "book", "subscribe"}):
        _refuse()
    if how is not None and (type(how) is not list or len(how) > 3
            or any(type(p) is not dict or set(p) != {"step", "text"}
                or type(p["step"]) is not int or not 1 <= p["step"] <= 3
                or not words(p["text"], 80) for p in how)):
        _refuse()
    for name, minimum, limit in (("name_candidates", 3, 28), ("assumptions", 1, 2048)):
        items = output[name]
        if type(items) is not list or not minimum <= len(items) <= 3 or any(not words(v, limit) for v in items):
            _refuse()
    encoded = json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 8192:
        _refuse()
    return json.loads(encoded)


validate_generation_output = _copy_output


def _original_output(session, intent_id):
    run_id, key = _INTENT.fullmatch(intent_id).groups()
    row = session.scalar(select(OnboardingStep).where(OnboardingStep.run_id == run_id,
        OnboardingStep.step_id == _STEP, OnboardingStep.phase == "commit",
        OnboardingStep.attempt == 1, OnboardingStep.idempotency_key == key).with_for_update())
    if row is None or row.status not in {"running", "failed"}:
        _refuse()
    try:
        document = json.loads(row.output_json) if row.output_json else {}
    except (TypeError, ValueError):
        _refuse()
    if type(document) is not dict or type(document.get("_firstlook_copy", {})) is not dict:
        _refuse()
    slots = document.get("_firstlook_copy", {})
    if set(slots) - _SLOTS:
        _refuse()
    for output in slots.values():
        _copy_output(output)
    return row, document


def checkpoint_generation_output(intent_id: str, *, workspace_id: str, venture_id: str,
                                 slot: str, output: dict | None) -> None:
    """CAS the safe private copy into the original step before receipt pricing."""
    generation_identity(intent_id, slot=slot)
    output = _copy_output(output)
    from app.db.session import serialize_writes

    with db()() as session:
        serialize_writes(session, key="firstlook-copy:" + intent_id)
        _current_step(intent_id, workspace_id, venture_id, session=session)
        row, document = _original_output(session, intent_id)
        slots = document.setdefault("_firstlook_copy", {})
        if slot in slots:
            if _copy_output(slots[slot]) != output:
                _refuse()
            return
        previous, original_id = row.output_json, row.id
        slots[slot] = output
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        changed = session.execute(update(OnboardingStep).where(OnboardingStep.id == row.id,
            OnboardingStep.output_json == previous, OnboardingStep.idempotency_key == row.idempotency_key,
            OnboardingStep.status == row.status).values(output_json=encoded)
            .execution_options(synchronize_session=False)).rowcount
        if changed != 1:
            _refuse()
        session.commit()
        session.expire_all()
        if session.get(OnboardingStep, original_id).output_json != encoded:
            _refuse()


def read_generation_output(intent_id: str, *, workspace_id: str, venture_id: str,
                           slot: str) -> tuple[bool, dict | None]:
    """Private domain output only; the caller must recover known money first."""
    generation_identity(intent_id, slot=slot)
    with db()() as session:
        _current_step(intent_id, workspace_id, venture_id, session=session)
        _, document = _original_output(session, intent_id)
        slots = document.get("_firstlook_copy", {})
        return (True, _copy_output(slots[slot])) if slot in slots else (False, None)
